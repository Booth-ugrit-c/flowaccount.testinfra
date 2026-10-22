#!/bin/sh
set -eu

ROOT=${SIM_ROOT:-sim/dotnet}
WORKSPACE=${GITHUB_WORKSPACE:-$(pwd)}

cache() {
    if [ -n "${SIM_CACHE_CMD:-}" ]; then
        "$SIM_CACHE_CMD" "$@"
    else
        python3 -m tools.sim_gate.cache "$@"
    fi
}

dotnet_cmd() {
    if [ -n "${SIM_DOTNET:-}" ]; then
        "$SIM_DOTNET" "$@"
    else
        dotnet "$@"
    fi
}

case ${RUNNER_ENVIRONMENT:-} in
    self-hosted) endpoint=${SIM_CACHE_ENDPOINT_LAB:-} ;;
    github-hosted) endpoint=${SIM_CACHE_ENDPOINT_TAILNET:-} ;;
    *) endpoint= ;;
esac
if [ -n "$endpoint" ]; then
    SIM_CACHE_ENDPOINT=$endpoint
    export SIM_CACHE_ENDPOINT
else
    unset SIM_CACHE_ENDPOINT
fi

mode=${1:-}
case $mode in
    tripwire)
        markers=${2:?usage: cache_build.sh tripwire MARKERS_DIR}
        mkdir -p "$markers"
        cache tripwire --markers "$markers" || true
        if [ -e "$markers/cache-tripwire" ]; then
            echo "cache: the store reports a tripwire; stopping before any build" >&2
            exit 1
        fi
        ;;
    build)
        plan=${2:?usage: cache_build.sh build PLAN_FILE BUILT_FILE}
        built=${3:?usage: cache_build.sh build PLAN_FILE BUILT_FILE}
        : > "$built"
        while IFS= read -r project; do
            [ -n "$project" ] || continue
            if [ "${SIM_CACHE_MODE:-use}" != off ]; then
                answer=$(cache restore --project "$project") || answer=
                if [ "$answer" = hit ]; then
                    echo "cache: hit ${project##*/}"
                    continue
                fi
            fi
            echo "cache: build ${project##*/}"
            (cd "$ROOT" && dotnet_cmd build "$WORKSPACE/$project" -c Release --disable-build-servers \
                -p:BuildProjectReferences=false -p:IncludeSourceRevisionInInformationalVersion=false) || {
                echo "cache: build failed ${project##*/}" >&2
                exit 1
            }
            printf '%s\n' "$project" >> "$built"
        done < "$plan"
        ;;
    save)
        built=${2:?usage: cache_build.sh save BUILT_FILE}
        [ "${SIM_CACHE_MODE:-use}" != off ] || exit 0
        [ -s "$built" ] || exit 0
        branch=${SIM_DEFAULT_BRANCH:-}
        case $branch in
            '' | -* | *[!A-Za-z0-9._/-]* | *..*) tip= ;;
            *) tip=$(git rev-parse --verify --quiet "refs/remotes/origin/$branch" || true) ;;
        esac
        head=$(git rev-parse --verify --quiet HEAD || true)
        if [ -z "$tip" ] || [ "$tip" != "$head" ]; then
            echo "skip: not the default branch tip"
            exit 0
        fi
        dirty=$(git status --porcelain --untracked-files=all | grep -Ev '^.. (.*/)?(bin|obj)/' || true)
        if [ -n "$dirty" ]; then
            echo "cache: no save, the tree has untracked or modified files outside bin and obj"
            exit 0
        fi
        while IFS= read -r project; do
            [ -n "$project" ] || continue
            cache save --project "$project" || echo "cache: save skipped for ${project##*/}"
        done < "$built"
        ;;
    *)
        echo "usage: cache_build.sh tripwire|build|save ..." >&2
        exit 2
        ;;
esac

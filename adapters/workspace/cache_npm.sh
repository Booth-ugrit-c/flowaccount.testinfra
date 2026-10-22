#!/bin/sh
set -eu

ROOT=${SIM_ROOT:-sim/workspace}

cache() {
    if [ -n "${SIM_CACHE_CMD:-}" ]; then
        "$SIM_CACHE_CMD" "$@"
    else
        python3 -m adapters.workspace.cache "$@"
    fi
}

store() {
    if [ -n "${SIM_STORE_CMD:-}" ]; then
        "$SIM_STORE_CMD" "$@"
    else
        python3 -m core.store "$@"
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

cache_off() {
    : > "$1/cache-off"
    echo "::warning title=cache off::$2; this run installs without the cache"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] || printf 'cache off: %s\n' "$2" >> "$GITHUB_STEP_SUMMARY"
}

if [ -n "${MARKERS:-}" ] && [ -e "$MARKERS/cache-off" ]; then
    SIM_CACHE_MODE=off
fi

mode=${1:-}
case $mode in
    tripwire)
        markers=${2:?usage: cache_npm.sh tripwire MARKERS_DIR}
        mkdir -p "$markers"
        if [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ] && [ "${SIM_TAILNET_JOIN:-}" != success ]; then
            cache_off "$markers" "the tailnet join failed"
            exit 0
        fi
        answer=$(store tripwire --markers "$markers") || answer=
        if [ "$answer" = tripped ] || [ -e "$markers/cache-tripwire" ]; then
            echo "cache: the store reports a tripwire; stopping before any install" >&2
            exit 1
        fi
        if [ "$answer" != clear ]; then
            cache_off "$markers" "the store did not answer the tripwire check"
        fi
        ;;
    restore)
        dir=${2:?usage: cache_npm.sh restore CACHE_DIR}
        mkdir -p "$dir"
        [ "${SIM_CACHE_MODE:-use}" != off ] || exit 0
        answer=$(cache restore --root "$ROOT" --dir "$dir") || answer=
        echo "cache: ${answer:-miss}"
        ;;
    save)
        dir=${2:?usage: cache_npm.sh save CACHE_DIR}
        [ "${SIM_CACHE_MODE:-use}" != off ] || exit 0
        answer=$(cache save --root "$ROOT" --dir "$dir") || answer=
        echo "cache: ${answer:-save skipped}"
        ;;
    *)
        echo "usage: cache_npm.sh tripwire|restore|save ..." >&2
        exit 2
        ;;
esac

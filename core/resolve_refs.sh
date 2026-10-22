#!/bin/sh
set -eu

refuse() {
    echo "::error::$1"
    exit 1
}

newline='
'

matches() {
    case $1 in *"$newline"*) return 1 ;; esac
    printf '%s\n' "$1" | grep -Eqx "$2"
}

on_a_branch() {
    [ -n "$(git for-each-ref --contains "$1" --format='%(refname)' refs/remotes/origin/)" ]
}

pr_branch=${PR_BRANCH:-}
pr_sha=${PR_SHA:-}
pr_number=${PR_NUMBER:-}
pr_author=${PR_AUTHOR:-}
retest_of=${RETEST_OF:-}
lanes=${LANES:-}
known_lanes=${KNOWN_LANES:-}
head_ref=${pr_sha:-$pr_branch}
base_ref=${BASE_REF:-}
default=${DEFAULT_BRANCH:-}

case $head_ref in
    '' | -* | refs/* | *..* | *[!A-Za-z0-9._/-]*) refuse "head must be a branch of this repository or a full commit SHA on one" ;;
esac
case $pr_branch in
    -* | refs/* | *..* | *[!A-Za-z0-9._/-]*) refuse "pr_branch must be a branch of this repository" ;;
esac
if [ -n "$pr_sha" ] && ! matches "$pr_sha" '[0-9a-f]{40}'; then
    refuse "pr_sha must be a full lowercase commit SHA"
fi
matches "$pr_number" '[0-9]{0,10}' || refuse "pr_number must be digits or empty"
matches "$pr_author" '([A-Za-z0-9-]{1,39}(\[bot\])?)?' || refuse "pr_author must be a GitHub login or empty"
matches "$retest_of" '[0-9]{0,20}' || refuse "retest_of must be a run id or empty"
case $base_ref in
    '' | -* | refs/* | *..* | *[!A-Za-z0-9._/~^-]*) refuse "base must name a commit on a branch of this repository" ;;
esac
case $default in
    '' | -* | *..* | *[!A-Za-z0-9._/-]*) refuse "the default branch name is not usable" ;;
esac
matches "$known_lanes" '[a-z0-9_]+( [a-z0-9_]+)*' || refuse "the workflow must name its lanes"
matches "$lanes" '([a-z0-9_]+(,[a-z0-9_]+)*)?' || refuse "lanes must be a comma-separated list of lane names"
for lane in $(printf '%s\n' "$lanes" | tr ',' ' '); do
    case " $known_lanes " in
        *" $lane "*) ;;
        *) refuse "lanes names a lane this stack does not have" ;;
    esac
done

if printf '%s\n' "$head_ref" | grep -Eqx '[0-9a-f]{40}'; then
    head_sha=$(git rev-parse --verify --quiet "$head_ref^{commit}") || refuse "head commit is not in this repository"
else
    head_sha=$(git rev-parse --verify --quiet "refs/remotes/origin/$head_ref^{commit}") || refuse "head is not a branch of this repository"
fi
base_sha=$(git rev-parse --verify --quiet "$base_ref^{commit}") || refuse "base is not in this repository"
on_a_branch "$head_sha" || refuse "head is not on a branch of this repository"
on_a_branch "$base_sha" || refuse "base is not on a branch of this repository"
if [ -n "$pr_sha" ] && [ -n "$pr_branch" ]; then
    branch_tip=$(git rev-parse --verify --quiet "refs/remotes/origin/$pr_branch^{commit}") || refuse "pr_branch is not a branch of this repository"
    git merge-base --is-ancestor "$head_sha" "$branch_tip" || refuse "pr_sha is not on pr_branch"
fi

tip=$(git rev-parse --verify --quiet "refs/remotes/origin/$default^{commit}" || true)
default_tip=false
if [ -n "$tip" ] && [ "$head_sha" = "$tip" ] && [ "${GITHUB_REF:-}" = "refs/heads/$default" ]; then
    default_tip=true
fi

{
    echo "pr_sha=$head_sha"
    echo "base_sha=$base_sha"
    echo "default_tip=$default_tip"
    echo "pr_number=$pr_number"
    echo "pr_branch=$pr_branch"
    echo "pr_author=$pr_author"
    echo "retest_of=$retest_of"
    for lane in $known_lanes; do
        case ",$lanes," in
            ,, | *",$lane,"*) echo "lane_$lane=true" ;;
            *) echo "lane_$lane=false" ;;
        esac
    done
} >> "${GITHUB_OUTPUT:?}"

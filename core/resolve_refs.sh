#!/bin/sh
set -eu

refuse() {
    echo "::error::$1"
    exit 1
}

on_a_branch() {
    [ -n "$(git for-each-ref --contains "$1" --format='%(refname)' refs/remotes/origin/)" ]
}

head_ref=${HEAD_REF:-}
base_ref=${BASE_REF:-}
default=${DEFAULT_BRANCH:-}

case $head_ref in
    '' | -* | refs/* | *..* | *[!A-Za-z0-9._/-]*) refuse "head must be a branch of this repository or a full commit SHA on one" ;;
esac
case $base_ref in
    '' | -* | refs/* | *..* | *[!A-Za-z0-9._/~^-]*) refuse "base must name a commit on a branch of this repository" ;;
esac
case $default in
    '' | -* | *..* | *[!A-Za-z0-9._/-]*) refuse "the default branch name is not usable" ;;
esac

if printf '%s\n' "$head_ref" | grep -Eqx '[0-9a-f]{40}'; then
    head_sha=$(git rev-parse --verify --quiet "$head_ref^{commit}") || refuse "head commit is not in this repository"
else
    head_sha=$(git rev-parse --verify --quiet "refs/remotes/origin/$head_ref^{commit}") || refuse "head is not a branch of this repository"
fi
base_sha=$(git rev-parse --verify --quiet "$base_ref^{commit}") || refuse "base is not in this repository"
on_a_branch "$head_sha" || refuse "head is not on a branch of this repository"
on_a_branch "$base_sha" || refuse "base is not on a branch of this repository"

tip=$(git rev-parse --verify --quiet "refs/remotes/origin/$default^{commit}" || true)
default_tip=false
if [ -n "$tip" ] && [ "$head_sha" = "$tip" ] && [ "${GITHUB_REF:-}" = "refs/heads/$default" ]; then
    default_tip=true
fi

{
    echo "head_sha=$head_sha"
    echo "base_sha=$base_sha"
    echo "default_tip=$default_tip"
} >> "${GITHUB_OUTPUT:?}"

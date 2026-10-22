#!/bin/sh
set -eu

ROOT=${SIM_ROOT:-sim/dotnet}

die() {
    echo "build_artifact: $*" >&2
    exit 1
}

in_tree() {
    case $1 in
        "$ROOT"/bin/* | "$ROOT"/obj/* | "$ROOT"/*/bin/* | "$ROOT"/*/obj/*) return 0 ;;
    esac
    return 1
}

mode=${1:-}
case $mode in
    pack)
        archive=${2:?usage: build_artifact.sh pack ARCHIVE}
        [ -d "$ROOT" ] || die "no directory $ROOT"
        find "$ROOT" -type d \( -name bin -o -name obj \) -prune -print0 | tar -cf - --null -T - > "$archive"
        echo "build_artifact: packed $(tar -tf - < "$archive" | wc -l | tr -d ' ') members"
        ;;
    unpack)
        archive=${2:?usage: build_artifact.sh unpack ARCHIVE}
        if [ -d "$ROOT" ]; then
            link=$(find "$ROOT" -type l -print | head -n 1)
            [ -z "$link" ] || die "refusing: the checkout holds a symlink $link"
        fi
        names=$(tar -tf - < "$archive") || die "unreadable archive"
        types=$(tar -tvf - < "$archive" | cut -c1) || die "unreadable archive"
        if [ -z "$names" ]; then
            echo "build_artifact: empty archive"
            exit 0
        fi
        if printf '%s\n' "$types" | grep -qv '^[-d]$'; then
            die "refusing: a link or special file"
        fi
        printf '%s\n' "$names" | while IFS= read -r name; do
            case $name in
                /*) die "refusing an absolute member $name" ;;
            esac
            case /$name/ in
                */../*) die "refusing a member with .. $name" ;;
            esac
            in_tree "$name" || die "refusing a member outside $ROOT bin and obj: $name"
        done || exit 1
        tar --no-same-owner -xf - < "$archive"
        echo "build_artifact: unpacked $(printf '%s\n' "$names" | wc -l | tr -d ' ') members"
        ;;
    *)
        echo "usage: build_artifact.sh pack|unpack ARCHIVE" >&2
        exit 2
        ;;
esac

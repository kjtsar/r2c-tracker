#!/bin/sh
# Read version and change notes from changes.txt, commit, and tag.
set -eu

CHANGES_FILE="$(dirname "$0")/changes.txt"

if [ ! -f "$CHANGES_FILE" ]; then
    echo "Error: $CHANGES_FILE not found." >&2
    exit 1
fi

# First non-empty line must be "vX.Y.Z:"
VERSION="$(grep -m1 '^v[0-9]' "$CHANGES_FILE" | sed 's/:$//')"
if [ -z "$VERSION" ]; then
    echo "Error: could not find a version line (e.g. 'v1.3.0:') in $CHANGES_FILE." >&2
    exit 1
fi

# Collect bullet lines under the version header
BULLETS="$(awk '/^'"$VERSION"':/{found=1; next} found && /^\*/{print} found && /^$/{next} found && !/^\*/{exit}' "$CHANGES_FILE")"
if [ -z "$BULLETS" ]; then
    echo "Error: no bullet points found under $VERSION in $CHANGES_FILE." >&2
    exit 1
fi

# Verify the tag doesn't already exist
if git -C "$(dirname "$0")" tag | grep -qx "$VERSION"; then
    echo "Error: tag $VERSION already exists." >&2
    exit 1
fi

COMMIT_MSG="$(printf '%s:\n%s' "$VERSION" "$BULLETS")"

echo "Version : $VERSION"
echo "Message :"
echo "$COMMIT_MSG"
echo ""

printf 'Proceed? [y/N] '
read -r REPLY
case "$REPLY" in
    [Yy]*) ;;
    *) echo "Aborted."; exit 0 ;;
esac

REPO="$(dirname "$0")"
git -C "$REPO" add -A
git -C "$REPO" commit -m "$COMMIT_MSG"
git -C "$REPO" tag "$VERSION"

echo ""
echo "Committed and tagged $VERSION."
echo "To push: git push && git push origin $VERSION"

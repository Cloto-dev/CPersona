#!/usr/bin/env bash
#
# Build this branch's documentation into a directory, at a given public URL.
#
# The toolchain pins live here rather than in the workflow because the version
# assembler runs this script inside a worktree of every line it publishes. A
# line therefore builds with the theme and plugin versions it shipped with, and
# bumping them on the development line cannot silently rebuild an older line
# with a theme it was never tested against.
#
# The site URL is passed in rather than read from mkdocs.yml because the same
# tree is built more than once: once under its own version path, and -- for the
# line currently served at the root -- once more at the root. Only site_url
# differs between those builds, and it is what canonical links and sitemap.xml
# are built from, so a copy would publish the wrong ones.
#
# Usage: scripts/build-docs.sh <output-dir> <site-url>

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <output-dir> <site-url>" >&2
  exit 2
fi

out=$1
site_url=$2

case "$site_url" in
  */) ;;
  *)
    # mkdocs joins site_url with page paths; without the trailing slash every
    # canonical link loses its last path segment. Failing here is cheaper than
    # finding it in the published HTML.
    echo "site-url must end with a slash: $site_url" >&2
    exit 2
    ;;
esac

MKDOCS_SITE_URL="$site_url" uvx \
  --with mkdocs-material==9.7.7 \
  --with mkdocs-static-i18n==1.3.1 \
  mkdocs build --strict --site-dir "$out"

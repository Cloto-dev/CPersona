#!/usr/bin/env bash
# Sync vendored mcp_common from mgp-py.
#
# Usage: ./scripts/sync-vendored-mcp-common.sh [path/to/mgp-py]
#
# Defaults to ../mgp-py (sibling checkout). The vendored copy lives at
# _vendored_mcp_common/ to keep cpersona installable from a tarball without
# any transitive git+url dependency (Sandbox install path).
set -euo pipefail

MGP_PY="${1:-../mgp-py}"
SRC="${MGP_PY}/packages/mcp-common/src/mcp_common/"
DST="./_vendored_mcp_common/"

if [ ! -d "$SRC" ]; then
  echo "Source not found: $SRC" >&2
  echo "Pass the path to your mgp-py checkout as the first argument." >&2
  exit 1
fi

rsync -a --delete --exclude '__pycache__' "$SRC" "$DST"

# Rewrite intra-package absolute imports so the vendored copy resolves under
# its renamed top-level package (_vendored_mcp_common) instead of the
# upstream `mcp_common` package, which is no longer installed.
find "$DST" -type f -name '*.py' -print0 | xargs -0 sed -i '' \
  -e 's|from mcp_common\.|from _vendored_mcp_common.|g' \
  -e 's|from mcp_common |from _vendored_mcp_common |g' \
  -e 's|import mcp_common\.|import _vendored_mcp_common.|g'

echo "Synced $SRC -> $DST (with import rewrite)"

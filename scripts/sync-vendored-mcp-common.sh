#!/usr/bin/env bash
# Sync vendored mcp_common from mgp-py.
#
# Usage: ./scripts/sync-vendored-mcp-common.sh [path/to/mgp-py]
#
# Defaults to ../mgp-py (sibling checkout). The vendored copy lives at
# cpersona/_vendored_mcp_common/ to keep cpersona installable from a tarball
# without any transitive git+url dependency (Sandbox install path).
#
# The three formerly cpersona-local patches are now UPSTREAM in mgp-py as of
# 2026-07-26 (mgp-py PR #11, commit a5b3fc1): bug-019 has_default arity in
# mcp_utils.py, and bug-103/bug-104 single-clock + action-id nulling in
# no_persist.py. Their marker strings were kept verbatim upstream so the gate
# below still recognises them. A sync from an mgp-py checkout at or after that
# commit is therefore no longer a downgrade for those three.
#
# CAUTION — this is still a blind `rsync -a --delete` mirror. Any future local
# edit to cpersona/_vendored_mcp_common/ is reverted by the next sync unless it
# is pushed upstream first, and a sync from an mgp-py checkout OLDER than
# a5b3fc1 reverts the three patches above. Note also that the vendored copy
# carries __version__ = "0.5.2" while upstream reads "0.5.1" (upstream's own
# pyproject says 0.6.0 — a pre-existing drift there), so a sync moves that
# string. The post-sync gate below runs verify-issues.sh + the pinned
# regression tests and fails this script non-zero if a patch was reverted, so
# a downgrade is caught here immediately instead of on the next CI push.
set -euo pipefail

MGP_PY="${1:-../mgp-py}"
SRC="${MGP_PY}/packages/mcp-common/src/mcp_common/"
DST="./cpersona/_vendored_mcp_common/"

if [ ! -d "$SRC" ]; then
  echo "Source not found: $SRC" >&2
  echo "Pass the path to your mgp-py checkout as the first argument." >&2
  exit 1
fi

rsync -a --delete --exclude '__pycache__' "$SRC" "$DST"

# Rewrite intra-package absolute imports so the vendored copy resolves under
# its renamed package path (cpersona._vendored_mcp_common) instead of the
# upstream `mcp_common` package, which is no longer installed.
find "$DST" -type f -name '*.py' -print0 | xargs -0 sed -i '' \
  -e 's|from mcp_common\.|from cpersona._vendored_mcp_common.|g' \
  -e 's|from mcp_common |from cpersona._vendored_mcp_common |g' \
  -e 's|import mcp_common\.|import cpersona._vendored_mcp_common.|g'

echo "Synced $SRC -> $DST (with import rewrite)"

# --- Post-sync reconciliation gate ------------------------------------------
# A blind mirror can revert the cpersona-local vendored patches (see CAUTION in
# the header). Catch that here with an immediate local signal instead of relying
# on CI to go red on the next push. verify-issues.sh greps each fix marker
# (bug-019/103/104) against the vendored files; the two regression tests pin the
# bug-019 / bug-104 behavior end-to-end.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Reconciling vendored patches (bug-019/103/104) against the registry + tests..."
if ! bash "$PROJECT_ROOT/scripts/verify-issues.sh" >/dev/null; then
  echo "ERROR: verify-issues.sh is RED after sync." >&2
  echo "  A pinned fix marker is missing — the mirror likely reverted a" >&2
  echo "  cpersona-local vendored patch (bug-019/103/104). Re-apply the patch to" >&2
  echo "  cpersona/_vendored_mcp_common/ AND push it upstream to mgp-py before" >&2
  echo "  committing this sync." >&2
  exit 1
fi

if command -v uv &>/dev/null; then
  if ! ( cd "$PROJECT_ROOT" && uv run pytest \
      tests/test_v2438_hardening.py::test_auto_tool_passes_explicit_none_default \
      tests/test_audit_2500b1.py::test_no_persist_skip_nulls_action_id_keys \
      -p no:cacheprovider -q ); then
    echo "ERROR: vendored-patch regression tests are RED after sync." >&2
    echo "  bug-019 / bug-104 behavior was reverted by the mirror. Reconcile the" >&2
    echo "  vendored copy before committing this sync." >&2
    exit 1
  fi
else
  echo "WARN: 'uv' not found; skipped the bug-019/bug-104 regression tests." >&2
  echo "      verify-issues.sh (marker grep) still passed. Run the suite manually." >&2
fi

echo "Vendored-patch reconciliation passed."

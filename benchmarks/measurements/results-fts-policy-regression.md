# FTS policy: full regression qualification

Measured 2026-09-15. Value: qualify the natural-language accuracy recovery
candidate against the existing 2.5-line regression suite. This is a local
qualification record, not release acceptance or an exhaustive feature proof.
The [completed accuracy exploration](results-fts-policy-exploration.md) is a
separate result; no unchanged accuracy benchmark was rerun here.

## Source and environment boundaries

Two candidate bases were exercised: the measurement endpoint `e43ad34`, and
tag `v2.5.12b4` (`4f08d87`). The latter adds shared identity/ACL changes and
two regression tests beyond the former. Both carry the same experimental
edge-delimiter policy and the same 24 additional retrieval cases.
Only the policy helper and two FTS reader call sites change production code.
The original literal compiler, existing assertions, and golden are preserved.

The host is macOS arm64. The Python 3.11.15 environment uses SQLite 3.50.4;
the Python 3.13.13 environment uses SQLite 3.53.1. The latter was created in
the isolated b4 worktree using `uv sync --frozen --dev --python 3.13`.
These cover both Python versions in the repository's CI matrix, not the
Linux CI host or every SQLite build.

## Results

Full `pytest -q` collection, without deselection or new skips:

| Candidate | Python | Passed | Failed | Existing skips |
| --- | --- | ---: | ---: | ---: |
| `e43ad34` + `edges` | 3.11.15 | 2,411 | 0 | 5 |
| `v2.5.12b4` + `edges`, original fixture | 3.11.15 | 2,413 | 0 | 5 |
| `v2.5.12b4` + `edges`, original fixture | 3.13.13 | 2,412 | 1 | 5 |
| `v2.5.12b4` + `edges`, repaired fixture | 3.11.15 | 2,413 | 0 | 5 |
| `v2.5.12b4` + `edges`, repaired fixture | 3.13.13 | 2,413 | 0 | 5 |

The b4 runs collect 2,418 cases across 140 test modules. The five existing
skips belong to queue-side synthesis paths removed before this change; they
are not five newly skipped failures. Exact case IDs, times and original
JUnit hashes are retained in [the machine-readable record](fts-policy-regression-results.json).

Other checks:

- CI-pinned `ruff@0.15.21 check .`: passes on both candidates.
- `bash scripts/verify-issues.sh`: both candidates report 433 entries,
  401 verified, 32 fixed, 0 stale, 0 errors. This is registry consistency,
  not independent behavioral proof for every entry.
- `scripts/capture-behaviour.py --check`: **93 scenarios unchanged** on
  each candidate; the same 93 golden scenarios are part of full pytest.
- b4 policy mutation: bypass the helper in both FTS readers; the additional
  retrieval tests fail **20 cases, with 4 passing**, at the missing-target
  assertion. Restore the call sites: **29 pass**, including existing bug-215
  tests. No original assertion or golden value was loosened.

## Executed coverage, not an exhaustive feature inventory

These are examples of existing modules actually collected and executed in the
full runs, rather than a claim inferred from their presence on disk:

| Surface | Representative executed modules |
| --- | --- |
| Identity and authorization | `test_acl`, `test_acl_transport`, `test_per_subject`, `test_shared_identity` (b4) |
| Project/channel/session isolation | `test_isolation`, `test_isolation_where`, `test_session_identity`, `test_257_session_key_stage2`, `test_operating_context` |
| Store, recall, FTS, vector fusion | `test_store_recall_surface`, `test_embedding_path`, `test_recall_quality`, `test_do_recall_response`, `test_bug215_fts_punctuation`, `test_fts_query_policy` |
| Index, scanning, calibration | `test_vector_index_read_path`, `test_vector_index_episodes`, `test_scan_window`, `test_chunked_exact_scan`, `test_threshold_calibration` |
| MCP and OAuth | `test_mcp_dispatch`, `test_oauth_discovery`, `test_oauth_verification` |
| Schema, merge, maintenance | `test_schema_v9_migration`, `test_channel_axis_migration`, `test_index_merge_paths`, `test_health_status`, `test_2512b2_handlers` |
| Refactor behavior | `test_refactor_seams_252`, `test_equivalence_252` |

Embedding-path integration uses the existing deterministic fake client; it
does not demonstrate equivalent accuracy with every embedding model.

## Failure attribution

### Initial environment setup: resolved without changing tests

The first candidate run reported 14 failures. An unchanged `e43ad34` control
reported the exact same 14 failing case IDs. Those runs placed the test DB
directly under `/private/tmp`, exposing a real directory-permissions finding
to health/golden tests and exercising the permission code's system-directory
boundary. The sandbox also denied the OAuth deadline test's loopback listener.

Re-running the candidate with a private `mktemp -d` directory and permission
for the test's loopback listener cleared all 14, with no source/assertion
changes. Original logs and databases were retained. Do not interpret the
first run as an FTS regression or suppress its permission assertions.

### Python 3.13 / SQLite fixture portability: diagnosis and repair

Before the fixture repair,
`test_short_content_repair_survives_a_corpus_past_the_variable_ceiling` failed
at the then-current `tests/test_2512b2_handlers.py:1699`: expected 250,001,
observed 100,000.
The test sets its row count to SQLite's native bind-variable ceiling plus one
and uses `str(i)` as content. With ceiling 250,000, strings from `100000`
onward contain six characters. The production predicate correctly selects
only content of at most five characters, hence exactly 100,000 eligible rows.
With ceiling 32,766 in the Python 3.11 environment, every seeded row is short.

The same test failure reproduces on **unchanged `e43ad34` with the 3.13
interpreter**. This is a demonstrated fixture portability defect, not evidence
that the FTS policy broke repair.

The repair keeps `count = native_ceiling + 1`, fixes every content to the
five-character string `short`, and uses the channel field for dedup uniqueness.
The repair under test scans all channels of the same agent, so all rows still
belong to the same repair invocation. New fixture assertions verify the exact
seeded count exceeds the native ceiling and both minimum and maximum trimmed
content length equal five. All existing count/fixed/remaining assertions stay
unchanged; no native limit, production predicate, or skip was altered.

The repaired focused test passes with 250,001 rows on Python 3.13. Mutation
proof sets `_SHORT_CONTENT_DELETE_CHUNK` from 500 to `2**31`, effectively
removing the split. The exact test then fails with **`sqlite3.OperationalError:
too many SQL variables`**, not a fixture assertion or setup error. Restoring
500 makes the test pass again. `cpersona/checks.py` is byte-for-byte restored
to its original SHA-256
`e8246adc688923b8ab735b1faefdf227712b1aac3e6955b00bbc9325e2a1ec4a`.

## Acceptance status and remaining work

After the fixture repair, **both Python 3.11 and 3.13 full suites pass**:
2,413 passed, five unchanged skips, zero failures on each. The runs took
71.82 and 88.56 seconds respectively. CI-pinned lint, issue-registry checks,
and the 93-scenario golden check also pass after the repair.
No candidate-induced regression was detected by these runs. This clears the
local full-suite gate, but does not prove every possible 2.5 behavior or
resolve the candidate's previously documented ambiguous punctuation,
short-token, and top-rank acceptance questions.

Not performed: a complete CI run on Linux, broad mutation proof for all
historical seams, wheel/container/client deployment acceptance, production
data validation, cross-model accuracy, or release-line selection. Prior
accuracy results remain attributed to their measured e43-based candidate;
they were not remeasured on b4. No commit, push, version bump, merge,
deployment, or release had been performed when this record was written. The
main checkout was not modified.

Raw logs and JUnit reports were kept on the measuring machine and are not
committed; the machine-readable summary records their hashes.

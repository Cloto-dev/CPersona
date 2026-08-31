"""SuperAuditor delivery seam for CPersona (the standard's second implementation).

``docs/SUPERAUDITOR_STANDARD.md`` v1 specifies how a server *reports* the
findings it already computes about its own stored state — the seam — and
deliberately says nothing about what a server detects. This module is that
seam and nothing else: it turns the issues ``cpersona.checks`` already produces
for ``check_health`` into the finding objects the pull tool
``get_session_findings`` delivers. One detector (standard §5.3): every finding
here came out of ``checks.run_health_checks``; nothing is detected twice.

What the seam adds to an issue is the two keys the standard defines — ``kind``
and ``severity`` — and honest caps. Everything else on the issue is passed
through verbatim as payload (``type``, ``check``, counts, ids, ``repairable``,
hints), because those are what a consumer acts on.

Kind vocabulary
---------------
A finding's ``kind`` is the registry name of the check that produced it,
which is also the name a caller passes to ``check_health(checks=[...])`` to
re-run exactly that probe. The standard requires severity to be a property
of the kind, assigned from a static map — no per-finding scoring — and
``check_health`` does not work that way: a few runners escalate their own
severity by a deterministic numeric rule (``null_embedding`` is info when
NULL is the configured steady state, warn when a client is configured, and
critical when more than half the rows are NULL), and the ``repairable``
contract de-escalates a warn whose repair cannot write a single row. Rather
than let one kind carry three severities, the escalation *tiers* are distinct
kinds — ``null_embedding_expected`` / ``null_embedding`` /
``null_embedding_pipeline_down`` — so a consumer routing on severity sees the
pipeline-down case as the critical it is, and the map stays static.

The de-escalation is not a tier. It exists so a gate (``check_health``'s
``status``) is not held down forever by a finding nobody can repair; this
channel is not a gate, and a contradiction that cannot be auto-repaired is
still a contradiction (the standard's ``warn``). The finding keeps the static
severity of its kind and carries ``check_health``'s own instance verdict as
``health_severity`` alongside ``needs_human_review``, so nothing is lost.

``check_crashed`` is the one kind that is not a probe: ``run_health_checks``
emits it when a runner raises, in place of that runner's findings. It is how
this implementation distinguishes "no findings" from "a probe failed" — the
response is a partial result and says so per kind, rather than failing the
whole pull.
"""

from cpersona.checks import HEALTH_CHECKS, SEVERITIES

# Standard §4 rule 2: if a fallback exists it MUST be the weakest severity.
FALLBACK_SEVERITY = "info"

DEFAULT_PER_KIND_LIMIT = 5

# Checks whose runner emits more than one severity, and how each tier is named.
# A runner not listed here emits exactly one severity for every finding — the
# registry default, or (fts_integrity / sqlite_integrity) one explicit value —
# and tests/test_superauditor_findings.py pins that inventory against the
# source, so a new escalation rule cannot appear without a tier entry.
_TIERED_CHECKS = frozenset(
    {"null_embedding", "null_episode_embedding", "schema_objects", "vector_index"}
)

_TIER_KINDS: dict[str, str] = {
    "null_embedding_expected": "info",
    "null_embedding_pipeline_down": "critical",
    "null_episode_embedding_expected": "info",
    "null_episode_embedding_pipeline_down": "critical",
    # schema_objects: the registry default (critical) covers the objects whose
    # loss breaks a data guarantee (dedup uniqueness, FTS sync); the
    # performance / scoping indexes are declared warn on their own spec.
    "schema_objects_perf_index": "warn",
    # vector_index: the registry default (info) covers the states that are not
    # defects — no index built yet, or a tail that has grown past the rebuild
    # threshold. The two the runner stamps warn are the ones where an index
    # exists and has stopped being used: reads stay correct either way, so
    # neither is critical, but silence is how they went unnoticed for a week.
    "vector_index_degraded": "warn",
    # Not a probe — a probe that could not run (see module docstring).
    "check_crashed": "warn",
}

# Registry defaults that the runner never actually uses: every finding
# check_fts_integrity emits is the FTS5 integrity-check *failing*, which it
# stamps critical itself (the warn default dates from a row-count comparison
# that 2.4.37 retired). The map states what is delivered, not what is defaulted.
_EXPLICIT_KINDS: dict[str, str] = {
    "fts_integrity": "critical",
}

FINDING_SEVERITY: dict[str, str] = {
    **{check.name: check.base_severity for check in HEALTH_CHECKS},
    **_EXPLICIT_KINDS,
    **_TIER_KINDS,
}
assert all(severity in SEVERITIES for severity in FINDING_SEVERITY.values())


def severity_for_kind(kind: str) -> str:
    """Static per-kind severity (standard §4 rule 1), ``info`` when unmapped (rule 2)."""
    return FINDING_SEVERITY.get(kind, FALLBACK_SEVERITY)


def finding_kind(issue: dict) -> str:
    """The kind a ``check_health`` issue delivers under.

    Deterministic in the issue's own fields: the registry name, plus — for the
    tiered checks — which tier the runner's escalation rule landed on, read
    off the severity the runner stamped. Nothing here scores anything; it
    names the branch the detector already took.
    """
    if issue.get("type") == "check_crashed":
        return "check_crashed"
    check = issue.get("check") or issue.get("type") or ""
    if check not in _TIERED_CHECKS:
        return check
    stamped = issue.get("severity")
    if check in ("null_embedding", "null_episode_embedding"):
        # _null_embedding_severity: info = NULL is the configured steady state
        # (no client, or no local BLOB stored), critical = more than
        # NULL_EMBEDDING_CRITICAL_RATIO of the rows are NULL, warn otherwise.
        # A de-escalation cannot reach here: repairable is zero exactly when
        # the runner already said info.
        if stamped == "critical":
            return f"{check}_pipeline_down"
        if stamped == "info":
            return f"{check}_expected"
        return check
    if check == "vector_index":
        # warn is the runner's stamp for "an index exists and is not being
        # used"; the unstamped states are observations, not defects.
        return "vector_index_degraded" if stamped == "warn" else "vector_index"
    # schema_objects: critical is the runner's own stamp for a guarantee-bearing
    # object; anything else is a performance index (warn, or info once the
    # repairable policy de-escalated it).
    return "schema_objects" if stamped == "critical" else "schema_objects_perf_index"


# The names this seam moves a payload key to. A probe that ever emits one of
# these itself would have its value silently replaced by the relocated one, so
# the collision is refused instead. Checked at the seam rather than only in a
# test, because the failure it guards is invisible: the response stays
# well-formed and one field means something other than it says.
RELOCATION_TARGETS = ("object_kind", "health_severity")


class ReservedKeyCollision(Exception):
    """A probe emitted a key this seam relocates into. See RELOCATION_TARGETS."""


def as_finding(issue: dict) -> dict:
    """A ``check_health`` issue as the finding this seam delivers it as.

    The payload is the issue verbatim, with two keys moved out of the way of
    the standard's own: the runner's ``severity`` becomes ``health_severity``
    (see the module docstring), and an issue's own ``kind`` — ``schema_objects``
    uses it for the object type, ``index`` / ``trigger`` — becomes
    ``object_kind``, because ``kind`` names the probe here.

    The relocation is refused rather than performed if the issue already
    carries the destination name. No probe does today; one that started would
    otherwise have its own field overwritten by the moved one, and nothing
    would look wrong — the standard reserves ``kind`` and ``severity`` but says
    nothing about where an implementation may move a colliding payload key to,
    so this end of the rule is ours to hold.
    """
    for target in RELOCATION_TARGETS:
        if target in issue:
            raise ReservedKeyCollision(
                f"issue from check {issue.get('check') or issue.get('type')!r} carries "
                f"{target!r}, which this seam relocates a payload key into; rename the "
                f"probe's field (the delivered finding would otherwise silently lose one)"
            )
    kind = finding_kind(issue)
    finding = {key: value for key, value in issue.items() if key not in ("kind", "severity")}
    if "kind" in issue:
        finding["object_kind"] = issue["kind"]
    if "severity" in issue:
        finding["health_severity"] = issue["severity"]
    finding["kind"] = kind
    finding["severity"] = severity_for_kind(kind)
    return finding


def deliver(detector_output: list[dict], per_kind_limit: int, severity_map: dict | None = None) -> dict:
    """The reference delivery transform (standard §5.2, §6).

    Keeps the first ``per_kind_limit`` findings of each kind in detector order,
    names in ``capped_kinds`` every kind that had more (observed from the
    overflow row, never inferred from ``count == limit``), and derives the
    counts from what is actually returned.

    ``detector_output`` entries already carry their ``kind``; ``severity`` is
    assigned here from ``severity_map`` — the static registry map by default.
    The conformance fixtures inject their own map, with opaque kinds, to prove
    the transform does not depend on this server's vocabulary. Production
    goes through ``deliver_issues``.
    """
    kept: list[dict] = []
    seen: dict[str, int] = {}
    capped: list[str] = []
    for entry in detector_output:
        kind = entry["kind"]
        n = seen.get(kind, 0)
        seen[kind] = n + 1
        if n >= per_kind_limit:
            if kind not in capped:
                capped.append(kind)
            continue
        if severity_map is None:
            severity = severity_for_kind(kind)
        else:
            severity = severity_map.get(kind, FALLBACK_SEVERITY)
        kept.append({**entry, "severity": severity})

    counts_by_kind: dict[str, int] = {}
    counts_by_severity: dict[str, int] = {}
    for finding in kept:
        counts_by_kind[finding["kind"]] = counts_by_kind.get(finding["kind"], 0) + 1
        counts_by_severity[finding["severity"]] = counts_by_severity.get(finding["severity"], 0) + 1
    return {
        "findings": kept,
        "total": len(kept),
        "counts_by_kind": counts_by_kind,
        "counts_by_severity": counts_by_severity,
        "capped_kinds": capped,
        "per_kind_limit": per_kind_limit,
    }


def deliver_issues(issues: list[dict], per_kind_limit: int) -> dict:
    """``check_health`` issues, delivered: ``as_finding`` then ``deliver``."""
    return deliver([as_finding(issue) for issue in issues], per_kind_limit)


def render_summary(delivered: dict) -> str:
    """Prose restatement of a delivered set — rendered from the trimmed set, so
    the sentence and the structure cannot disagree (standard §5.2)."""
    if not delivered["findings"]:
        return "Storage findings: none."
    by_severity = ", ".join(
        f"{severity} {delivered['counts_by_severity'][severity]}"
        for severity in reversed(SEVERITIES)
        if severity in delivered["counts_by_severity"]
    )
    by_kind = ", ".join(f"{kind} {count}" for kind, count in delivered["counts_by_kind"].items())
    text = f"Storage findings: {delivered['total']} returned ({by_severity}); kinds: {by_kind}."
    if delivered["capped_kinds"]:
        text += (
            f" Capped at {delivered['per_kind_limit']} per kind: "
            f"{', '.join(delivered['capped_kinds'])} — more exist than were returned."
        )
    return text

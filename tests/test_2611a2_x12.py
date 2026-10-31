"""Regression tests for configuration parsing, owned files and finding severity in 2.6.11a2.

  bug-654  CPERSONA_MAX_MEMORIES was the one scan-window setting parsed without a
           bound. A negative value (SQLite's "no limit", which an operator may set
           to mean "scan everything") reached the coarse supplier as a scan position
           it refuses, so every time-cued recall raised, and it switched the far
           list on at the default reach of 0.
  bug-655  the reconstruct budget and tail-quote defaults were fixed numbers, so
           raising only CPERSONA_RECALL_PREVIEW_CHARS, or lowering only the maximum
           budget or the quote size, stopped startup over a default nobody set.
  bug-656  boolean flags were read as `value.lower() == "true"`, so `1`, `yes` and
           `on` turned a default-on feature off with no warning.
  bug-661  fileperms.owned_paths() left out the coarse index and block index
           files, so check_file_permissions could neither report nor narrow them.
  bug-662  the delivery kind embedding_backend_misconfigured had no static
           severity, so a state check_health stamps warn was delivered as info.
"""

import importlib
import inspect
import os
import re
import stat
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x12.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from cpersona import (  # noqa: E402
    block_index,
    checks,
    coarse_index,
    config,
    fileperms,
    findings,
    session,
)
from cpersona.database import get_db  # noqa: E402

# Every variable a test below sets, cleared first so that a value exported in the
# shell running pytest cannot decide what "unset" means here.
_BUDGET_ENV = (
    "CPERSONA_RECALL_PREVIEW_CHARS",
    "CPERSONA_RECONSTRUCT_DEFAULT_BUDGET",
    "CPERSONA_RECONSTRUCT_MAX_BUDGET",
    "CPERSONA_RECONSTRUCT_FORCED_BUDGET",
    "CPERSONA_RECONSTRUCT_QUOTE_CHARS",
    "CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS",
)


def _reload(monkeypatch, **env):
    """Re-read config with *env* set (a value of None unsets the variable)."""
    with monkeypatch.context() as m:
        for name in _BUDGET_ENV:
            m.delenv(name, raising=False)
        for name, value in env.items():
            if value is None:
                m.delenv(name, raising=False)
            else:
                m.setenv(name, value)
        return importlib.reload(config)


@pytest.fixture
def fresh_config():
    yield
    importlib.reload(config)


def _warned(caplog, name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and name in r.getMessage()]


# ---------------------------------------------------------------------------
# bug-654 -- a scan window below one row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["-1", "-50000", "0"])
def test_a_scan_window_below_one_row_falls_back_and_says_so(monkeypatch, fresh_config, caplog, raw):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_MAX_MEMORIES=raw, CPERSONA_VECTOR_REACH=None)
    assert cfg.MAX_MEMORIES == 10000, "a scan window that cannot be a scan position was accepted"
    assert _warned(caplog, "CPERSONA_MAX_MEMORIES"), "the value was replaced without a word"
    # The far list exists only when the reach exceeds the window. At the default
    # reach of 0 it must stay off, which a negative window turned on.
    assert not cfg.VECTOR_REACH > cfg.MAX_MEMORIES


def test_the_smallest_real_scan_window_is_taken_as_written(monkeypatch, fresh_config, caplog):
    """The boundary: one row is a window, and is not replaced."""
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_MAX_MEMORIES="1")
    assert cfg.MAX_MEMORIES == 1
    assert not _warned(caplog, "CPERSONA_MAX_MEMORIES")


# ---------------------------------------------------------------------------
# bug-655 -- unset budget and tail defaults follow the settings they must fit
# ---------------------------------------------------------------------------


def test_nothing_set_keeps_the_shipped_budget_and_tail(monkeypatch, fresh_config):
    cfg = _reload(monkeypatch)
    assert (cfg.RECONSTRUCT_DEFAULT_BUDGET, cfg.RECONSTRUCT_MAX_BUDGET) == (4000, 20000)
    assert (cfg.RECONSTRUCT_QUOTE_CHARS, cfg.RECONSTRUCT_TAIL_QUOTE_CHARS) == (800, 400)
    cfg.validate_reconstruct_counts()


def test_raising_only_the_preview_tier_still_starts(monkeypatch, fresh_config):
    """A 2.5.x deployment with a larger preview, upgraded and otherwise untouched."""
    cfg = _reload(monkeypatch, CPERSONA_RECALL_PREVIEW_CHARS="5000")
    cfg.validate_reconstruct_counts()  # must not refuse a default nobody set
    assert cfg.RECONSTRUCT_DEFAULT_BUDGET == 5000, "the unset default must rise to one preview excerpt"
    assert cfg.RECONSTRUCT_MAX_BUDGET == 20000


def test_a_preview_tier_above_the_maximum_raises_the_unset_maximum(monkeypatch, fresh_config):
    cfg = _reload(monkeypatch, CPERSONA_RECALL_PREVIEW_CHARS="30000")
    cfg.validate_reconstruct_counts()
    assert cfg.RECONSTRUCT_MAX_BUDGET == 30000
    assert cfg.RECONSTRUCT_DEFAULT_BUDGET == 30000


def test_lowering_only_the_maximum_budget_still_starts(monkeypatch, fresh_config):
    cfg = _reload(monkeypatch, CPERSONA_RECONSTRUCT_MAX_BUDGET="3000")
    cfg.validate_reconstruct_counts()
    assert cfg.RECONSTRUCT_DEFAULT_BUDGET == 3000, "the unset default must follow the maximum down"


def test_lowering_only_the_quote_size_still_starts(monkeypatch, fresh_config):
    """A 2.6.0-2.6.3 configuration: there was no tail setting to set."""
    cfg = _reload(monkeypatch, CPERSONA_RECONSTRUCT_QUOTE_CHARS="300")
    cfg.validate_reconstruct_counts()
    assert cfg.RECONSTRUCT_TAIL_QUOTE_CHARS == 300, "the unset tail must follow the quote size down"


def test_a_malformed_budget_default_follows_too(monkeypatch, fresh_config):
    cfg = _reload(
        monkeypatch,
        CPERSONA_RECONSTRUCT_MAX_BUDGET="3000",
        CPERSONA_RECONSTRUCT_DEFAULT_BUDGET="4k",
    )
    assert cfg.RECONSTRUCT_DEFAULT_BUDGET == 3000
    cfg.validate_reconstruct_counts()


@pytest.mark.parametrize(
    "env, name",
    [
        (
            {"CPERSONA_RECALL_PREVIEW_CHARS": "5000", "CPERSONA_RECONSTRUCT_DEFAULT_BUDGET": "4000"},
            "CPERSONA_RECONSTRUCT_DEFAULT_BUDGET",
        ),
        (
            {"CPERSONA_RECALL_PREVIEW_CHARS": "5000", "CPERSONA_RECONSTRUCT_MAX_BUDGET": "4000"},
            "CPERSONA_RECONSTRUCT_MAX_BUDGET",
        ),
        (
            {"CPERSONA_RECONSTRUCT_MAX_BUDGET": "3000", "CPERSONA_RECONSTRUCT_DEFAULT_BUDGET": "4000"},
            "CPERSONA_RECONSTRUCT_DEFAULT_BUDGET",
        ),
        (
            {"CPERSONA_RECONSTRUCT_QUOTE_CHARS": "300", "CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS": "400"},
            "CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS",
        ),
    ],
)
def test_a_value_set_explicitly_is_still_refused(monkeypatch, fresh_config, env, name):
    """Only the defaults move. A value the operator wrote is taken as written."""
    cfg = _reload(monkeypatch, **env)
    with pytest.raises(ValueError, match=name):
        cfg.validate_reconstruct_counts()


# ---------------------------------------------------------------------------
# bug-656 -- boolean flags
# ---------------------------------------------------------------------------

# (variable, attribute, documented default) for every on/off flag config.py reads.
_FLAGS = [
    ("CPERSONA_RECALL_PROPAGATION_SEAT", "RECALL_PROPAGATION_SEAT", False),
    ("CPERSONA_FAR_SEATS_ENABLED", "FAR_SEATS_ENABLED", False),
    ("CPERSONA_FTS_ENABLED", "FTS_ENABLED", True),
    ("CPERSONA_LOGICAL_DIRECTORY", "LOGICAL_DIRECTORY", False),
    ("CPERSONA_DECLARED_FOLDERS", "DECLARED_FOLDERS", False),
    ("CPERSONA_DASHBOARD", "DASHBOARD", True),
    ("CPERSONA_DASHBOARD_REMOTE", "DASHBOARD_REMOTE", False),
    ("CPERSONA_DASHBOARD_STORES", "DASHBOARD_STORES", True),
    ("CPERSONA_DEGRADED_ADVISORY", "DEGRADED_ADVISORY_ENABLED", True),
    ("CPERSONA_TASK_QUEUE_ENABLED", "TASK_QUEUE_ENABLED", True),
    ("CPERSONA_UPDATE_CHECK", "UPDATE_CHECK_ENABLED", True),
    ("CPERSONA_SCOPE_STATS_CACHE", "SCOPE_STATS_CACHE_ENABLED", True),
    ("CPERSONA_CONFIDENCE_ENABLED", "CONFIDENCE_ENABLED", False),
    ("CPERSONA_BLOCK_BUILD_ENABLED", "BLOCK_BUILD_ENABLED", True),
    ("CPERSONA_BLOCK_RETRIEVAL_ENABLED", "BLOCK_RETRIEVAL_ENABLED", True),
    ("CPERSONA_BLOCK_INDEX", "BLOCK_INDEX_ENABLED", True),
    ("CPERSONA_STORE_BLOB", "STORE_BLOB", True),
    ("CPERSONA_AUTO_CALIBRATE", "AUTO_CALIBRATE", False),
    ("CPERSONA_CALIBRATE_ON_MODEL_CHANGE", "CALIBRATE_ON_MODEL_CHANGE", True),
    ("CPERSONA_FUSED_GATE_ENABLED", "FUSED_GATE_ENABLED", True),
    ("CPERSONA_AUTOCUT_ENABLED", "AUTOCUT_ENABLED", True),
    ("CPERSONA_EPISODE_PENALTY_ENABLED", "EPISODE_PENALTY_ENABLED", False),
]
_FLAG_IDS = [attr for _, attr, _ in _FLAGS]


def test_the_flag_list_is_every_flag_config_reads():
    """The list below would otherwise go stale the day a flag is added."""
    source = inspect.getsource(config)
    read = set(re.findall(r'_parse_bool\(\s*"([A-Z_]+)"', source))
    read |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)",\s*"(?:true|false)"\s*\)\.lower\(\) == "true"', source))
    assert read == {var for var, _, _ in _FLAGS}


@pytest.mark.parametrize("var, attr, default", _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("raw", ["True", "1", "yes", " ON "])
def test_the_usual_spellings_of_true_are_true(monkeypatch, fresh_config, var, attr, default, raw):
    cfg = _reload(monkeypatch, **{var: raw})
    assert getattr(cfg, attr) is True, f"{var}={raw!r} was read as off"


@pytest.mark.parametrize("var, attr, default", _FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("raw", ["FALSE", "0", "no", "off"])
def test_the_usual_spellings_of_false_are_false(monkeypatch, fresh_config, var, attr, default, raw):
    cfg = _reload(monkeypatch, **{var: raw})
    assert getattr(cfg, attr) is False, f"{var}={raw!r} was read as on"


# The switches an unreadable value turns off rather than leaving at the default:
# on, they open an outbound connection (tests/test_2611a2_x17.py).
_OFF_WHEN_UNREADABLE = {"CPERSONA_UPDATE_CHECK"}


@pytest.mark.parametrize("var, attr, default", _FLAGS, ids=_FLAG_IDS)
def test_an_unrecognised_value_keeps_the_default_and_warns(monkeypatch, fresh_config, caplog, var, attr, default):
    expected = False if var in _OFF_WHEN_UNREADABLE else default
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, **{var: "enabled"})
    assert getattr(cfg, attr) is expected, f"{var}=enabled did not read as {expected}"
    assert _warned(caplog, var), f"{var}=enabled was read without a warning"


@pytest.mark.parametrize("var, attr, default", _FLAGS, ids=_FLAG_IDS)
def test_unset_and_empty_are_the_default(monkeypatch, fresh_config, var, attr, default):
    assert getattr(_reload(monkeypatch, **{var: None}), attr) is default
    assert getattr(_reload(monkeypatch, **{var: ""}), attr) is default


def test_the_block_reader_still_follows_construction_when_unset(monkeypatch, fresh_config):
    cfg = _reload(monkeypatch, CPERSONA_BLOCK_BUILD_ENABLED="0", CPERSONA_BLOCK_RETRIEVAL_ENABLED=None)
    assert (cfg.BLOCK_BUILD_ENABLED, cfg.BLOCK_RETRIEVAL_ENABLED) == (False, False)
    cfg.validate_block_gates()


def test_an_unrecognised_cue_mode_stays_off_and_warns(monkeypatch, fresh_config, caplog):
    """The cue mode is a word setting whose unknown values are off by contract
    (only `true` reads the live store); what was missing was saying so."""
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_CUE_COARSE_ENABLED="1")
    assert cfg.CUE_COARSE_MODE == "off"
    assert _warned(caplog, "CPERSONA_CUE_COARSE_ENABLED")


@pytest.mark.parametrize("raw, mode", [("", "auto"), ("auto", "auto"), ("true", "on"), ("false", "off")])
def test_a_recognised_cue_mode_is_read_without_a_warning(monkeypatch, fresh_config, caplog, raw, mode):
    with caplog.at_level("WARNING"):
        cfg = _reload(monkeypatch, CPERSONA_CUE_COARSE_ENABLED=raw)
    assert cfg.CUE_COARSE_MODE == mode
    assert not _warned(caplog, "CPERSONA_CUE_COARSE_ENABLED")


# ---------------------------------------------------------------------------
# bug-661 -- the coarse and block index files are owned paths
# ---------------------------------------------------------------------------

pytestmark_posix = pytest.mark.skipif(
    not fileperms.mode_bits_are_enforced(),
    reason="permission bits are not the access control on this platform",
)


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    return await get_db()


def _index_files() -> list[str]:
    return [
        coarse_index.index_path("memories"),
        coarse_index.index_path("episodes"),
        block_index.index_path(),
    ]


def test_owned_paths_names_the_coarse_and_block_index_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "corpus.db"))
    owned = {p.path: p for p in fileperms.owned_paths()}
    for path in _index_files():
        assert path in owned, f"{os.path.basename(path)} is placed by this package and not listed"
        assert owned[path].kind == "file"


@pytestmark_posix
@pytest.mark.asyncio
async def test_a_readable_index_file_is_reported_and_narrowed(db, tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.setattr(config, "DB_PATH", str(root / "corpus.db"))
    os.chmod(root, 0o700)
    targets = _index_files()
    for path in targets:
        with open(path, "wb") as fh:
            fh.write(b"\0")
        os.chmod(path, 0o644)

    issues, _ = await checks.run_health_checks(db, fix=True, checks=["file_permissions"])
    reported = {os.path.basename(i["path"]) for i in issues}
    assert reported == {os.path.basename(p) for p in targets}
    for path in targets:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, f"{os.path.basename(path)} was left readable"


# ---------------------------------------------------------------------------
# bug-662 -- every kind a tiered check can deliver has a static severity
# ---------------------------------------------------------------------------


def _reachable_tier_kinds() -> set[str]:
    """Every kind finding_kind can mint for a tiered check, from the issue types
    the checks module actually spells and every severity a runner can stamp."""
    source = inspect.getsource(checks)
    kinds = set()
    for check in findings._TIERED_CHECKS:
        types = {None, check, *re.findall(rf'"({re.escape(check)}_[a-z_]+)"', source)}
        for issue_type in types:
            for stamped in (*checks.SEVERITIES, None):
                issue = {"check": check, "severity": stamped}
                if issue_type is not None:
                    issue["type"] = issue_type
                kinds.add(findings.finding_kind(issue))
    return kinds


def test_every_kind_a_tiered_check_delivers_has_a_static_severity():
    kinds = _reachable_tier_kinds()
    assert "embedding_backend_misconfigured" in kinds  # the inventory reaches the case
    missing = sorted(k for k in kinds if k not in findings.FINDING_SEVERITY)
    assert not missing, f"kinds delivered at the fallback severity: {missing}"


def test_a_misconfigured_backend_is_delivered_as_warn_like_its_twin():
    for issue_type in ("embedding_backend_misconfigured", "embedding_backend_unreachable"):
        finding = findings.as_finding({"check": "embedding_backend", "type": issue_type, "severity": "warn"})
        assert finding["kind"] == issue_type
        assert finding["severity"] == "warn", f"{issue_type} delivered as {finding['severity']}"
        assert finding["health_severity"] == "warn"

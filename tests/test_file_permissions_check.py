"""``check_file_permissions``: the residual of the pre-creation fix (bug-289).

Pre-creation settles a file this version made. An install that upgraded keeps
the 0644 files it already had -- deliberately, because an existing file's mode
belongs to the operator who set it. The half that was missing is something that
SAYS so, for more than the database: the boot warning looks at one path, once,
into a log.

Every test here builds a scratch corpus and points ``config.DB_PATH`` at it, so
the modes under test are the ones the test wrote rather than whatever umask the
shell running pytest happened to carry.
"""

import os
import stat

import pytest
import pytest_asyncio

from cpersona import checks, config, fileperms, session
from cpersona.database import get_db

pytestmark = pytest.mark.skipif(
    not fileperms.mode_bits_are_enforced(),
    reason="permission bits are not the access control on this platform",
)


@pytest_asyncio.fixture
async def db():
    session.reset_pauses_for_tests()
    return await get_db()


def _corpus(tmp_path, monkeypatch, *, widened=(), dir_mode=0o700):
    """Lay out one file per owned path, at modes this test chose."""
    root = tmp_path / "corpus"
    # exist_ok: a test may lay the corpus down twice to compare two runs from
    # the same starting state.
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", str(root / "corpus.db"))
    placed = {}
    for owned in fileperms.owned_paths():
        if owned.kind != "file":
            continue
        with open(owned.path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(owned.path, 0o644 if os.path.basename(owned.path) in widened else 0o600)
        placed[os.path.basename(owned.path)] = owned.path
    os.chmod(root, dir_mode)
    return root, placed


async def _run(db, fix=False):
    issues, _ = await checks.run_health_checks(db, fix=fix, checks=["file_permissions"])
    return issues


@pytest.mark.asyncio
async def test_a_private_corpus_reports_nothing(db, tmp_path, monkeypatch):
    """The control. Without it, "the check found the widened file" cannot be
    told apart from "the check reports whatever it is pointed at"."""
    _corpus(tmp_path, monkeypatch)
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_every_owned_file_is_reachable_by_the_check(db, tmp_path, monkeypatch):
    """Widen all of them and the check names all of them.

    The point is coverage of the LIST, not of one path: the boot warning this
    replaces was correct about the database and blind to everything beside it.
    """
    _, placed = _corpus(tmp_path, monkeypatch)
    for path in placed.values():
        os.chmod(path, 0o644)

    issues = await _run(db)
    assert {os.path.basename(i["path"]) for i in issues} == set(placed)
    assert {i["mode"] for i in issues} == {"0644"}
    assert all(i["repairable"] == 1 for i in issues)
    assert all(i["holds"] for i in issues), "every finding says what reaching it would give"


@pytest.mark.asyncio
async def test_the_wal_is_reported_even_when_the_database_is_private(db, tmp_path, monkeypatch):
    """The gap that motivated the check: the -wal holds the memories not yet
    checkpointed, and the boot warning never looks at it."""
    _corpus(tmp_path, monkeypatch, widened={"corpus.db-wal"})

    (issue,) = await _run(db)
    assert issue["path"].endswith("-wal")
    assert issue["type"] == "file_reachable_by_other_accounts"


@pytest.mark.asyncio
async def test_fix_narrows_the_file_and_the_next_run_is_clean(db, tmp_path, monkeypatch):
    _, placed = _corpus(tmp_path, monkeypatch, widened={"corpus.db"})
    target = placed["corpus.db"]

    (issue,) = await _run(db, fix=True)
    assert issue["fixed"] is True
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_fix_never_widens(db, tmp_path, monkeypatch):
    """A mode narrower than 0600 is not a finding and the fix does not loosen it."""
    _, placed = _corpus(tmp_path, monkeypatch)
    target = placed["corpus.db"]
    os.chmod(target, 0o400)

    assert await _run(db, fix=True) == []
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o400


@pytest.mark.asyncio
async def test_repairable_does_not_depend_on_the_fix_argument(db, tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, widened={"corpus.db"})
    dry = [i["repairable"] for i in await _run(db, fix=False)]
    _corpus(tmp_path, monkeypatch, widened={"corpus.db"})
    wet = [i["repairable"] for i in await _run(db, fix=True)]
    assert dry == wet == [1]


@pytest.mark.asyncio
async def test_a_widened_directory_is_reported_and_not_narrowed(db, tmp_path, monkeypatch):
    """A 0600 file inside a 0755 directory is still unreadable by anyone else,
    so the directory is defence in depth -- and it can hold files this package
    did not place, which a chmod would reach past."""
    root, _ = _corpus(tmp_path, monkeypatch, dir_mode=0o755)

    (issue,) = await _run(db, fix=True)
    assert issue["type"] == "directory_reachable_by_other_accounts"
    assert issue["repairable"] == 0
    assert "fixed" not in issue
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o755, "the directory was chmod-ed"


@pytest.mark.asyncio
async def test_a_directory_only_finding_stops_gating(db, tmp_path, monkeypatch):
    """repairable == 0 meets the registry's de-escalation rule: nothing an
    operator can run means the corpus does not stay 'degraded' forever."""
    _corpus(tmp_path, monkeypatch, dir_mode=0o755)

    issues, summary = await checks.run_health_checks(db, checks=["file_permissions"])

    (issue,) = issues
    assert issue["severity"] == "info"
    assert issue["needs_human_review"] is True
    assert "narrowed" in issue["hint"], "the generic out-of-reach hint replaced the real reason"
    assert summary["warn"] == 0
    assert checks.health_status(summary) == "healthy"


@pytest.mark.asyncio
async def test_a_world_writable_file_says_so(db, tmp_path, monkeypatch):
    """Readable loses the corpus; writable lets someone replace it. Both are
    warn -- the read contract is not broken yet -- so the difference has to be
    on the finding for an operator to see it."""
    _, placed = _corpus(tmp_path, monkeypatch)
    os.chmod(placed["corpus.db"], 0o666)

    (issue,) = await _run(db)
    assert issue["writable_by_others"] is True

    os.chmod(placed["corpus.db"], 0o644)
    (issue,) = await _run(db)
    assert issue["writable_by_others"] is False


@pytest.mark.asyncio
async def test_an_absent_path_is_not_a_finding(db, tmp_path, monkeypatch):
    """Most owned paths do not exist in most installs; the list carries them so
    the caller decides what absence means, and here it means nothing."""
    root = tmp_path / "empty"
    root.mkdir()
    # pytest's tmp_path arrives at 0755, so the mode is said rather than
    # inherited -- otherwise this test would be asserting about the directory it
    # was handed instead of about the absent files it is named for.
    os.chmod(root, 0o700)
    monkeypatch.setattr(config, "DB_PATH", str(root / "corpus.db"))
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_an_in_memory_database_owns_no_paths(db, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", ":memory:")
    assert fileperms.owned_paths() == []
    assert await _run(db) == []


@pytest.mark.asyncio
async def test_the_check_declines_where_the_bits_are_not_the_control(db, tmp_path, monkeypatch):
    """Windows is a shipped target (``Operating System :: OS Independent``) and
    a mode there is not a statement anything enforces. Scoring those bits would
    report every install as reachable by everyone and offer a repair that
    changes nothing -- so on such a platform the check has no opinion at all.

    The seam is patched rather than ``os.name``, which is global: rebinding it
    mid-run makes ``pathlib.Path`` produce a ``WindowsPath``, and pytest builds
    one while formatting a failure. A test written that way passes cleanly and
    then, the moment it has something to report, takes the whole run down with
    an INTERNALERROR instead of failing. Measured while mutating this check,
    not supposed.
    """
    _corpus(tmp_path, monkeypatch, widened={"corpus.db"})
    assert await _run(db), "the fixture must fire before its absence means anything"

    monkeypatch.setattr(fileperms, "mode_bits_are_enforced", lambda: False)
    assert await _run(db, fix=True) == []


@pytest.mark.parametrize(
    "platform_name, enforced",
    [("posix", True), ("nt", False), ("java", False)],
)
def test_which_platforms_enforce_the_bits(platform_name, enforced):
    """Both answers, without rebinding os.name — see the docstring on the
    function for why that global is not an option here."""
    assert fileperms.mode_bits_are_enforced(platform_name) is enforced


def test_the_bits_are_enforced_where_this_suite_runs():
    """The skipif at the top of the module already says so. This pins that the
    positive branch is the one every test above is exercising."""
    assert fileperms.mode_bits_are_enforced() is True

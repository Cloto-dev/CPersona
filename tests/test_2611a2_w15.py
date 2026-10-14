"""Regression tests for three operational gaps.

  uvx detection   A process uv launched lives in ``<cache>/archive-v0/<hash>``.
                  The cache root is ``%LOCALAPPDATA%\\uv\\cache`` on Windows and
                  anything at all under UV_CACHE_DIR, so detection keys on the
                  ``archive-v0`` parent of the prefix, not on a ``uv`` directory
                  above it. Missing it classifies the launch as pip, and
                  ``check_update(apply=true)`` then installs into uv's cache.
  release gate    The publish workflow runs the suite on the exact commit it
                  uploads, and a push to a maintenance line gets CI: a tag cut
                  on a directly pushed backport otherwise ships untested, and a
                  published wheel cannot be replaced.
  alias ledger    The ledger is part of what a backup must carry. Restoring the
                  database without it refuses boot, and the refusal names
                  restoring the file before the remedies that would sever every
                  subject from its memory.

The workflow tests read the YAML as text: the suite's environment carries no
YAML parser, and the shapes checked here are the ones a reviewer reads.
"""

import json
import os
import pathlib
import re
import sys
import tempfile

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_w15.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402

from cpersona import acl, aliases, update_check  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


# ---------------------------------------------------------------------------
# uvx detection
# ---------------------------------------------------------------------------

# (label, sys.prefix, the package directory inside it)
_UVX_LAUNCHES = [
    (
        "linux default cache",
        "/home/u/.cache/uv/archive-v0/deadbeef",
        "/home/u/.cache/uv/archive-v0/deadbeef/lib/python3.12/site-packages/cpersona",
    ),
    (
        "UV_CACHE_DIR whose last component is not 'uv'",
        "/opt/uvcache/archive-v0/_K5kcaNBMR3kFpnt",
        "/opt/uvcache/archive-v0/_K5kcaNBMR3kFpnt/lib/python3.12/site-packages/cpersona",
    ),
    (
        "Windows default %LOCALAPPDATA%\\uv\\cache",
        r"C:\Users\u\AppData\Local\uv\cache\archive-v0\AbCd",
        r"C:\Users\u\AppData\Local\uv\cache\archive-v0\AbCd\Lib\site-packages\cpersona",
    ),
]


def _pin_location(monkeypatch, prefix, package_dir):
    monkeypatch.setattr(update_check.sys, "prefix", prefix)
    monkeypatch.setattr(update_check, "_package_dir", lambda: package_dir)
    monkeypatch.setattr(
        update_check, "_installer_prefix", lambda: [sys.executable, "-m", "pip", "install"]
    )


@pytest.mark.parametrize(("label", "prefix", "package_dir"), _UVX_LAUNCHES)
def test_uvx_is_detected_under_any_cache_root(monkeypatch, label, prefix, package_dir):
    _pin_location(monkeypatch, prefix, package_dir)
    install = update_check.detect_install("2.6.11")
    assert install["method"] == "uvx", label
    assert install["argv_steps"] == [], label
    assert install["command"] == "uvx cpersona@2.6.11", label


def test_archive_v0_must_be_the_parent_of_the_environment(monkeypatch):
    """The marker is the directory uv unpacks environments into, not a
    substring: a venv that is itself named archive-v0 is an ordinary install."""
    _pin_location(
        monkeypatch, "/srv/archive-v0", "/srv/archive-v0/lib/python3.12/site-packages/cpersona"
    )
    assert update_check.detect_install("2.6.11")["method"] == "pip"


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "prefix", "package_dir"), _UVX_LAUNCHES)
async def test_apply_runs_nothing_under_a_uvx_launch(monkeypatch, label, prefix, package_dir):
    _pin_location(monkeypatch, prefix, package_dir)
    monkeypatch.setattr(
        update_check, "current", lambda: {"state": "update_available", "available": "2.6.11"}
    )
    ran = []

    async def record(argv):
        ran.append(argv)
        return 0, "Successfully installed cpersona-2.6.11"

    monkeypatch.setattr(update_check, "_run_step", record)
    result = await update_check.apply()
    assert ran == [], label
    assert result["applied"] is False, label
    assert "uvx" in result["reason"], label


# ---------------------------------------------------------------------------
# Release gate (workflow structure)
# ---------------------------------------------------------------------------


def _children(lines, start, indent):
    """The `key:` lines directly under ``lines[start]`` at ``indent``, mapped to
    the index of each; the block ends at the first line indented less."""
    out = {}
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth = len(line) - len(line.lstrip(" "))
        if depth < indent:
            break
        m = re.match(r"^ {%d}([A-Za-z0-9_-]+):" % indent, line)
        if m:
            out[m.group(1)] = i
    return out


def _body(lines, start):
    """The text of the block under ``lines[start]``."""
    depth = len(lines[start]) - len(lines[start].lstrip(" "))
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= depth:
            break
        body.append(line)
    return "\n".join(body)


def _jobs(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    top = _children(lines, -1, 0)
    jobs = _children(lines, top["jobs"], 2)
    return {name: _body(lines, i) for name, i in jobs.items()}


def _needs(job_body):
    m = re.search(r"^ {4}needs: *(.+)$", job_body, re.M)
    if not m:
        return set()
    return {n.strip() for n in m.group(1).strip("[] ").split(",") if n.strip()}


def _checkout_ref(job_body):
    m = re.search(r"uses: actions/checkout@\S+\n\s+with:\n\s+ref: *(\S.*)$", job_body, re.M)
    return m.group(1).strip() if m else None


def _needs_closure(jobs, name):
    seen, todo = set(), [name]
    while todo:
        for dep in _needs(jobs[todo.pop()]):
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return seen


def test_publish_uploads_only_after_the_suite_passes_on_the_same_commit():
    jobs = _jobs(WORKFLOWS / "publish.yml")
    uploaders = [n for n, b in jobs.items() if "pypa/gh-action-pypi-publish" in b]
    assert len(uploaders) == 1, uploaders
    uploader = uploaders[0]
    upload_ref = _checkout_ref(jobs[uploader])
    assert upload_ref, "the upload job does not pin the ref it builds"

    gates = [
        n
        for n in _needs_closure(jobs, uploader)
        if re.search(r"^\s+run: .*\bpytest\b", jobs[n], re.M)
    ]
    assert gates, (
        "no job the upload depends on runs the test suite: any v* tag is "
        "built and uploaded to PyPI untested"
    )
    for gate in gates:
        # Testing some other commit than the one uploaded is no gate at all.
        assert _checkout_ref(jobs[gate]) == upload_ref, (gate, _checkout_ref(jobs[gate]))


def _branch_filter_matches(pattern, branch):
    """GitHub's branch-filter glob (the subset the workflow uses)."""
    rx = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            rx += ".*"
            i += 2
            continue
        if c == "*":
            rx += "[^/]*"
        elif c == "[":
            j = pattern.index("]", i)
            rx += pattern[i : j + 1]
            i = j + 1
            continue
        elif c in "+?":
            rx += c
        else:
            rx += re.escape(c)
        i += 1
    return re.fullmatch(rx, branch) is not None


def test_a_push_to_a_maintenance_line_runs_ci():
    lines = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8").splitlines()
    on = _children(lines, -1, 0)["on"]
    push = _children(lines, on, 2)["push"]
    branches_line = lines[_children(lines, push, 4)["branches"]]
    patterns = [
        p.strip().strip("'\"")
        for p in branches_line.split(":", 1)[1].strip().strip("[]").split(",")
    ]
    for branch in ("master", "2.4.x", "2.5.x", "2.6.x", "3.0.x"):
        assert any(_branch_filter_matches(p, branch) for p in patterns), (branch, patterns)
    # The filter still means something: a feature branch is covered by its PR.
    assert not any(_branch_filter_matches(p, "fix/some-branch") for p in patterns), patterns


# ---------------------------------------------------------------------------
# Alias ledger: backup runbook and the boot refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "start", "end"),
    [
        ("operations.md", "**The `.db` is not the whole instance.**", "## Detecting a dead embedding server"),
        ("operations.ja.md", "**`.db` はインスタンスの全体ではありません。**", "## "),
    ],
)
def test_backup_runbook_lists_the_alias_ledger(page, start, end):
    text = (ROOT / "docs" / page).read_text(encoding="utf-8")
    begin = text.index(start)
    section = text[begin : text.index(end, begin)]
    assert "alias_ledger.json" in section, page
    assert "CPERSONA_ALIAS_LEDGER_FILE" in section, page


ISSUER = "https://auth.example.com"


@pytest.fixture
def _per_subject(tmp_path):
    path = tmp_path / "acl.json"
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": f"oauth:{ISSUER}:c",
                        "token": None,
                        "grants": {"*": "read-write"},
                        "per_subject": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    acl.activate(acl.load_config(str(path)))
    yield tmp_path
    acl.activate(None)
    acl.activate_ledger(None)


@pytest.mark.asyncio
async def test_boot_refusal_after_a_restore_names_restoring_the_ledger_first(_per_subject):
    from cpersona import database, memory_handlers, server

    await database.init_db()
    host_a = _per_subject / "a"
    host_b = _per_subject / "b"
    host_a.mkdir()
    host_b.mkdir()
    acl.activate_ledger(aliases.AliasLedger(str(host_a / "alias_ledger.json")))
    alias, issued = acl.active_ledger().resolve_or_issue(ISSUER, "user-1")
    assert issued
    try:
        stored = await memory_handlers.do_store(alias, {"content": "a subject's memory"})
        assert stored["ok"] is True
        await server._assert_no_reserved_agent_ids()

        # The database came back; the ledger beside it did not.
        missing = str(host_b / "alias_ledger.json")
        acl.activate_ledger(aliases.AliasLedger(missing))
        with pytest.raises(RuntimeError) as refused:
            await server._assert_no_reserved_agent_ids()
        message = str(refused.value)
        assert alias in message
        assert missing in message, "the refusal does not say where the ledger was looked for"
        assert "CPERSONA_ALIAS_LEDGER_FILE" in message
        restore = message.lower().find("restore")
        assert restore != -1, message
        # Renaming the agents or dropping per_subject severs every subject from
        # what it stored, so restoring the file has to be offered first.
        assert restore < message.index("merge_memories"), message
        assert restore < message.index("remove per_subject"), message
    finally:
        async with database.connection() as db:
            await db.execute("DELETE FROM memories WHERE agent_id = ?", (alias,))
            await db.commit()

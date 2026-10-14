"""Regression tests for CI and documentation gates that missed what they claim to check.

  CI concurrency   The weekly scheduled run shares no concurrency group with a
                   push: a scheduled run on master otherwise cancels the push
                   run's test, lint and mutation gates and replaces them with a
                   run that skips every one of them.
  locked sync      Every ``uv sync`` in CI passes ``--locked``, so a lock that
                   disagrees with pyproject fails the job instead of being
                   re-resolved in the runner and merged stale.
  docs trigger     Every mkdocs build hook is on the docs workflow's path
                   filters, or editing a hook is the one change the strict
                   build never runs on.
  env defaults     docs-facts compares a documented default wherever the
                   package reads the variable, not only for config.py's parse
                   shapes, and a row it cannot compare is a failure.
  dead patterns    Each docs-facts claim pattern must find its subject: a
                   pattern that matches no page passes the claim unchecked.
  ja LOC claims    The Japanese QA page's LOC figures are read like the
                   English ones.
  docs-live age    A stalled publisher is measured by the oldest unpublished
                   commit, not by the newest one.
  nested docs      The i18n drift gate and the mermaid gate walk docs/research/
                   and docs/design/, which the site publishes like any page.
  waiver registry  A waivers key that is missing or not a list is a malformed
                   registry, not an empty one.

Workflow files are read as text: the suite's environment carries no YAML
parser, and the shapes checked here are the ones a reviewer reads.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

os.environ.setdefault("CPERSONA_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_2611a2_x16.db"))
os.environ.setdefault("CPERSONA_EMBEDDING_MODE", "none")

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# CI: concurrency group and locked sync
# ---------------------------------------------------------------------------


def _concurrency_group(text: str) -> str:
    m = re.search(r"^concurrency:\n(?:  #.*\n)*  group: (?P<group>.+)$", text, re.M)
    assert m, "ci.yml declares no top-level concurrency group"
    return m.group("group").strip()


def _render(expression: str, context: dict[str, str]) -> str:
    """Render the ``${{ github.<name> }}`` placeholders of a workflow string.

    Anything else inside ``${{ }}`` is refused rather than guessed at: a group
    this cannot render has to be read by a person, not passed by a test.
    """

    def one(m: re.Match) -> str:
        inner = m.group(1).strip()
        assert inner in context, f"cannot render {inner!r} in {expression!r}"
        return context[inner]

    return re.sub(r"\$\{\{(.*?)\}\}", one, expression)


def test_scheduled_ci_run_does_not_share_the_push_concurrency_group():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^  schedule:", text, re.M), "ci.yml no longer has a schedule trigger"
    group = _concurrency_group(text)
    push = _render(group, {"github.ref": "refs/heads/master", "github.event_name": "push"})
    later_push = _render(group, {"github.ref": "refs/heads/master", "github.event_name": "push"})
    scheduled = _render(group, {"github.ref": "refs/heads/master", "github.event_name": "schedule"})
    assert scheduled != push, (
        f"a scheduled run on master joins the push run's group {push!r}, so with "
        "cancel-in-progress it cancels the push run's gates and skips them itself"
    )
    # Two pushes to one branch still share a group: superseding them is the point.
    assert later_push == push


def test_every_ci_dependency_sync_refuses_a_stale_lock():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    syncs = [line.strip() for line in text.splitlines() if re.search(r"\buv sync\b", line)]
    assert syncs, "ci.yml runs no uv sync — the check has no subject"
    unlocked = [line for line in syncs if "--locked" not in line.split()]
    assert not unlocked, (
        "these syncs re-resolve a lock that disagrees with pyproject instead of failing, "
        f"so a stale uv.lock merges green: {unlocked}"
    )


# ---------------------------------------------------------------------------
# docs workflow: every build hook is on the trigger
# ---------------------------------------------------------------------------


def _mkdocs_hooks() -> list[str]:
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = re.search(r"^hooks:\n(?P<body>(?:  - .*\n)+)", text, re.M)
    assert block, "mkdocs.yml declares no hooks"
    return [line[4:].strip() for line in block.group("body").splitlines()]


def _trigger_paths(text: str, trigger: str) -> list[str]:
    block = re.search(rf"^  {trigger}:\n(?P<body>(?:    .*\n|\n)*)", text, re.M)
    assert block, f"no {trigger} trigger found"
    return re.findall(r'^      - "(?P<path>[^"]+)"', block.group("body"), re.M)


def test_every_mkdocs_hook_is_on_the_docs_trigger():
    hooks = _mkdocs_hooks()
    assert len(hooks) >= 4, hooks
    text = (WORKFLOWS / "docs.yml").read_text(encoding="utf-8")
    for trigger in ("push", "pull_request"):
        paths = _trigger_paths(text, trigger)
        missing = [hook for hook in hooks if hook not in paths]
        assert not missing, (
            f"mkdocs runs these hooks in every build, but docs.yml's {trigger} filter "
            f"does not list them, so a change to one never runs the strict build: {missing}"
        )


# ---------------------------------------------------------------------------
# docs-facts
# ---------------------------------------------------------------------------


@pytest.fixture
def cdf():
    module = _load("check_docs_facts_x16", "check-docs-facts.py")
    module.failures.clear()
    return module


def _fake_tree(tmp_path: Path, sources: dict[str, str], pages: dict[str, str]) -> list[Path]:
    for name, body in sources.items():
        path = tmp_path / "cpersona" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    docs = []
    for name, body in pages.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        docs.append(path)
    return docs


def test_env_defaults_read_outside_config_are_parsed(cdf):
    defaults = cdf.parsed_env_defaults()
    assert defaults.get("CPERSONA_HTTP_HOST") == "127.0.0.1"
    assert defaults.get("CPERSONA_HTTP_PORT") == "8402"
    assert defaults.get("CPERSONA_ALLOW_UNAUTHENTICATED_HTTP") == "false"
    assert defaults.get("CPERSONA_EMBEDDING_MODE") == "none"
    assert defaults.get("CPERSONA_DB_PATH") == "data/cpersona.db"


def test_a_changed_server_side_default_fails_the_docs_table(cdf, tmp_path):
    docs = _fake_tree(
        tmp_path,
        {
            "config.py": 'X = _parse_int("CPERSONA_A", 3)\n',
            "server.py": 'host = os.environ.get("CPERSONA_HTTP_HOST", "0.0.0.0")\n'
            'port = config.parse_int("CPERSONA_HTTP_PORT", 8403)\n',
        },
        {
            "docs/configuration.md": "| Var | Default |\n|---|---|\n"
            "| `CPERSONA_HTTP_HOST` | `127.0.0.1` | Bind address |\n"
            "| `CPERSONA_HTTP_PORT` | `8402` | Bind port |\n",
        },
    )
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = docs
    cdf.check_env_tables(cdf.parsed_env_defaults())
    joined = "\n".join(cdf.failures)
    assert "CPERSONA_HTTP_HOST" in joined and "0.0.0.0" in joined, cdf.failures
    assert "CPERSONA_HTTP_PORT" in joined and "8403" in joined, cdf.failures


def test_a_documented_default_no_parse_shape_reads_is_a_failure(cdf, tmp_path):
    docs = _fake_tree(
        tmp_path,
        {"config.py": 'X = int(os.getenv("CPERSONA_ODD") or 7)\n'},
        {"docs/configuration.md": "| `CPERSONA_ODD` | `7` | read through an unparsed shape |\n"},
    )
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = docs
    cdf.check_env_tables(cdf.parsed_env_defaults())
    assert any("CPERSONA_ODD" in f and "compared" in f for f in cdf.failures), cdf.failures


def test_two_different_defaults_for_one_variable_are_reported(cdf, tmp_path):
    _fake_tree(
        tmp_path,
        {
            "config.py": 'A = os.environ.get("CPERSONA_HOST", "127.0.0.1")\n',
            "server.py": 'B = os.environ.get("CPERSONA_HOST", "0.0.0.0")\n',
        },
        {},
    )
    cdf.ROOT = tmp_path
    cdf.parsed_env_defaults()
    assert any("CPERSONA_HOST" in f for f in cdf.failures), cdf.failures


def test_the_live_env_tables_are_fully_compared(cdf):
    cdf.check_env_tables(cdf.parsed_env_defaults())
    assert cdf.failures == []


@pytest.fixture(scope="module")
def acceptance():
    module = _load("check_docs_facts_x16_acceptance", "check-docs-facts.py")
    return module.measured_axis_acceptance()


@pytest.mark.parametrize("page", ["docs/tools.md", "docs/tools.ja.md"])
def test_channel_name_pattern_reaches_the_live_page(cdf, acceptance, page):
    text = (ROOT / page).read_text(encoding="utf-8")
    named = [
        set(re.findall(r"`(\w+)`", m.group(1)))
        for pattern in cdf._CHANNEL_NAME_CLAIMS
        for m in re.finditer(pattern, text)
    ]
    assert named, f"no channel-name pattern matches {page}: the name check never runs there"
    assert all(n == set(acceptance["channel"]) for n in named), (named, acceptance["channel"])


@pytest.mark.parametrize("page", ["docs/tools.md", "docs/tools.ja.md"])
def test_a_wrong_channel_tool_name_fails(cdf, acceptance, tmp_path, page):
    text = (ROOT / page).read_text(encoding="utf-8")
    assert "`traverse`" in text
    copy = tmp_path / page
    copy.parent.mkdir(parents=True)
    copy.write_text(text.replace("`traverse`", "`get_contents`"), encoding="utf-8")
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = [copy]
    cdf.check_axis_claims(acceptance)
    assert any("get_contents" in f and "channel" in f for f in cdf.failures), cdf.failures


def test_the_live_axis_claims_hold(cdf, acceptance):
    cdf.check_axis_claims(acceptance)
    assert cdf.failures == []


def test_an_axis_claim_pattern_with_no_subject_fails(cdf, acceptance, tmp_path):
    # The tools pages with the channel sentences reworded out of every pattern's reach.
    pages = {}
    for page in ("docs/tools.md", "docs/tools.ja.md"):
        text = (ROOT / page).read_text(encoding="utf-8")
        text = text.replace("`channel` by exactly", "`channel` by only").replace(
            "`channel` はちょうど", "`channel` は"
        )
        pages[page] = text
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = _fake_tree(tmp_path, {}, pages)
    cdf.check_axis_claims(acceptance)
    dead = [f for f in cdf.failures if "matched nothing" in f]
    # Both count patterns and both name patterns for `channel` lost their subject.
    assert len(dead) == 4, cdf.failures


def test_embed_batch_pattern_with_no_subject_fails(cdf, tmp_path):
    cdf.check_embed_batch(64)
    assert not any("matched nothing" in f for f in cdf.failures), cdf.failures
    cdf.failures.clear()
    text = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert "texts per request**" in text
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = _fake_tree(
        tmp_path,
        {},
        {
            "docs/getting-started.md": text.replace("texts per request**", "texts** per request"),
            "docs/getting-started.ja.md": (ROOT / "docs" / "getting-started.ja.md").read_text(encoding="utf-8"),
        },
    )
    cdf.check_embed_batch(64)
    assert any("matched nothing" in f and "texts per request" in f for f in cdf.failures), cdf.failures


def test_every_volatile_claim_pattern_finds_its_subject(cdf):
    # A negative stat skips the drift comparison, so only reach is measured here.
    cdf.check_volatile_claims({key: -1 for key in set(cdf.VOLATILE_CLAIMS.values())})
    assert cdf.failures == []


def test_a_volatile_claim_pattern_with_no_subject_fails(cdf, tmp_path):
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = _fake_tree(tmp_path, {}, {"docs/empty.md": "nothing measurable here\n"})
    cdf.check_volatile_claims({key: -1 for key in set(cdf.VOLATILE_CLAIMS.values())})
    dead = [f for f in cdf.failures if "matched nothing" in f]
    assert len(dead) == len(cdf.VOLATILE_CLAIMS), cdf.failures


@pytest.mark.parametrize("key", ["server LOC", "vendored LOC", "test LOC"])
def test_japanese_qa_page_loc_claims_are_read(cdf, key):
    ja = (ROOT / "docs" / "quality-assurance.ja.md").read_text(encoding="utf-8")
    en = (ROOT / "docs" / "quality-assurance.md").read_text(encoding="utf-8")

    def claims(text: str) -> set[int]:
        return {
            int(m.group(1).replace(",", ""))
            for pattern, k in cdf.VOLATILE_CLAIMS.items()
            if k == key
            for m in re.finditer(pattern, text)
        }

    assert claims(en), f"no pattern reads the English {key} claim"
    assert claims(ja) == claims(en), (
        f"the Japanese QA page's {key} figure is read as {claims(ja)}, the English "
        f"one as {claims(en)}: a stale translation keeps publishing an unchecked number"
    )


def test_a_drifted_japanese_loc_figure_fails(cdf, tmp_path):
    ja = (ROOT / "docs" / "quality-assurance.ja.md").read_text(encoding="utf-8")
    cdf.ROOT = tmp_path
    cdf.DOC_FILES = _fake_tree(tmp_path, {}, {"docs/quality-assurance.ja.md": ja})
    stats = {key: -1 for key in set(cdf.VOLATILE_CLAIMS.values())}
    stats["vendored LOC"] = 10_000_000
    cdf.check_volatile_claims(stats)
    assert any("quality-assurance.ja.md" in f and "vendored LOC" in f for f in cdf.failures), cdf.failures


# ---------------------------------------------------------------------------
# docs-live: the age of what has been waiting
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, when: dt.datetime | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def test_a_stopped_publisher_is_stalled_while_commits_keep_landing(tmp_path, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    live = _load("check_docs_live_x16", "check-docs-live.py")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    now = dt.datetime.now(dt.timezone.utc)
    commits = {}
    # A was published; B has waited 120h, longer than the 48h bound, while the
    # newest commit, D, is only 20h old.
    for name, hours_ago in (("A", 150), ("B", 120), ("C", 80), ("D", 20)):
        (repo / "f").write_text(name)
        _git(repo, "add", "f")
        _git(repo, "commit", "-q", "-m", name, when=now - dt.timedelta(hours=hours_ago))
        commits[name] = _git(repo, "rev-parse", "HEAD")

    site = tmp_path / "public"
    site.mkdir()
    (site / "index.html").write_text("<p>page</p>")
    (site / "versions.json").write_text(
        json.dumps({"current": "2.6", "versions": [{"id": "2.6", "commit": commits["D"]}]})
    )
    published = json.dumps({"current": "2.6", "versions": [{"id": "2.6", "commit": commits["A"]}]})

    def fake_fetch(url):
        if url.endswith("versions.json"):
            return published.encode(), None
        raise AssertionError(f"a lagging or stalled line is not compared, yet {url} was fetched")

    monkeypatch.setattr(live, "fetch", fake_fetch)
    monkeypatch.chdir(repo)
    rc = live.main(["--site", str(site), "--base", "https://example.invalid/site/"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "waiting to publish" in out, out


def test_a_line_published_recently_is_only_lagging(tmp_path, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    live = _load("check_docs_live_x16_lag", "check-docs-live.py")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    now = dt.datetime.now(dt.timezone.utc)
    commits = {}
    # A is old, but the first commit after it has waited only 10h.
    for name, hours_ago in (("A", 400), ("B", 10), ("C", 5)):
        (repo / "f").write_text(name)
        _git(repo, "add", "f")
        _git(repo, "commit", "-q", "-m", name, when=now - dt.timedelta(hours=hours_ago))
        commits[name] = _git(repo, "rev-parse", "HEAD")
    site = tmp_path / "public"
    site.mkdir()
    (site / "index.html").write_text("<p>page</p>")
    (site / "versions.json").write_text(
        json.dumps({"current": "2.6", "versions": [{"id": "2.6", "commit": commits["C"]}]})
    )
    published = json.dumps({"current": "2.6", "versions": [{"id": "2.6", "commit": commits["A"]}]})
    monkeypatch.setattr(live, "fetch", lambda url: (published.encode(), None))
    monkeypatch.chdir(repo)
    rc = live.main(["--site", str(site), "--base", "https://example.invalid/site/"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "lagging" in out, out


# ---------------------------------------------------------------------------
# Nested docs pages: i18n drift and mermaid
# ---------------------------------------------------------------------------


def test_i18n_drift_reads_translations_in_subdirectories(tmp_path, monkeypatch, capsys):
    drift = _load("check_i18n_drift_x16", "check-i18n-drift.py")
    research = tmp_path / "docs" / "research"
    research.mkdir(parents=True)
    (tmp_path / "docs" / "top.md").write_text("top\n")
    top_blob = drift.blob_sha(tmp_path / "docs" / "top.md")
    (tmp_path / "docs" / "top.ja.md").write_text(f"<!-- i18n-source: docs/top.md@blob:{top_blob} -->\nja\n")
    (research / "note.md").write_text("an English edit\n")
    (research / "note.ja.md").write_text("<!-- i18n-source: docs/research/note.md@blob:" + "0" * 40 + " -->\nja\n")
    drift.ROOT = tmp_path
    monkeypatch.setattr(sys, "argv", ["check-i18n-drift.py", "--strict", "--json"])
    rc = drift.main()
    report = json.loads(capsys.readouterr().out)
    assert [f["translation"] for f in report["stale"]] == ["docs/research/note.ja.md"], report
    assert rc == 1


def test_mermaid_gate_parses_blocks_in_subdirectories(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the docs workflow runs the mermaid gate with it")
    # The script's own code, run against stand-ins for its two dependencies: a
    # parser that rejects one marked block, and a DOM that is never rendered into.
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check-mermaid.mjs", tmp_path / "scripts" / "check-mermaid.mjs")
    for name, body in {
        "jsdom": "export class JSDOM { constructor() { this.window = {}; } }\n",
        "mermaid": "export default { initialize() {}, async parse(text) {"
        " if (text.includes('BROKEN')) throw new Error('parse error'); return true; } };\n",
    }.items():
        pkg = tmp_path / "node_modules" / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"name": name, "type": "module", "main": "index.js"}))
        (pkg / "index.js").write_text(body)
    docs = tmp_path / "docs"
    (docs / "research").mkdir(parents=True)
    (docs / "top.md").write_text("```mermaid\ngraph TD\n  A --> B\n```\n")
    (docs / "research" / "note.md").write_text("# Note\n\n```mermaid\nBROKEN\n```\n")
    run = subprocess.run(
        [node, str(tmp_path / "scripts" / "check-mermaid.mjs"), str(docs)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert "research/note.md" in run.stdout, run.stdout
    assert "2 mermaid block(s)" in run.stderr, run.stderr


# ---------------------------------------------------------------------------
# Mutation-waiver registry: the list itself must be there
# ---------------------------------------------------------------------------


@pytest.fixture
def mw():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import mutation_waivers
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    return mutation_waivers


@pytest.mark.parametrize(
    "registry",
    [
        {"version": "1.0.0", "waiver": []},
        {"version": "1.0.0", "waivers": {}},
        {"version": "1.0.0", "waivers": "none"},
        {"version": "1.0.0", "waivers": ["not-an-entry"]},
    ],
    ids=["renamed-key", "object", "string", "non-object-entry"],
)
def test_a_registry_without_a_waiver_list_is_malformed(mw, tmp_path, registry, capsys):
    problems = mw.verify(tmp_path, registry, date(2026, 10, 14))
    assert any(p["severity"] == "hard" for p in problems), problems
    path = tmp_path / "waivers.json"
    path.write_text(json.dumps(registry))
    assert mw.main(["--registry", str(path), "--repo", str(tmp_path)]) == 1
    capsys.readouterr()


def test_an_empty_waiver_list_is_still_valid(mw, tmp_path, capsys):
    path = tmp_path / "waivers.json"
    path.write_text(json.dumps({"version": "1.0.0", "waivers": []}))
    assert mw.main(["--registry", str(path), "--repo", str(tmp_path), "--strict"]) == 0
    assert mw.active_waivers(tmp_path, {"waivers": {}}, date(2026, 10, 14)) == {}
    capsys.readouterr()

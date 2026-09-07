"""The version map is readable, and the published root is stated once.

`scripts/build-all-versions.py` assembles the documentation site from one branch
per version line. Two of its inputs can drift apart without any build failing:
the map itself (a hand-edited JSON file that nothing else parses) and the site
root, which is now written both in the map and as the default of the `!ENV` tag
in `mkdocs.yml`. A disagreement between those two publishes canonical links and
a sitemap pointing at a root the link checker does not check against, and every
gate stays green while it happens.

These tests are about the inputs, not the assembly. Whether every declared line
actually builds, and whether the manifest matches the support policy, is checked
against a built tree, which is a different subject and a different gate.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "docs-versions.json"
MKDOCS_PATH = REPO_ROOT / "mkdocs.yml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docs.yml"


def _workflow_text() -> str:
    """The workflow as text.

    Read textually rather than parsed, for the same reason the documentation
    index check reads mkdocs.yml that way: a YAML loader is not a dependency of
    this suite, and adding one to assert three strings would make the assertion
    the reason for the dependency.
    """
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _assembler():
    """Import the hyphenated script by path — `scripts/` is not a package."""
    path = REPO_ROOT / "scripts" / "build-all-versions.py"
    spec = importlib.util.spec_from_file_location("build_all_versions_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mkdocs_site_url_default() -> str:
    """The default in mkdocs.yml's site_url, read the way the docs-index gate reads it.

    Deliberately textual: the value has to be readable before a build, and the
    build environment's YAML loader is not available to every reader of this
    file.
    """
    pattern = re.compile(
        r"^site_url:\s*!ENV\s*\[\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*"
        r"[\"'](?P<url>[^\"']+)[\"']\s*\]\s*$"
    )
    plain = re.compile(r"^site_url:\s*(?P<url>\S+)\s*$")
    for line in MKDOCS_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line) or plain.match(line)
        if match:
            return match.group("url")
    raise AssertionError("mkdocs.yml declares no site_url")


def test_the_version_map_loads():
    """The map passes the assembler's own validation, not a second copy of it."""
    assembler = _assembler()
    config = assembler.load_config(CONFIG_PATH)
    assert config["versions"], "a site with no version lines has nothing to publish"


def test_the_current_line_is_one_of_the_declared_lines():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ids = [version["id"] for version in config["versions"]]
    assert config["current"] in ids


def test_the_published_root_is_the_same_in_both_places():
    """The map and mkdocs.yml must agree on where the site lives.

    They are separate because they are read at different times — the map before
    any build, mkdocs.yml during one — and neither can see the other. This is
    the only thing keeping them in step.
    """
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["site_url"] == _mkdocs_site_url_default()


def test_every_declared_line_names_a_branch_that_exists():
    """A line declared before its branch is cut fails the build with no tree to show.

    Caught here instead, where the message can say which entry is early: the map
    is edited by hand on the day a line is split, and that is exactly when the
    branch may not have been pushed yet.
    """
    assembler = _assembler()
    config = assembler.load_config(CONFIG_PATH)
    for version in config["versions"]:
        try:
            assembler.resolve_ref(version["branch"])
        except assembler.BuildError as exc:  # pragma: no cover - only on a real break
            pytest.fail(f"version {version['id']}: {exc}")


def test_a_version_id_cannot_escape_its_directory(tmp_path):
    """The id becomes a path segment, so the map must not be able to write outside it.

    Pinned because the failure is silent in the artifact rather than at build
    time: a traversing id produces files somewhere unexpected and a tree that
    looks complete.
    """
    assembler = _assembler()
    bad = tmp_path / "docs-versions.json"
    bad.write_text(
        json.dumps(
            {
                "site_url": "https://example.test/",
                "current": "../escape",
                "versions": [{"id": "../escape", "title": "x", "branch": "master"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(assembler.BuildError, match="path segment"):
        assembler.load_config(bad)


def test_the_two_publishing_conditions_stay_identical():
    """The artifact upload and the deploy job must gate on the same events.

    They are the pair that can disagree silently. Widen one and not the other and
    every job is green while nothing reaches the site -- the deploy runs with no
    artifact, or the artifact is built for a run that never deploys. Neither
    shows up as a failure, which is why the agreement is pinned here instead of
    being left to whoever edits one of them next.
    """
    conditions = re.findall(
        r"^\s*if: (github\.event_name [!=]= 'pull_request')\s*$", _workflow_text(), re.M
    )
    assert len(conditions) == 2, f"expected two publishing conditions, found {conditions}"
    assert conditions[0] == conditions[1], conditions


def test_what_the_assembler_writes_is_what_gets_published():
    """The directory the assembly step writes is the one the upload step reads.

    Two independent strings in the same file naming the same directory. If they
    drift, the upload publishes an empty or stale path and the run stays green,
    so the site keeps serving whatever it served before with nothing saying why.
    """
    text = _workflow_text()
    written = re.search(r"build-all-versions\.py\s*\n?\s*--out (?P<dir>\S+)", text)
    uploaded = re.search(r"upload-pages-artifact@v3.*?\n\s*with:\s*\n\s*path: (?P<dir>\S+)", text, re.S)
    assert written, "the assembly step no longer passes --out"
    assert uploaded, "the upload step no longer declares a path"
    assert written.group("dir") == uploaded.group("dir").rstrip("/")


def test_publishing_is_not_only_driven_by_pushes_to_this_branch():
    """A change on another line's branch produces no event here.

    Without a clock and a manual trigger, a correction to a released line would
    sit unpublished until something unrelated happened to touch a path on this
    branch. Both triggers are load-bearing rather than conveniences, so removing
    either is a behaviour change and should have to be deliberate.
    """
    text = _workflow_text()
    assert re.search(r"^  schedule:\s*$", text, re.M), "the daily publish is gone"
    assert re.search(r"^  workflow_dispatch:\s*$", text, re.M), "the manual publish is gone"


def test_only_review_builds_cancel_a_publish_that_is_already_running():
    """A publish that has started is never killed by a later one.

    Measured rather than assumed. With a push mid-flight and two dispatches sent
    ten seconds apart into the same group: the running push deployed
    successfully, the first dispatch was cancelled while still *pending* and
    never started a job, and the second ran to a successful deploy afterwards.

    So this setting buys two things, and only the first is what it is named
    after: a run that has begun always finishes, and among the runs waiting
    behind it only the newest survives. Both are what this site wants. The first
    is the failure the published-site check exists to catch -- a deploy
    cancelled by a run that does not replace it, after which nothing retries and
    nothing reports. The second is free: a queued run would publish content the
    newer one is about to supersede.
    """
    text = _workflow_text()
    match = re.search(r"^\s*cancel-in-progress: (?P<value>.+)$", text, re.M)
    assert match, "the concurrency block no longer says what it cancels"
    assert "pull_request" in match.group("value"), (
        f"cancel-in-progress is {match.group('value')!r}: publishes cancel each other again"
    )

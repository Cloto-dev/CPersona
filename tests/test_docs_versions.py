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

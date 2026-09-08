"""The line a page says it applies to is derived, and nothing writes it by hand.

Eight pages open with "Applies to: CPersona <line>", and each has a translated
copy that says the same thing. Written by hand, that sentence is right until the
branch is copied to start the next line -- at which point sixteen pages name the
line they were copied from, the site builds, the translations stay in sync, every
link resolves, and nothing is red.

So the label is filled in at build time by `scripts/docs_version.py`, from the
version the tree itself states, and three things have to hold for that to be
worth anything:

  * the hook is actually wired into the build (a build without it succeeds and
    publishes the placeholder),
  * no page writes the label anyway (the banner is the most copied paragraph on
    the site, so a new page gets one by being written beside a page that has
    one), and
  * the published page is compared against where it was published, by something
    outside the build -- a build has only its own branch to look at.

Each test below is one of those. They are separate because each can fail while
the other two pass.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MKDOCS_PATH = REPO_ROOT / "mkdocs.yml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docs.yml"
HOOK_PATH = REPO_ROOT / "scripts" / "docs_version.py"


def _load(name: str, filename: str):
    """Import a script by path -- `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook():
    return _load("docs_version_under_test", "docs_version.py")


# --- the hook is wired into the build --------------------------------------


def test_the_build_declares_the_hook():
    """Removing the hook is a green build that publishes the placeholder.

    Pinned textually here as well as caught by the published-tree gate, because
    this failure is free to detect before anything is built, and the tree gate
    only runs in the workflow that assembles one.
    """
    text = MKDOCS_PATH.read_text(encoding="utf-8")
    match = re.search(r"^hooks:\n(?P<entries>(?:\s+-\s+\S+\n)+)", text, re.M)
    assert match, "mkdocs.yml declares no hooks, so nothing fills in the version line"
    assert "scripts/docs_version.py" in match.group("entries"), (
        "the version-line hook is not among the build's hooks"
    )


def test_editing_the_hook_or_the_version_republishes_the_site():
    """Both inputs to the banner are on the workflow's paths filter.

    The filter is what decides whether an edit rebuilds the site at all. A hook
    that is not on it can be changed without the pages that depend on it being
    rebuilt, and the version bump that moves this branch to the next line is
    exactly the edit whose result has to reach the published pages.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    for path in ("scripts/docs_version.py", "cpersona/__init__.py"):
        assert text.count(f'- "{path}"') == 2, (
            f"{path} is missing from a paths filter: editing it would not republish the site"
        )


# --- the hook fills in what the tree states --------------------------------


def test_the_hook_states_the_line_this_tree_is_on(hook):
    """The label is the tree's own version, cut to its line."""
    import cpersona

    line = hook.line_of_version(cpersona.__version__)
    assert line, f"{cpersona.__version__!r} names no line"
    assert hook.on_page_markdown(f"applies to {hook.PLACEHOLDER}.") == (
        f"applies to {line}.x."
    )


def test_a_page_without_the_placeholder_is_returned_unchanged(hook):
    """The hook runs on every page in both languages, so it must be inert elsewhere.

    Identity rather than "no exception": a hook that reformatted the markdown it
    passed through would corrupt every page that does not carry a banner, and
    nothing downstream of it would notice.
    """
    markdown = "# Title\n\n> **Note.** Braces {like these} and {{ other_tokens }}.\n"
    assert hook.on_page_markdown(markdown) == markdown


def test_a_tree_that_states_no_version_fails_the_build(hook, tmp_path):
    """There is no sensible fallback, so it must not invent one.

    A build that cannot read its own version has nothing true to put in the
    banner. Publishing a guess is the failure this whole mechanism removes.
    """
    with pytest.raises(RuntimeError) as raised:
        hook.tree_label(tmp_path)
    assert "which line" in str(raised.value)


def test_every_banner_in_this_tree_asks_for_the_line(hook):
    """The pages really do carry the placeholder, and there really are pages.

    The gate below is a ban, and a ban proves nothing about a site that stopped
    carrying the thing banned. This is the positive half: the banners exist, and
    every one of them defers to the build.
    """
    labels = [
        match.group("label")
        for page in sorted((REPO_ROOT / "docs").rglob("*.md"))
        for match in hook.BANNER.finditer(page.read_text(encoding="utf-8"))
    ]
    assert labels, "no page carries an applies-to banner"
    assert set(labels) == {hook.PLACEHOLDER}, (
        f"a page writes its own line label: {sorted(set(labels) - {hook.PLACEHOLDER})}"
    )


# --- nothing writes the label by hand --------------------------------------


def _facts_gate(root: pathlib.Path):
    """The docs-facts gate, pointed at a tree written for one test.

    Its inputs are module globals, so they are rebound rather than passed. The
    alternative is a parameter added to production code for the sake of a test,
    which is how a check ends up with a path only tests take.
    """
    module = _load("check_docs_facts_banner", "check-docs-facts.py")
    module.ROOT = root
    module.DOC_FILES = []
    module.failures = []
    return module


def _page(root: pathlib.Path, name: str, label: str) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / name).write_text(
        f"# Page\n\n> **Applies to: CPersona {label}.** Prose follows.\n", encoding="utf-8"
    )


def test_a_hand_written_line_is_reported(hook, tmp_path):
    module = _facts_gate(tmp_path)
    _page(tmp_path, "handwritten.md", "2.5.x")

    module.check_applies_to_banners()

    assert module.failures, "a page naming its own line was accepted"
    assert "handwritten.md" in module.failures[0]
    assert hook.PLACEHOLDER in module.failures[0], (
        "the failure does not say what to write instead"
    )


def test_a_page_outside_the_scanned_list_is_still_read(hook, tmp_path):
    """The ban follows the claim, not a file list.

    A check limited to DOC_FILES would say nothing about a page added later,
    which is the page most likely to have copied the banner from a neighbour.
    DOC_FILES is empty in this fixture, so a failure here can only come from
    walking docs/.
    """
    module = _facts_gate(tmp_path)
    _page(tmp_path, "brand-new-page.md", "2.4.x")

    module.check_applies_to_banners()

    assert any("brand-new-page.md" in failure for failure in module.failures)


def test_the_placeholder_is_accepted(hook, tmp_path):
    """The positive control: the ban must not fire on the shape it demands."""
    module = _facts_gate(tmp_path)
    _page(tmp_path, "derived.md", hook.PLACEHOLDER)

    module.check_applies_to_banners()

    assert module.failures == []


def test_a_ban_with_nothing_left_to_ban_reports_itself(tmp_path):
    """An empty scan is the one result that proves nothing.

    If the banners are ever retired, this line is what makes that a deliberate
    deletion rather than a gate that silently stopped having a subject.
    """
    module = _facts_gate(tmp_path)
    (tmp_path / "docs").mkdir()

    module.check_applies_to_banners()

    assert module.failures, "a scan that found no banner at all reported success"
    assert "no subject" in module.failures[0] or "proves nothing" in module.failures[0]


# --- the published page is checked against where it was published ----------


THEMED = (
    '<html><body><header class="md-header" data-md-component="header">'
    '<div class="md-select cp-version"><button class="md-header__button">{title}</button>'
    '<ul><a href="{root}{path}" class="md-select__link" aria-current="true">{title}</a></ul>'
    "</div></header>"
    "<blockquote><p><strong>Applies to: CPersona {label}.</strong> Prose.</p></blockquote>"
    "</body></html>"
)


def _published(tree: pathlib.Path, subtree: str, label: str, root: str, title: str) -> None:
    page = tree / subtree / "faq" / "index.html" if subtree else tree / "faq" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    path = f"{subtree}/" if subtree else ""
    page.write_text(
        THEMED.format(label=label, root=root, path=path, title=title), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def selector_gate():
    return _load("check_version_selector_banner", "check-version-selector.py")


def _map(gate) -> tuple[str, str, str]:
    """The line served at the root of the real map, its title and the site root.

    Read from the map the gate itself reads, so this test does not carry a
    second copy of which lines exist -- the day a line is added, these tests
    exercise the same set the gate does.
    """
    import json

    config = json.loads(gate.CONFIG_PATH.read_text(encoding="utf-8"))
    current = config["current"]
    titles = {v["id"]: v.get("title", v["id"]) for v in config["versions"]}
    return current, titles[current], config["site_url"]


def test_a_page_published_under_another_line_is_reported(selector_gate, tmp_path, capsys):
    """The failure the whole mechanism exists for, seen from outside the build.

    A branch that has moved to the next line still builds, still renders, still
    links. Only its position in the assembled tree disagrees with what it says
    about itself, and only something reading the assembled tree can see that.
    """
    current, title, root = _map(selector_gate)
    _published(tmp_path, current, "9.9.x", root, title)

    code = selector_gate.main(["check-version-selector.py", str(tmp_path)])

    assert code == 1
    assert "says it applies to '9.9.x'" in capsys.readouterr().err


def test_an_unsubstituted_placeholder_is_reported(selector_gate, tmp_path, capsys):
    """A build with the hook switched off publishes the token to readers.

    It is reported as the wrong label rather than as a missing banner, because
    "the hook did not run" and "this page has no banner" must not look the same
    from here.
    """
    current, title, root = _map(selector_gate)
    _published(tmp_path, current, "{{ version_line }}", root, title)

    code = selector_gate.main(["check-version-selector.py", str(tmp_path)])

    assert code == 1
    assert "{{ version_line }}" in capsys.readouterr().err


def test_a_line_whose_pages_state_nothing_is_reported(selector_gate, tmp_path, capsys):
    """A tree with no banner at all is not a clean run."""
    current, title, root = _map(selector_gate)
    _published(tmp_path, current, "2.5.x", root, title)
    page = tmp_path / current / "faq" / "index.html"
    page.write_text(page.read_text(encoding="utf-8").replace("Applies to", "About"), "utf-8")

    code = selector_gate.main(["check-version-selector.py", str(tmp_path)])

    assert code == 1
    assert "no page states which line it applies to" in capsys.readouterr().err


def test_the_matching_line_is_accepted(selector_gate, tmp_path, capsys):
    """The positive control: the shape the assembler really writes passes.

    Without it, every assertion above would still hold if the banner check
    rejected everything.
    """
    current, title, root = _map(selector_gate)
    _published(tmp_path, current, f"{current}.x", root, title)

    selector_gate.main(["check-version-selector.py", str(tmp_path)])

    assert "applies to" not in capsys.readouterr().err

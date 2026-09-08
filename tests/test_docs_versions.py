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


def test_the_version_list_marks_the_build_it_is_for():
    """Exactly one row carries the marker, and it is the one asked for.

    The marker rides inside the delimited string rather than in an environment
    variable of its own because mkdocs parses an environment value as YAML: a
    bare "2.5" arrives as the float 2.5 and never equals the string "2.5". That
    was measured, not guessed, and it failed in the worst available way -- the
    selector rendered, its links worked, and only the marker saying where the
    reader was went missing, which nothing else checks.
    """
    assembler = _assembler()
    config = {
        "versions": [
            {"id": "2.5", "title": "2.5.x"},
            {"id": "2.6", "title": "2.6.x"},
        ]
    }
    rows = assembler.version_list(config, "2.6").split(";")
    assert [row.split("|")[2] for row in rows] == ["", "here"]
    assert [row.split("|")[0] for row in rows] == ["2.5", "2.6"]


def test_a_delimiter_in_a_version_field_is_refused(tmp_path):
    """A delimiter inside a field would split one row into two, silently.

    The list reaches the selector as one string joined on ";" and "|", so a field
    containing either does not produce a broken string -- it produces a different,
    plausible list. Refused at the map instead, where the message can name the
    field.
    """
    assembler = _assembler()
    bad = tmp_path / "docs-versions.json"
    bad.write_text(
        json.dumps(
            {
                "site_url": "https://example.test/",
                "current": "2.5",
                "versions": [{"id": "2.5", "title": "2.5|beta", "branch": "master"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(assembler.BuildError, match="delimiter"):
        assembler.load_config(bad)


def test_the_themes_own_version_selector_stays_off():
    """Setting `extra.version` would switch Material's selector on, and it is wrong here.

    It derives its base from the last path segment of the URL, so under /2.5/ja/
    it reads "ja" as a version name and fetches a versions.json that is not
    there. The Japanese pages would carry a broken selector and no gate would
    say so, which is why the absence of this one key is worth an assertion.
    """
    text = MKDOCS_PATH.read_text(encoding="utf-8")
    assert not re.search(r"^\s{2}version:\s", text, re.M), (
        "mkdocs.yml sets extra.version: Material's own version selector is now on"
    )


# The version selector: where it is rendered, and what its placement costs.
#
# It is rendered into the site header by overrides/partials/alternate.html.
# Overriding that partial is the only way into the header -- partials/header.html
# carries no block to extend -- and it buys the placement with two liabilities
# that nothing else in this repository would notice: a verbatim copy of a theme
# file, and a dependency on the theme including that file at all.

ALTERNATE_PATH = REPO_ROOT / "overrides" / "partials" / "alternate.html"
MAIN_TEMPLATE_PATH = REPO_ROOT / "overrides" / "main.html"
BUILD_SCRIPT_PATH = REPO_ROOT / "scripts" / "build-docs.sh"


def _version_block(partial: str) -> str:
    """The version control's own markup, cut out of the partial.

    The file holds two controls built from the same theme classes: this one and
    a verbatim copy of the language selector. Anything asserted against the
    whole file is answered by whichever of the two still satisfies it.
    """
    start = partial.index('class="md-select cp-version"')
    return partial[start:partial.index("</ul>", start)]


def test_the_version_selector_is_rendered_into_the_header():
    """It is in the header partial, and not in the announcement banner.

    The banner was where it started, and the banner sits above a sticky header
    and scrolls away with the page: past the first screenful nothing said which
    version was being read, which is most of what the control is for. Pinned
    because "put it back in the banner" is a one-line change that looks like a
    simplification -- the banner block needs no override of a theme partial.
    """
    partial = ALTERNATE_PATH.read_text(encoding="utf-8")
    assert 'class="md-select cp-version"' in partial, (
        "the version selector is no longer rendered in the header partial"
    )
    # Read the version control on its own. The file also holds a copy of the
    # theme's language selector, which is built from the same classes -- so an
    # unscoped `in partial` is satisfied by that copy no matter what happens to
    # the version control. Measured: replacing md-select__inner in the version
    # block left this test green until it was narrowed to the block.
    block = _version_block(partial)
    assert 'class="md-select__inner"' in block, (
        "the version list is no longer built on md-select: md-version's list carries no "
        "top or left, so from this position it opens on top of its own button"
    )
    assert "hreflang" not in block, (
        "a version row carries hreflang: the language routing would file a click on it "
        "as a choice of language and honour it on every page after"
    )
    main = MAIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\{%-?\s*block\s+announce\s*-?%\}", main), (
        "main.html defines the announcement banner again: the selector belongs in the header"
    )


def test_the_copied_theme_file_names_the_theme_the_build_installs():
    """The copy states which theme version it was taken from, and it is the pinned one.

    Half of overrides/partials/alternate.html is a verbatim copy of the theme's
    own partial, which is what an override costs when the file being overridden
    has no block to extend. A copy is only safe while it is a copy of what is
    actually installed: bump the pin and the header silently keeps rendering the
    older theme's markup, with no build failure and nothing visibly wrong.

    Checked against scripts/build-docs.sh rather than the workflow, because that
    script is what every published tree is built by -- the workflow's own pin
    builds the review tree, which is not the one readers get.
    """
    pinned = re.search(r"mkdocs-material==(?P<version>[\w.]+)", BUILD_SCRIPT_PATH.read_text(encoding="utf-8"))
    assert pinned, "scripts/build-docs.sh no longer pins mkdocs-material"
    claimed = re.search(r"mkdocs-material (?P<version>\d[\w.]*)", ALTERNATE_PATH.read_text(encoding="utf-8"))
    assert claimed, "the header partial no longer says which theme version it was copied from"
    assert claimed.group("version") == pinned.group("version"), (
        f"the header partial was copied from mkdocs-material {claimed.group('version')}, "
        f"but the build installs {pinned.group('version')}: re-sync the copy"
    )


def test_the_copied_language_selector_keeps_what_the_routing_reads():
    """The routing finds a reader's explicit choice through markup this copy now owns.

    overrides/main.html records a language as chosen by watching for a click on
    an .md-select__link and reading its hreflang. Both attributes used to be the
    theme's to guarantee; they are ours since the copy. Paraphrase either away
    and the selector still renders, still navigates, and silently stops
    remembering -- the reader is bounced back to their browser's language on the
    next page, which reads as the site ignoring them.
    """
    partial = ALTERNATE_PATH.read_text(encoding="utf-8")
    main = MAIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    for rendered, read_back in (
        ('class="md-select__link"', "md-select__link"),
        ('hreflang="', "hreflang"),
    ):
        assert rendered in partial, f"the copied language selector dropped {rendered}"
        assert read_back in main, (
            f"main.html no longer reads {read_back}: this pin is watching the wrong pair"
        )


def test_the_selector_gate_runs_on_the_tree_the_assembler_writes():
    """The gate is wired to the built tree, and editing it runs it.

    Two failures in one: a gate pointed at a directory the assembly does not
    write reports nothing and exits 0, and a checker missing from the trigger's
    paths list is the one file whose edit cannot be tested -- the workflow says
    so itself, above those lists.
    """
    text = _workflow_text()
    assert text.count('- "scripts/check-version-selector.py"') == 2, (
        "the selector gate is missing from a paths filter: editing it would not run it"
    )
    invocation = re.search(r"check-version-selector\.py (?P<dir>\S+)", text)
    assert invocation, "the build job no longer runs the selector gate"
    written = re.search(r"build-all-versions\.py\s*\n?\s*--out (?P<dir>\S+)", text)
    assert written, "the assembly step no longer passes --out"
    assert invocation.group("dir") == written.group("dir"), (
        f"the gate reads {invocation.group('dir')!r} but the assembler writes {written.group('dir')!r}"
    )


def _version_map_gate():
    """Import the version-map gate by path — `scripts/` is not a package."""
    path = REPO_ROOT / "scripts" / "check-version-map.py"
    spec = importlib.util.spec_from_file_location("check_version_map_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _site_link_gate():
    """Import the site-link gate by path — `scripts/` is not a package."""
    path = REPO_ROOT / "scripts" / "check-site-urls.py"
    spec = importlib.util.spec_from_file_location("check_site_urls_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _support_table(rows: str) -> str:
    return (
        "# Support\n\n## Status\n\n"
        "| Line | Tier | Frozen | Decision | Notes |\n| --- | --- | --- | --- | --- |\n"
        f"{rows}\n\n## Known issues\n\nnone\n"
    )


def _written_support(tmp_path, rows: str):
    path = tmp_path / "SUPPORT.md"
    path.write_text(_support_table(rows), encoding="utf-8")
    return path


_TWO_LINES = "| 2.4.x | **Stable** | — | certified | . |\n| 2.5.x | **Current** | not yet | — | . |"


def _map(**overrides) -> dict:
    config = {
        "current": "2.5",
        "versions": [{"id": "2.5", "title": "2.5.x", "branch": "master"}],
        "lines_without_docs": [{"line": "2.4.x", "reason": "no mkdocs tree on that branch"}],
    }
    config.update(overrides)
    return config


def test_a_map_that_agrees_with_the_support_table_reports_nothing(tmp_path):
    """The positive control.

    Without it, every assertion below is satisfied by a checker that reports on
    everything -- which is the same evidence as a checker that works.
    """
    gate = _version_map_gate()
    problems = gate.check_declarations(_map(), "map.json", _written_support(tmp_path, _TWO_LINES))
    assert problems == [], problems


def test_a_supported_line_must_be_published_or_declared_undocumented(tmp_path):
    """Silence is the failure: a line nobody published and nobody excused.

    The exclusion of 2.4.x is real and permanent -- that branch has no mkdocs
    tree to build -- so this cannot be a check that every supported line is
    published. What it can require is that the omission was written down, which
    is what tells it apart from forgetting a line that does have documentation.
    """
    gate = _version_map_gate()
    problems = gate.check_declarations(
        _map(lines_without_docs=[]), "map.json", _written_support(tmp_path, _TWO_LINES)
    )
    assert any("neither published nor declared" in problem for problem in problems), problems


def test_an_exemption_without_a_reason_is_not_an_exemption(tmp_path):
    gate = _version_map_gate()
    problems = gate.check_declarations(
        _map(lines_without_docs=[{"line": "2.4.x", "reason": "   "}]),
        "map.json",
        _written_support(tmp_path, _TWO_LINES),
    )
    assert any("with no reason" in problem for problem in problems), problems


def test_an_exemption_outliving_its_line_is_reported(tmp_path):
    """An exemption for a line the table no longer has stops excusing anything.

    Left in place it is worse than absent: the next line that needs excusing
    reads as already covered.
    """
    gate = _version_map_gate()
    problems = gate.check_declarations(
        _map(lines_without_docs=[{"line": "1.9.x", "reason": "gone"}]),
        "map.json",
        _written_support(tmp_path, _TWO_LINES),
    )
    assert any("outlived its subject" in problem for problem in problems), problems


def test_the_root_must_serve_a_line_the_table_calls_current(tmp_path):
    """Not "the only Current line" -- v1.4 of the standard removed that exclusivity."""
    gate = _version_map_gate()
    rows = "| 2.4.x | **Stable** | — | certified | . |\n| 2.5.x | **Candidate** | not yet | — | . |"
    problems = gate.check_declarations(_map(), "map.json", _written_support(tmp_path, rows))
    assert any("Current tier" in problem for problem in problems), problems

    both = _TWO_LINES + "\n| 2.6.x | **Current** | not yet | — | . |"
    relaxed = gate.check_declarations(
        _map(lines_without_docs=[
            {"line": "2.4.x", "reason": "no mkdocs tree"},
            {"line": "2.6.x", "reason": "not published yet"},
        ]),
        "map.json",
        _written_support(tmp_path, both),
    )
    assert relaxed == [], relaxed


def test_a_line_cannot_be_published_and_excused_at_once(tmp_path):
    gate = _version_map_gate()
    problems = gate.check_declarations(
        _map(lines_without_docs=[{"line": "2.5.x", "reason": "contradiction"}]),
        "map.json",
        _written_support(tmp_path, _TWO_LINES),
    )
    assert any("both as a published version" in problem for problem in problems), problems


def test_an_unreadable_status_table_fails_rather_than_passes(tmp_path):
    """A parse that finds no rows would satisfy every comparison above."""
    gate = _version_map_gate()
    path = tmp_path / "SUPPORT.md"
    path.write_text("# Support\n\nNo table here.\n", encoding="utf-8")
    problems = gate.check_declarations(_map(), "map.json", path)
    assert problems and "no Status table" in problems[0], problems


def test_the_version_map_gate_is_reachable_from_a_support_md_edit():
    """It runs in CI, and in the workflow whose trigger SUPPORT.md can reach.

    docs.yml filters on paths and SUPPORT.md is on that list only for the link
    gate; the map gate has to run for edits that touch neither docs/ nor the
    map, so it lives in ci.yml, whose pull_request trigger has no paths filter
    at all. If that filter is ever added, this gate stops running on exactly the
    edits it exists for -- silently.
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # Four spaces, not "any indentation": a job's own keys are indented deeper
    # than the job name, so a laxer body pattern runs on through the next job.
    # Measured while mutating this test -- with `[ \t]+` the body reached the
    # docs-facts job below and read *its* fetch-depth, leaving this assertion
    # green with this job's checkout gone shallow.
    job = re.search(r"^  version-map:\n(?P<body>(?:[ ]{4,}.*\n|\n)*)", ci, re.M)
    assert job, "ci.yml has no version-map job"
    body = job.group("body")
    assert "scripts/check-version-map.py" in body, "the version-map job no longer runs the gate"
    # Scoped to this job on purpose: `fetch-depth: 0` appears elsewhere in the
    # file, so a search over the whole workflow would stay green with this job's
    # checkout gone shallow -- and a shallow checkout carries no tags, which is
    # half of what the gate reads.
    assert "fetch-depth: 0" in body, (
        "the version-map job's checkout is shallow: it carries neither the tags nor the "
        "branches the gate reads"
    )

    trigger = re.search(r"^on:\n(?P<body>(?:[ \t]+.*\n|\n)*?)^\S", ci, re.M)
    assert trigger, "ci.yml declares no triggers"
    inside_pr = False
    for line in trigger.group("body").splitlines():
        if re.match(r"^  \S", line):
            inside_pr = line.strip().startswith("pull_request:")
            continue
        if inside_pr and line.strip().startswith("paths"):
            raise AssertionError(
                "ci.yml's pull_request trigger grew a paths filter: the version-map gate "
                "would stop running on the SUPPORT.md edits it exists to catch"
            )


def _trigger_paths(text: str, trigger: str) -> list[str]:
    """The `paths:` entries under one trigger in a workflow, read textually."""
    block = re.search(
        rf"^  {trigger}:\n(?P<body>(?:    .*\n|\n)*)", text, re.M
    )
    assert block, f"no {trigger} trigger found"
    return re.findall(r'^      - "(?P<path>[^"]+)"', block.group("body"), re.M)


def _glob_matches(pattern: str, path: str) -> bool:
    regex = re.escape(pattern).replace(r"\*\*", "\x00").replace(r"\*", "[^/]*").replace("\x00", ".*")
    return re.fullmatch(regex, path) is not None


def test_every_file_publishing_a_site_link_is_on_the_docs_trigger():
    """The gate is only as wide as the trigger that runs it.

    check-site-urls.py scans the whole repository on purpose -- a hand-kept list
    of files that publish links produces, when it falls behind, a new file whose
    links nothing checks, which looks exactly like success. The workflow's paths
    filter is such a hand-kept list, one level up: a publisher outside it is
    scanned only when some other file drags the workflow into running. This
    turns that into a red suite instead of a silent gap.
    """
    gate = _site_link_gate()
    site_url = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["site_url"]
    publishers = sorted(
        {str(path.relative_to(REPO_ROOT)) for path, _, _ in gate.links(re.compile(re.escape(site_url) + gate.URL_BODY))}
    )
    assert publishers, "no file publishes a site link — the scan is broken, not the repository"

    text = _workflow_text()
    for trigger in ("push", "pull_request"):
        patterns = _trigger_paths(text, trigger)
        missing = [
            publisher
            for publisher in publishers
            if not any(_glob_matches(pattern, publisher) for pattern in patterns)
        ]
        assert not missing, (
            f"these files publish links into the site but are not on docs.yml's {trigger} "
            f"paths filter, so editing one does not run the link gate: {missing}"
        )


def test_the_site_link_gate_runs_on_the_tree_the_assembler_writes():
    """Same pair of failures as the selector gate: wrong directory, or never run."""
    text = _workflow_text()
    assert text.count('- "scripts/check-site-urls.py"') == 2, (
        "the link gate is missing from a paths filter: editing it would not run it"
    )
    invocation = re.search(r"check-site-urls\.py (?P<dir>\S+)", text)
    assert invocation, "the build job no longer runs the link gate"
    written = re.search(r"build-all-versions\.py\s*\n?\s*--out (?P<dir>\S+)", text)
    assert written, "the assembly step no longer passes --out"
    assert invocation.group("dir") == written.group("dir"), (
        f"the gate reads {invocation.group('dir')!r} but the assembler writes "
        f"{written.group('dir')!r}"
    )

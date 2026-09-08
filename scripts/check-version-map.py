#!/usr/bin/env python3
"""The version map, the support table and the git tags describe the same lines.

`docs-versions.json` decides what the published site contains: one subtree per
declared line, built from that line's branch. Nothing in that file is checked by
building it -- an assembly succeeds whenever the branches it names can be built,
which is true of a map that names the wrong branches. The two ways it goes wrong
are both silent:

  * The map and `SUPPORT.md` drift apart. The support table is what a reader is
    told about tiers and end-of-life; the map is what they are served. A line
    that is Current in one and absent from the other is not a broken build, it
    is a site that answers a question differently depending on which page the
    reader lands on.

  * A branch moves to another line underneath the map. `2.5` is served from
    `master` today; the day `master` bumps to a 2.6 version, `/2.5/` starts
    publishing 2.6 documentation under a 2.5 label, and every other gate stays
    green -- the tree builds, the selector renders, the links resolve. This is
    the failure the version map's own comment promises is covered "by a gate
    comparing the version a tree claims against that line's newest tag", and
    this is that gate.

What it deliberately does NOT check: whether a page's body text names the right
patch version. Pages state their line ("2.5.x"), never their patch -- a patch
label would be wrong between a cut and the next bump, and nothing about a page
changes per patch. So the agreement checked here is at line granularity, which
is the granularity at which a tree names a version at all.

The banner that states the line is filled in at build time from the same two
places this reads (scripts/docs_version.py), and whether the published page ends
up under the line it names is checked against the assembled tree by
scripts/check-version-selector.py. This gate is the one that can see the branch
and the tags, and that is the half it answers.

Reads the version a branch is on from that branch, and which ref that is from
the assembler, so the gate cannot check a different ref than the one the site is
built from. Exit 0 when everything agrees; exit 1 with one line per
disagreement.

Usage: check-version-map.py [--as-branch BRANCH]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from docs_version import LINE_PREFIX, VERSION_SOURCES  # noqa: E402 — sibling script

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "docs-versions.json"
SUPPORT_PATH = ROOT / "SUPPORT.md"
ASSEMBLER_PATH = ROOT / "scripts" / "build-all-versions.py"
# VERSION_SOURCES and LINE_PREFIX are imported, not declared: the documentation
# build reads the same two places to fill in the line each page says it applies
# to (scripts/docs_version.py). Two implementations of "where a tree states its
# version" would let this gate agree with a map the pages disagree with, which
# is the same class of green-and-meaningless as reading a different ref than the
# site is built from.
STATUS_HEADING = re.compile(r"^##\s+Status\s*$")
NEXT_HEADING = re.compile(r"^##\s")
EMPHASIS = re.compile(r"[*_`]")


def load_assembler():
    """The assembler module, imported for the one function that must not be copied.

    `resolve_ref` decides which commit a line is published from -- the remote
    branch, fetched if absent, and a local branch only when there is no remote
    one. A second implementation of that rule here would let the gate check a
    ref the site is not built from, which is the one way this check could be
    green and meaningless at the same time.
    """
    spec = importlib.util.spec_from_file_location("build_all_versions_for_gate", ASSEMBLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {ASSEMBLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args: str) -> str:
    result = subprocess.run(
        ("git",) + args, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git " + " ".join(args) + " failed: " + (result.stderr.strip() or "(no output)")
        )
    return result.stdout.strip()


def cells(row: str) -> list[str]:
    """The cells of a markdown table row, without the outer pipes."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def status_rows(text: str) -> list[list[str]]:
    """The rows of the Status table in SUPPORT.md, header and rule excluded.

    Parsed rather than imported because SUPPORT.md is prose for readers, not a
    data file: it is edited by hand, and the point of this gate is to catch a
    hand edit that no longer matches the map.
    """
    rows: list[list[str]] = []
    in_section = False
    for raw in text.splitlines():
        if STATUS_HEADING.match(raw):
            in_section = True
            continue
        if in_section and NEXT_HEADING.match(raw):
            break
        if not in_section or not raw.lstrip().startswith("|"):
            continue
        row = cells(raw)
        if not row or set("".join(row)) <= set("-: "):
            continue  # the rule under the header
        rows.append(row)
    return rows[1:] if rows else rows  # drop the header row


def tier_of(row: list[str]) -> str:
    return EMPHASIS.sub("", row[1]).strip() if len(row) > 1 else ""


def line_of(row: list[str]) -> str:
    return EMPHASIS.sub("", row[0]).strip() if row else ""


def declared_version(module, branch: str, here: str | None) -> tuple[str | None, str, str]:
    """The version a line's branch is on, the ref it was read from, and where.

    Read from the same place the assembler builds from: the checkout itself when
    this is the branch we are on, and the resolved remote ref otherwise. A gate
    that always read the remote would pass a pull request that moves the line,
    because the move is not on the remote branch yet.
    """
    here_is_it = here is not None and branch == here
    ref = "HEAD" if here_is_it else module.resolve_ref(branch)

    for path, pattern in VERSION_SOURCES:
        if here_is_it:
            source = ROOT / path
            text = source.read_text(encoding="utf-8") if source.is_file() else ""
        else:
            try:
                text = git("show", f"{ref}:{path}")
            except RuntimeError:
                continue  # a line that does not carry this file states it elsewhere
        found = pattern.search(text)
        if found:
            return found.group("version"), ref, path

    return None, ref, "/".join(path for path, _ in VERSION_SOURCES)


def newest_tag(line: str) -> str | None:
    """The most recently created `v<line>.*` tag, or None when the line has none.

    Sorted by creation date rather than by version, because "newest" here means
    the last release cut on the line, and because a version sort has to decide
    what to do with the pre-release suffixes this project uses heavily
    (`2.5.12b3`) -- a decision with no bearing on the question being asked.
    """
    listed = git("tag", "--list", f"v{line}.*", "--sort=-creatordate").splitlines()
    return listed[0].strip() if listed else None


def check_declarations(config: dict, config_name: str, support_path: pathlib.Path) -> list[str]:
    """Everything the map and the support table say about each other.

    Separated from the git half so it can be exercised against a written-out map
    and support table: the failures here are the ones a person introduces by
    editing one file and not the other, and a detector for them that is only
    ever run against the correct pair is a detector nobody has seen work.
    """
    problems: list[str] = []
    versions = config["versions"]
    titles = {version["id"]: version.get("title", version["id"]) for version in versions}

    for identifier, title in sorted(titles.items()):
        # The id is a path segment and the title is what a reader sees; letting
        # them describe different lines would make every message below ambiguous
        # about which of the two the site is actually serving.
        if not title.startswith(identifier):
            problems.append(
                f"{config_name}: line {identifier!r} is titled {title!r}, which is not "
                f"a label for that line"
            )

    rows = status_rows(support_path.read_text(encoding="utf-8"))
    if not rows:
        # An empty parse is the one outcome that proves nothing: every
        # comparison below would pass against a table this could not find.
        return [f"{support_path.name}: no Status table found, so nothing could be compared"]

    supported = {line_of(row): tier_of(row) for row in rows}
    current_id = config["current"]
    current_title = titles[current_id]

    # Not "the only Current row": v1.4 of the release standard removed the
    # exclusivity of the Current tier on purpose, so a table with two of them is
    # a supported state and must not be read as a defect here.
    current_lines = sorted(line for line, tier in supported.items() if tier == "Current")
    if not current_lines:
        problems.append(f"{support_path.name}: no line is on the Current tier")
    elif current_title not in current_lines:
        problems.append(
            f"the site serves {current_title!r} at its root, but the Current tier in "
            f"{support_path.name} is {', '.join(current_lines)}"
        )

    for identifier, title in sorted(titles.items()):
        if title not in supported:
            problems.append(
                f"{config_name}: line {title!r} is published, but {support_path.name} "
                f"has no row for it"
            )

    # The reverse direction is not an error, and must not become one: 2.4.x has
    # no mkdocs tree on its branch and therefore cannot be assembled at all, so
    # requiring every supported line to be published would make red the normal
    # state of this gate. What is required instead is that the omission is
    # stated -- a silent exclusion is indistinguishable from forgetting to
    # publish a line that does have documentation.
    exempt = {entry.get("line"): entry.get("reason", "") for entry in config.get("lines_without_docs", [])}
    for line, reason in sorted(exempt.items()):
        if line in titles.values():
            problems.append(
                f"{config_name}: {line!r} is declared both as a published version and as "
                f"a line without documentation"
            )
        elif line not in supported:
            problems.append(
                f"{config_name}: {line!r} is exempted from publishing, but "
                f"{support_path.name} has no such line -- the exemption outlived its subject"
            )
        elif not reason.strip():
            problems.append(f"{config_name}: {line!r} is exempted from publishing with no reason")

    for line in sorted(supported):
        if line not in titles.values() and line not in exempt:
            problems.append(
                f"{support_path.name}: line {line!r} is neither published nor declared in "
                f"{config_name} as a line without documentation"
            )

    return problems


def check_branches(module, config: dict, here: str | None) -> list[str]:
    """That each line is published from a branch still on that line, and released.

    The git half. Kept apart from the declarations above because it needs the
    repository's refs and tags -- a shallow checkout can answer none of it, and
    a caller that has one should get that as a failure of this check rather than
    as a silent absence inside a larger one.
    """
    problems: list[str] = []
    for version in config["versions"]:
        identifier, branch = version["id"], version["branch"]
        try:
            stated, ref, where = declared_version(module, branch, here)
        except (RuntimeError, OSError) as exc:
            problems.append(f"{identifier}: cannot read a version from {branch}: {exc}")
            continue

        prefix = LINE_PREFIX.match(stated) if stated is not None else None
        if stated is None:
            problems.append(f"{identifier}: {ref} states no version in {where}")
        elif not prefix:
            problems.append(f"{identifier}: {ref} states version {stated!r}, which names no line")
        elif prefix.group("line") != identifier:
            problems.append(
                f"{identifier}/ is built from {branch}, which is on the "
                f"{prefix.group('line')} line ({where} states {stated}) -- the subtree would "
                f"publish {prefix.group('line')} documentation under a {identifier} label"
            )

        tag = newest_tag(identifier)
        if tag is None:
            problems.append(
                f"{identifier}: no v{identifier}.* tag exists, so the site would publish a "
                f"line that has never been released"
            )
            continue
        target = "HEAD" if ref == "HEAD" else ref
        reachable = subprocess.run(
            ("git", "merge-base", "--is-ancestor", tag, target),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if reachable.returncode != 0:
            problems.append(
                f"{identifier}: the newest tag on the line ({tag}) is not in {branch} "
                f"({ref}), so the published tree is missing released content"
            )

    return problems


def check(as_branch: str | None) -> list[str]:
    module = load_assembler()
    config = module.load_config(CONFIG_PATH)
    here = as_branch or module.current_branch()
    return check_declarations(config, CONFIG_PATH.name, SUPPORT_PATH) + check_branches(
        module, config, here
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--as-branch",
        default=None,
        metavar="BRANCH",
        help=(
            "treat this checkout as that branch, matching the assembler's own option. A "
            "pull request is checked out as a detached merge ref that is on no branch, so "
            "without it the line under review is read from its published content and the "
            "change being reviewed is not examined at all."
        ),
    )
    args = parser.parse_args(argv)

    try:
        problems = check(args.as_branch)
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} disagreement(s)", file=sys.stderr)
        return 1

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    served = ", ".join(sorted(version["id"] for version in config["versions"]))
    print(f"version map: {served} agree with {SUPPORT_PATH.name} and with the tags on each line")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

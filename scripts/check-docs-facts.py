#!/usr/bin/env python3
"""Verify that measurable facts stated in the docs match the source of truth.

Hand-written numbers rot: the bundled skill said "28 tools / ~5,600 LOC" for
months after both had grown (caught by a production user, 2026-08-21), and the
README's test counts drifted within a day of being measured. This gate makes
the drift visible in CI instead of waiting for a reader to trip over it.

Checked facts and their sources of truth:

  tool count      runtime registry (import cpersona.server, count registered
                  tools) — NOT grep: a grep of `auto_tool(` has already
                  miscounted once (matched a non-registration line)
  schema version  SCHEMA_VERSION literal in cpersona/database.py
  env defaults    static parse of cpersona/config.py, compared against every
                  markdown table row in docs/ that names a `CPERSONA_*` var
  env names       every `CPERSONA_*` token in the checked pages, wherever it is
                  written, must be a name the package actually reads — the
                  table-row reading above left inline bullets and code fences
                  unchecked, which is where the broken-install reference page
                  writes all of its variables
  latest release  the newest final `vX.Y.Z` git tag, compared against every
                  "latest v2.5.4" / "Latest release: 2.5.4" claim. Version
                  freshness is the one fact that rots on a schedule — it goes
                  stale the moment a tag is cut, and it went stale twice
                  (README and SUPPORT both named a superseded release) before
                  this check existed
  volatile stats  LOC / test-function / test-module / collected-case counts,
                  re-measured here with the same commands the docs cite;
                  docs state them as rounded `~` values and this check allows
                  TOLERANCE relative drift before failing, so routine commits
                  do not churn the docs while real drift still trips the gate

Exit 0 when every claim holds; exit 1 with one line per violation otherwise.
Run from the repository root (CI does): `uv run python scripts/check-docs-facts.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Docs surfaces that claim CURRENT state and therefore must track the code.
# *_DESIGN.md / *_STANDARD.md are point-in-time design records — statements
# like "(24 → 25 tools)" describe the moment a design shipped and are allowed
# to stay as written — so they are deliberately not scanned.
DOC_FILES = [
    ROOT / "README.md",
    ROOT / "SUPPORT.md",
    ROOT / "skills" / "cpersona-memory" / "SKILL.md",
    # The skill's on-demand reference. It ships on the user's disk with the
    # package exactly as SKILL.md does, and it is the page that names commands,
    # environment variables and endpoints for someone whose install is already
    # broken — the reader least able to absorb a stale instruction.
    ROOT / "skills" / "cpersona-memory" / "references" / "embedding-backend-repair.md",
    # The other on-demand reference: it names the store call and the tool a
    # reader pairs with a client memory file, and it is read while someone is
    # about to move a corpus, so a stale argument name there costs data.
    ROOT / "skills" / "cpersona-memory" / "references" / "always-loaded-index.md",
    ROOT / "docs" / "index.md",
    ROOT / "docs" / "getting-started.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "tools.md",
    ROOT / "docs" / "behavior-contracts.md",
    ROOT / "docs" / "operations.md",
    ROOT / "docs" / "configuration.md",
    ROOT / "docs" / "faq.md",
    ROOT / "docs" / "quality-assurance.md",
    ROOT / "docs" / "llms.txt",
    # Translations restate the same measurable claims, so they rot the same
    # way. The checks below key on `CPERSONA_*` cells and on the numbers
    # themselves, not on English prose, so a ja page is scanned identically.
    ROOT / "docs" / "index.ja.md",
    ROOT / "docs" / "faq.ja.md",
    ROOT / "docs" / "behavior-contracts.ja.md",
    ROOT / "docs" / "operations.ja.md",
    ROOT / "docs" / "configuration.ja.md",
    ROOT / "docs" / "getting-started.ja.md",
    ROOT / "docs" / "architecture.ja.md",
    ROOT / "docs" / "tools.ja.md",
    ROOT / "docs" / "quality-assurance.ja.md",
]

# Packaging manifests are not docs, but they carry the same current-state
# claims to a wider audience than any page here: pyproject's description is
# what PyPI shows and what the repository's own description was copied from,
# and server.json is the registry entry. Both drifted to a stale tool count
# while every scanned page stayed correct (2026-08-31) — the count was right
# everywhere the gate could see and wrong everywhere else, which is what an
# exemption that follows paths rather than claims produces.
MANIFEST_FILES = [
    ROOT / "pyproject.toml",
    ROOT / "server.json",
]

# Relative drift allowed on volatile stats before the gate goes red. 3% keeps
# routine test additions from forcing a doc edit per commit, while an 8% drift
# like the one this gate was built for (13,000 vs 14,129 LOC) still fails.
TOLERANCE = 0.03

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


# --- sources of truth -------------------------------------------------------


def measured_tool_count() -> int:
    sys.path.insert(0, str(ROOT))
    import cpersona.server as server  # noqa: PLC0415 — deliberate late import

    return len(server.registry._tools)


def measured_project_version() -> str:
    # 2.5.8: the version moved into the package and pyproject derives it from
    # there (`[tool.hatch.version]`), so the source of truth is the module. Read
    # it as text rather than importing: this script also runs where the package
    # is not installed, and a literal is what the build itself parses.
    text = (ROOT / "cpersona" / "__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not m:
        fail("cpersona/__init__.py: __version__ literal not found — update this script")
        return ""
    return m.group(1)


def measured_schema_version() -> int:
    text = (ROOT / "cpersona" / "database.py").read_text()
    m = re.search(r"^SCHEMA_VERSION = (\d+)$", text, re.M)
    if not m:
        fail("database.py: SCHEMA_VERSION literal not found — update this script")
        return -1
    return int(m.group(1))


def measured_axis_acceptance() -> dict[str, list[str]]:
    """{axis: sorted tool names whose input schema offers it}, from the registry.

    The three isolation axes are offered unevenly and the docs state the counts,
    so the counts are a schema-derived fact rather than prose. Measured off the
    same registry `measured_tool_count` reads, which is what the server serves.
    """
    sys.path.insert(0, str(ROOT))
    import cpersona.server as server  # noqa: PLC0415 — deliberate late import

    tools = server.registry._tools
    items = tools.items() if isinstance(tools, dict) else [(t.name, t) for t in tools]
    return {
        axis: sorted(
            name
            for name, tool in items
            if axis in ((tool.inputSchema or {}).get("properties") or {})
        )
        for axis in ("agent_id", "project_id", "channel")
    }


def measured_calibrate_default() -> str:
    """The calibration method a bare `calibrate_threshold` call resolves to."""
    text = (ROOT / "cpersona" / "config.py").read_text()
    m = re.search(r'CALIBRATE_METHOD = os\.environ\.get\("CPERSONA_CALIBRATE_METHOD", "(\w+)"\)', text)
    if not m:
        fail("config.py: CALIBRATE_METHOD default not found — update this script")
        return ""
    return m.group(1)


def measured_embed_batch() -> int:
    """The largest number of texts CPersona puts in one /embed request.

    Read from the named constant rather than from a literal in a slice, so the
    check keeps measuring the same thing if the batching loop is rewritten.
    """
    text = (ROOT / "cpersona" / "checks.py").read_text()
    m = re.search(r"^EMBED_BATCH_SIZE = (\d+)$", text, re.M)
    if not m:
        fail("checks.py: EMBED_BATCH_SIZE literal not found — update this script")
        return -1
    return int(m.group(1))


def parsed_env_defaults() -> dict[str, str]:
    """Static parse of config.py → {VAR_NAME: normalized default}.

    Handles the four assignment shapes config.py uses. Vars read through any
    other shape are simply absent here and fall back to an existence-only
    check against the package source -- which is a silent hole, not a feature:
    the documented default of such a var is never compared to the code. When a
    new shape appears in config.py, it belongs here in the same change, or the
    table row it produces is prose that no gate reads.
    """
    text = (ROOT / "cpersona" / "config.py").read_text()
    defaults: dict[str, str] = {}
    for m in re.finditer(r'_parse_int\("(CPERSONA_\w+)",\s*(-?\d+)\)', text):
        defaults[m.group(1)] = m.group(2)
    for m in re.finditer(r'_parse_float\("(CPERSONA_\w+)",\s*(-?[\d.]+)\)', text):
        defaults[m.group(1)] = m.group(2)
    for m in re.finditer(
        r'os\.environ\.get\("(CPERSONA_\w+)"\s*,\s*"([^"]*)"\)(\.lower\(\)\s*==\s*"true")?',
        text,
    ):
        defaults[m.group(1)] = m.group(2)
    # A word-valued setting: the default is the second argument, and the tuple
    # after it is the closed set of accepted values.
    for m in re.finditer(
        r'_parse_choice\(\s*"(CPERSONA_\w+)",\s*"([^"]*)"', text
    ):
        defaults[m.group(1)] = m.group(2)
    return defaults


def measured_volatile_stats() -> dict[str, float]:
    def loc(paths: list[Path]) -> int:
        return sum(len(p.read_text().splitlines()) for p in paths)

    server_loc = loc(sorted((ROOT / "cpersona").glob("*.py")))
    vendored_loc = loc(sorted((ROOT / "cpersona" / "_vendored_mcp_common").rglob("*.py")))
    test_files = sorted((ROOT / "tests").glob("*.py"))
    test_loc = loc(test_files)
    test_funcs = sum(
        len(re.findall(r"\bdef test_", p.read_text())) for p in test_files
    )
    test_modules = len([p for p in test_files if p.name.startswith("test_")])

    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    m = re.search(r"(\d+) tests collected", collect.stdout)
    if not m:
        fail(
            "pytest --collect-only did not report a test count "
            f"(rc={collect.returncode}) — cannot verify the collected-case claim"
        )
        cases = -1
    else:
        cases = int(m.group(1))

    return {
        "server LOC": server_loc,
        "vendored LOC": vendored_loc,
        "test LOC": test_loc,
        "test functions": test_funcs,
        "test modules": test_modules,
        "collected cases": cases,
    }


# --- claims found in the docs ----------------------------------------------


def check_tool_and_schema_claims(tool_count: int, schema_version: int) -> None:
    for doc in DOC_FILES + MANIFEST_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        # Both spellings, because the translations restate the same count and a
        # detector that only knows the English phrase passes them vacuously —
        # which is indistinguishable from passing them correctly.
        for pattern in (r"\b(\d+) tools\b", r"(\d+) 個のツール"):
            for m in re.finditer(pattern, text):
                if int(m.group(1)) != tool_count:
                    fail(f"{rel}: claims '{m.group(0)}' but the registry serves {tool_count}")
        for m in re.finditer(r"\b[Ss]chema v(\d+)\b", text):
            if int(m.group(1)) != schema_version:
                fail(f"{rel}: claims '{m.group(0)}' but SCHEMA_VERSION is {schema_version}")


def check_manifest_version(project_version: str) -> None:
    """The registry manifest must name the version this tree builds.

    server.json is published to the MCP registry by hand, so nothing in CI
    ever forced it to move: it was last caught up to 2.5.4 and sat there
    through two finals while pyproject went on. Tying it to pyproject makes
    the catch-up automatic — the release commit that bumps one has to bump
    the other or this fails — rather than a step someone has to remember.

    Both the top-level version and every package entry are checked: they are
    separate fields, and a bump that moves one and not the other publishes a
    manifest whose own two halves disagree about what it ships.
    """
    if not project_version:
        return  # measured_project_version already failed with the reason
    manifest_path = ROOT / "server.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        fail(f"server.json: unreadable ({e})")
        return
    declared = manifest.get("version")
    if declared != project_version:
        fail(
            f"server.json: version is '{declared}' but pyproject builds "
            f"{project_version} — the registry manifest is behind the tree"
        )
    packages = manifest.get("packages")
    if not isinstance(packages, list) or not packages:
        fail("server.json: no packages[] to check — update this script")
        return
    for pkg in packages:
        pkg_version = pkg.get("version")
        if pkg_version != project_version:
            fail(
                f"server.json: package '{pkg.get('identifier')}' is pinned at "
                f"'{pkg_version}' but pyproject builds {project_version}"
            )


def normalize_doc_default(cell: str) -> str | None:
    """Return a comparable default from a markdown table cell, or None if the
    cell does not state a single literal default (prose descriptions etc.)."""
    cell = cell.strip()
    if cell in ("*(unset)*", "—", ""):
        return "<unset>"
    m = re.fullmatch(r"`([^`]*)`", cell)
    if not m:
        return None
    return m.group(1)


def check_env_tables(env_defaults: dict[str, str]) -> None:
    package_source = "\n".join(
        p.read_text() for p in (ROOT / "cpersona").glob("*.py")
    )
    for doc in DOC_FILES:
        rel = doc.relative_to(ROOT)
        for line in doc.read_text().splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            var = None
            var_idx = None
            for i, c in enumerate(cells):
                m = re.fullmatch(r"`(CPERSONA_\w+)`", c)
                if m:
                    var, var_idx = m.group(1), i
                    break
            if var is None:
                continue
            # Existence: a documented variable must be read somewhere in the
            # package — a renamed or removed env var must not survive in docs.
            if f'"{var}"' not in package_source and f"'{var}'" not in package_source:
                fail(f"{rel}: table documents `{var}`, which no source file reads")
                continue
            # Default: only checkable for vars the static parse understood and
            # cells that state a single literal.
            if var not in env_defaults or var_idx + 1 >= len(cells):
                continue
            documented = normalize_doc_default(cells[var_idx + 1])
            if documented is None:
                continue
            actual = env_defaults[var]
            if documented == "<unset>":
                continue  # existence-only: parse shapes for unset vars vary
            try:
                if float(documented) == float(actual):
                    continue  # numerically equal (5 vs 5.0)
            except ValueError:
                pass
            if documented != actual:
                fail(
                    f"{rel}: `{var}` documented default `{documented}` != "
                    f"config.py default `{actual}`"
                )


# A `CPERSONA_*` token anywhere in a page. The trailing `*` is kept because
# prose uses it to name a family (`CPERSONA_CALIBRATE_*`); the bare prefix
# `CPERSONA_` is not matched, since `+` requires at least one character after
# the underscore and pages legitimately name the prefix itself.
_ENV_NAME = re.compile(r"CPERSONA_[A-Za-z0-9_]+\*?")


def known_env_names() -> set[str]:
    """Every `CPERSONA_*` name the package reads, as written in its source."""
    package_source = "\n".join(p.read_text() for p in (ROOT / "cpersona").glob("*.py"))
    return set(re.findall(r"""["'](CPERSONA_[A-Z0-9_]+)["']""", package_source))


def env_name_violations(text: str, known: set[str]) -> list[tuple[int, str]]:
    """(line, token) for every `CPERSONA_*` name in `text` the source never reads.

    Split out from the file walk so it can be exercised on a page's text with a
    mutation applied — the gap this closes was found that way, and proving the
    repair should not require writing a mutated page to disk.
    """
    found: list[tuple[int, str]] = []
    reported: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        for token in _ENV_NAME.findall(line):
            if token in reported:
                continue
            if token.endswith("*"):
                # A family name is checked through its members, not waived. An
                # allowlist entry would stop checking the prefix itself; this
                # way a typo in the prefix still fails, and narrowing is how
                # the gate got here.
                if any(name.startswith(token[:-1]) for name in known):
                    continue
            elif token in known:
                continue
            reported.add(token)
            found.append((lineno, token))
    return found


def check_env_names() -> None:
    """Every `CPERSONA_*` name a page writes must be one the source reads.

    `check_env_tables` reads table rows only, so a name written as a bullet or
    inside a code fence went unchecked — and the reference page a reader with a
    broken install is sent to writes all of its variables that way, which put
    every one of them outside the gate (bug-282). Measured by mutation before
    this existed: a typo in that page's inline bullet left the gate at exit 0.

    This answers only "is this name real". Whether a *table* is complete and
    states the current default stays with `check_env_tables`; the two questions
    are different and widening one is not a reason to drop the other.
    """
    known = known_env_names()
    for doc in DOC_FILES:
        rel = doc.relative_to(ROOT)
        for lineno, token in env_name_violations(doc.read_text(), known):
            fail(f"{rel}:{lineno}: names `{token}`, which no environment variable the source reads matches")


_AXIS_COUNT_CLAIMS = (
    # (axis, English pattern, Japanese pattern). Both spellings for the reason the
    # tool-count check carries both: a detector that only knows the English phrase
    # passes the translation vacuously, and vacuous is indistinguishable from correct.
    ("agent_id", r"`agent_id` is accepted by\s+most tools \((\d+) of \d+\)", r"個のツールのうち\s*(\d+) 個が受け取り"),
    ("project_id", r"`project_id` by (\w+)", r"`project_id` は (\d+) 個"),
    ("channel", r"`channel` by exactly (\w+)", r"`channel` はちょうど (\d+) 個"),
)

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}

# Where a page spells the `channel` tools out by name. Group 1 is the run of names.
_CHANNEL_NAME_CLAIMS = (
    r"`channel` by exactly \w+ —\s*\n?((?:[^\n]*`\w+`[^\n]*\n?){1,3})",
    r"`channel` はちょうど \d+ 個 —\s*\n?((?:[^\n]*`\w+`[^\n]*\n?){1,3}?)\s*— だけです",
)


def check_axis_claims(acceptance: dict[str, list[str]]) -> None:
    """The per-axis tool counts, and the four tool names `channel` is named with.

    The names matter as much as the count: a page that lists the four tools by name
    stays literally true on the count while naming the wrong one, and the reader is
    the one who finds out.
    """
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for axis, en_pattern, ja_pattern in _AXIS_COUNT_CLAIMS:
            actual = len(acceptance[axis])
            for pattern in (en_pattern, ja_pattern):
                for m in re.finditer(pattern, text):
                    raw = m.group(1)
                    claimed = _NUMBER_WORDS.get(raw)
                    if claimed is None and raw.isdigit():
                        claimed = int(raw)
                    if claimed is None:
                        # An unreadable spelling is reported, not skipped: a pattern
                        # that quietly stops matching returns the same green as one
                        # that matched and agreed.
                        fail(
                            f"{rel}: states a count for `{axis}` as '{raw}', which this "
                            "check cannot read — add it to _NUMBER_WORDS or write the digit"
                        )
                        continue
                    if claimed != actual:
                        fail(
                            f"{rel}: claims `{axis}` is accepted by {raw}, but "
                            f"{actual} tool schemas offer it"
                        )
        # The four named tools, wherever a page spells them out next to `channel`.
        for pattern in _CHANNEL_NAME_CLAIMS:
            for m in re.finditer(pattern, text):
                named = set(re.findall(r"`(\w+)`", m.group(1)))
                expected = set(acceptance["channel"])
                if named != expected:
                    fail(
                        f"{rel}: names {sorted(named)} as the tools taking `channel`, "
                        f"but the schemas say {sorted(expected)}"
                    )


def check_calibrate_default(method: str) -> None:
    if not method:
        return  # already reported by the measurement
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for m in re.finditer(r"by default \(`(\w+)`\)|既定 \(`(\w+)`\)", text):
            claimed = m.group(1) or m.group(2)
            if claimed != method:
                fail(
                    f"{rel}: claims calibrate_threshold defaults to `{claimed}`, but "
                    f"CPERSONA_CALIBRATE_METHOD defaults to `{method}`"
                )


def check_embed_batch(batch: int) -> None:
    if batch < 0:
        return  # already reported by the measurement
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for pattern in (r"at most \*\*(\d+) texts per request\*\*", r"送るのは最大 \*\*(\d+) 件\*\*"):
            for m in re.finditer(pattern, text):
                if int(m.group(1)) != batch:
                    fail(
                        f"{rel}: claims at most {m.group(1)} texts per /embed request, "
                        f"but EMBED_BATCH_SIZE is {batch}"
                    )


# Commands from the reference embedding server that the setup pages tell a reader to
# run. CPersona does not depend on CEmbedding, so there is nothing here to import and
# nothing to introspect — the entry points are a fact about another distribution.
#
# Why an allowlist rather than reading CEmbedding's metadata over the network: a check
# that resolves a remote pyproject or PyPI record turns an unrelated outage — or a
# private repository, or an offline `uv run` — into a red build on this repository, for
# a claim confined to one page. Weighed against that, the failure this actually has to
# catch is the one that already shipped: getting-started told readers to run `python
# download_model.py`, a file that had been replaced by a console script, so anyone
# following the page stopped at a missing file.
#
# So the bound is honest and worth stating: this catches a page inventing or keeping a
# command that is not on the list. It does NOT notice CEmbedding renaming one — that
# arrives as a doc edit here, or not at all. Verified against
# https://github.com/Cloto-dev/CEmbedding `[project.scripts]` on 2026-08-25.
CEMBEDDING_COMMANDS = {
    "cembedding",
    "cembedding-download-model",
}
# `python -m` forms the same pages offer for a source checkout.
CEMBEDDING_MODULES = {
    "cembedding",
    "cembedding.download_model",
}


def check_script_file_invocations() -> None:
    """`python <something>.py` must name a file that exists in this repository.

    This is the shape the defect took: getting-started told readers to run `python
    download_model.py`, which had been a file in ANOTHER distribution before it became
    a console script. Nobody here could have noticed by reading, because the page looked
    exactly like the `python server.py` line two sections above it — and that one is
    real, which is why the rule is existence rather than a ban on the form.

    Anything not in this tree is either a stale instruction or an instruction pointing
    into a distribution whose layout this repository does not control. Both are the same
    advice to the reader: name an entry point (`console_script`, or `python -m module`),
    not a path into someone else's source tree.
    """
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for m in re.finditer(r"python[0-9]?\s+([\w./-]+\.py)\b", text):
            script = m.group(1)
            if (ROOT / script).exists():
                continue
            fail(
                f"{rel}: tells the reader to run `python {script}`, and no such file "
                "exists in this repository. If it belongs to another distribution, name "
                "its entry point instead — a path into someone else's source tree stops "
                "working the moment they package it differently, and the reader is the "
                "one who finds out."
            )


def check_external_commands() -> None:
    """Every `cembedding*` command a page tells the reader to run must exist upstream.

    Bidirectional on purpose. An unused allowlist entry is reported too: this list is
    the only record of what was verified, and an entry no page mentions is either a
    command that quietly left the docs or one that was never there — both make the
    remaining greens mean less than they appear to.
    """
    seen: set[str] = set()
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for m in re.finditer(r"(?<![\w./\"-])(cembedding(?:[-.][\w.-]+)?)(?![\w./-])", text):
            token = m.group(1)
            pool = CEMBEDDING_MODULES if "." in token else CEMBEDDING_COMMANDS
            if token in pool:
                seen.add(token)
                continue
            fail(
                f"{rel}: tells the reader to run `{token}`, which is not an entry point "
                f"CEmbedding ships ({sorted(CEMBEDDING_COMMANDS | CEMBEDDING_MODULES)}). "
                "This is a claim about another distribution, so a red here is usually a "
                "stale instruction on this page rather than a defect in cpersona — check "
                "CEmbedding's [project.scripts] and correct whichever side moved."
            )
    unused = (CEMBEDDING_COMMANDS | CEMBEDDING_MODULES) - seen
    if unused:
        fail(
            f"the CEmbedding command allowlist carries {sorted(unused)}, which no scanned "
            "page mentions. The list is the record of what was verified; prune it, or "
            "find the page that lost the instruction."
        )


VOLATILE_CLAIMS = {
    # claim regex (group 1 = number) → measured-stat key. These are the only
    # numeric claims docs are allowed to hand-round; each is stated with `~`.
    # The test-FUNCTION count is deliberately absent: Gate 12 in
    # tests/test_structural_gates.py owns that claim (one invariant, one
    # implementation) — it predates this script and is mutation-protected.
    r"~([\d,]+) LOC\*?\* Python": "server LOC",
    r"a ([\d,]+)-line vendored": "vendored LOC",
    r"([\d,]+) test modules": "test modules",
    r"~([\d,]+) cases": "collected cases",
    r"\(~([\d,]+) LOC, more test code": "test LOC",
    r"~([\d,]+) LOC Python across": "server LOC",
}


def check_volatile_claims(stats: dict[str, float]) -> None:
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for pattern, key in VOLATILE_CLAIMS.items():
            for m in re.finditer(pattern, text):
                claimed = int(m.group(1).replace(",", ""))
                actual = stats[key]
                if actual <= 0:
                    continue  # measurement itself failed and was reported
                drift = abs(actual - claimed) / actual
                if drift > TOLERANCE:
                    fail(
                        f"{rel}: claims {key} ≈ {claimed}, measured {int(actual)} "
                        f"(drift {drift:.1%} > {TOLERANCE:.0%})"
                    )


# (regex with group 1 = claimed version, human label). The separator is
# `[\s>]+` rather than a single space because a claim inside a blockquote can
# wrap across lines: a README status note once read "(latest\n> v2.4.41", and a
# pattern that only matched one space found the 2.5 claim, missed the 2.4 one,
# and reported green. A hit rate is not a coverage report, so this family is
# audited against an independent reading of the same pages by
# tests/test_structural_gates.py::test_release_claim_patterns_reach_every_claim.
RELEASE_CLAIMS = (
    # One pattern for both phrasings ("latest v2.5.6", "Latest release: 2.5.6").
    # They were two, and the first one's only site was the README's status
    # blockquote; when that blockquote stopped naming a version the pattern
    # matched nothing anywhere, which the dead-pattern assertion below reports
    # as a gate with no subject. Merging widens what is checked instead of
    # narrowing it: either spelling is caught in any scanned page.
    (
        re.compile(r"(?i)latest(?:\s+release:)?[\s>]+v?(\d+\.\d+\.\d+)\b"),
        "latest [release:] <version>",
    ),
)


def measured_latest_finals() -> dict[str, str]:
    """The newest FINAL release tag per minor line, from git.

    Pre-releases are excluded on purpose: a line's "latest release" in the
    lifecycle table is the shipped final, and an alpha does not answer that
    question. That is also why pyproject cannot be the source of truth here —
    it holds the version being prepared, which between a cut and the next bump
    is either a pre-release or a final that is already tagged anyway.

    A shallow clone has no tags, and a check that silently measures nothing is
    worse than no check: it reports green over an unexamined claim. So an empty
    tag list is a failure with the fix in the message, not a skip.
    """
    proc = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=ROOT, capture_output=True, text=True
    )
    finals: dict[str, tuple[int, int, int]] = {}
    for tag in proc.stdout.split():
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
        if not m:
            continue  # pre-release (v2.5.6a1) or a non-version tag
        major, minor, patch = (int(g) for g in m.groups())
        line = f"{major}.{minor}"
        if finals.get(line, (-1, -1, -1)) < (major, minor, patch):
            finals[line] = (major, minor, patch)
    if not finals:
        fail(
            "no final release tags visible (git tag --list 'v*' found none) — the "
            "'latest release' claims cannot be checked. In CI this means the "
            "checkout is shallow: set fetch-depth: 0 on the docs-facts job."
        )
    return {line: "%d.%d.%d" % v for line, v in finals.items()}


def check_release_claims(finals: dict[str, str]) -> None:
    if not finals:
        return  # already reported by the measurement
    for doc in DOC_FILES:
        text = doc.read_text()
        rel = doc.relative_to(ROOT)
        for pattern, label in RELEASE_CLAIMS:
            for m in pattern.finditer(text):
                claimed = m.group(1)
                line = ".".join(claimed.split(".")[:2])
                latest = finals.get(line)
                if latest is None:
                    fail(
                        f"{rel}: claims '{label}' = {claimed}, but no final tag "
                        f"exists on the {line}.x line"
                    )
                elif claimed != latest:
                    fail(
                        f"{rel}: claims '{label}' = {claimed}, newest final tag on "
                        f"the {line}.x line is {latest}"
                    )


# The LMEB Track A/B table is published twice: in the README, where a visitor
# decides whether the pipeline costs ranking quality, and in benchmarks/README,
# beside the harness that produced it. Two copies of the same measurement is
# exactly the shape this gate exists for — the second copy is the one nobody
# remembers to update, and a benchmark number that quietly disagrees with itself
# is worse than one nobody published.
#
# Rows are compared as text after whitespace is squeezed, so reformatting the
# table is free and changing a number is not. A missing table on either side is
# a failure rather than a skip: a comparison with nothing to compare reports
# green over an unexamined claim.
BENCH_TABLE_SOURCES = (
    ROOT / "README.md",
    ROOT / "benchmarks" / "README.md",
)
BENCH_ROW = re.compile(r"^\|\s*([\w.\-/]+)\s*\|\s*\d+M\s*\|.*$", re.M)


def _bench_rows(doc: Path) -> dict[str, str]:
    text = doc.read_text()
    return {
        m.group(1): re.sub(r"\s+", " ", m.group(0)).strip()
        for m in BENCH_ROW.finditer(text)
    }


def check_benchmark_tables_agree() -> None:
    tables = {doc: _bench_rows(doc) for doc in BENCH_TABLE_SOURCES}
    for doc, rows in tables.items():
        if not rows:
            fail(
                f"{doc.relative_to(ROOT)}: no LMEB benchmark rows found. The table is "
                "published in both README.md and benchmarks/README.md and this gate "
                "compares them; if it moved, update BENCH_TABLE_SOURCES rather than "
                "leaving the comparison with nothing to compare."
            )
    if any(not rows for rows in tables.values()):
        return

    first, second = BENCH_TABLE_SOURCES
    a, b = tables[first], tables[second]
    for model in sorted(set(a) | set(b)):
        if model not in a or model not in b:
            present, missing = (first, second) if model in a else (second, first)
            fail(
                f"{missing.relative_to(ROOT)}: has no benchmark row for {model!r}, "
                f"which {present.relative_to(ROOT)} publishes — the two tables must "
                "carry the same models."
            )
        elif a[model] != b[model]:
            fail(
                f"benchmark row for {model!r} differs between the two tables:\n"
                f"      {first.relative_to(ROOT)}: {a[model]}\n"
                f"      {second.relative_to(ROOT)}: {b[model]}"
            )


def main() -> int:
    tool_count = measured_tool_count()
    schema_version = measured_schema_version()
    project_version = measured_project_version()
    env_defaults = parsed_env_defaults()
    stats = measured_volatile_stats()
    finals = measured_latest_finals()
    acceptance = measured_axis_acceptance()
    calibrate_default = measured_calibrate_default()
    embed_batch = measured_embed_batch()

    check_tool_and_schema_claims(tool_count, schema_version)
    check_manifest_version(project_version)
    check_env_tables(env_defaults)
    check_env_names()
    check_volatile_claims(stats)
    check_release_claims(finals)
    check_axis_claims(acceptance)
    check_external_commands()
    check_script_file_invocations()
    check_calibrate_default(calibrate_default)
    check_embed_batch(embed_batch)
    check_benchmark_tables_agree()

    print(
        f"measured: {tool_count} tools, schema v{schema_version}, "
        + ", ".join(f"{k}={int(v)}" for k, v in stats.items())
        + f", {len(env_defaults)} env defaults parsed"
        + (", latest finals " + "/".join(f"{k}={v}" for k, v in sorted(finals.items())) if finals else "")
        + ", axes "
        + "/".join(f"{axis}={len(names)}" for axis, names in acceptance.items())
        + f", calibrate default={calibrate_default}, embed batch={embed_batch}"
    )
    if failures:
        print(f"\n{len(failures)} documentation fact(s) out of date:", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 1
    print("docs facts: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

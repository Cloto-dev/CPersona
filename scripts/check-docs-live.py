#!/usr/bin/env python3
"""Compare the published documentation site against the one this tree builds.

Every other documentation gate reads the source. That is the right place for
almost all of them, and it is why none of them can see the failure this script
exists for: the site can stop matching the source without any source being
wrong, and then every check stays green while readers are served a stale page.

How that happens here, mechanically, rather than as a worry:

  * The docs workflow declares ``concurrency: cancel-in-progress`` for reviews,
    and a run waiting behind another is replaced rather than queued.
  * Its triggers are path-filtered, so a push that touches no documentation path
    does not start a run at all.

Put those together and a documentation change can be cancelled by a following
push that never re-triggers the workflow. Nothing retries, nothing reports, and
the source stays correct — which is precisely why no source-reading gate notices.

Since the site became multi-version, "behind" needs one more distinction, and it
is the reason this script reads a manifest instead of only bytes. Each line is
published from its own branch, and publishing runs on a clock: between two runs
the site is *supposed* to be behind whatever the branches have grown since. A
check that compares the site against the branch heads as they are right now
reports that ordinary interval as a defect, every morning, until someone stops
reading it. Worse, the two clocks cannot be ordered — the publish and this check
are both scheduled workflows, and a scheduled workflow here has been measured
firing four to six hours late, so "publish runs first" is not a property anything
can rely on.

So the comparison is made against provenance rather than against a clock. The
assembler records, in the manifest it writes into the site, the commit each line
was built from. This script reads that manifest back off the live site:

  * the site's commit for a line equals this tree's  -> compare bytes strictly.
    Any difference is a real defect: the site is not serving what it says it
    was built from.
  * they differ -> the line is *lagging*. Byte differences and missing pages are
    expected, and are not reported as faults. What is checked instead is how
    long the unpublished commit has been waiting: past the bound, publishing has
    stopped rather than merely not caught up yet.
  * the site does not list the line at all -> it is *absent*. Declaring a line
    and never serving it is not a lag, it is a site that does not have what the
    version map promises.

The byte comparison is possible because the generator's output is deterministic:
a page built twice from the same source is identical, with no timestamp or build
id embedded. That was measured before this script was written, and the test suite
pins it — if the output ever acquires a varying field, this becomes a source of
false drift reports and must learn to ignore that field rather than being
relaxed into uselessness.

Usage:
    check-docs-live.py --site public [--base https://host/path/] [--jobs N]
                       [--lag-hours H]

Exit codes:
    0  the site serves what it was built from (lines may be lagging)
    1  a page differs or is missing, a line is absent, publishing has stalled,
       or the site could not be reached
    2  the check could not run (no built tree, nothing to compare)

A page that cannot be fetched is reported separately from a page that differs,
and both separately from a page the site does not have. They are three findings,
not one: the site is behind, the site is missing something, or the check could
not see the site at all — and reporting a network outage as "the docs are stale"
would send the reader to fix the wrong thing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE = "https://cloto-dev.github.io/CPersona/"
TIMEOUT_SECONDS = 30
MANIFEST_NAME = "versions.json"

# How long an unpublished commit may wait before the lag stops being ordinary.
# Publishing runs daily and a scheduled run has been measured four to six hours
# late, so a bound shorter than a day would fire on the delay rather than on the
# defect. Two days leaves room for one missed run without crying, and still
# names a publisher that has stopped within the same week it stopped.
DEFAULT_LAG_HOURS = 48

# Verdicts a line can carry. `strict` is the original behaviour and the only one
# under which byte differences are faults.
STRICT, LAGGING, STALLED, ABSENT = "strict", "lagging", "stalled", "absent"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(site: pathlib.Path) -> dict:
    path = site / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def fetch(url: str) -> tuple[bytes | None, str | None]:
    """Return ``(body, error)``. Exactly one is None."""
    request = urllib.request.Request(url, headers={"User-Agent": "cpersona-docs-live-check"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read(), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - any transport failure is "could not see it"
        return None, f"{type(exc).__name__}: {exc}"


def commit_age_hours(commit: str, now: dt.datetime | None = None) -> float | None:
    """Hours since `commit` was committed, or None when this clone cannot say.

    A commit the clone does not have is not evidence of anything -- it happens
    with a shallow checkout, and after a branch is rewritten -- so the caller is
    told "unknown" rather than handed a number that would decide a gate.
    """
    result = subprocess.run(
        ("git", "show", "-s", "--format=%cI", commit),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    when = dt.datetime.fromisoformat(result.stdout.strip())
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - when).total_seconds() / 3600.0


def line_of(relative: str, ids: set[str], current: str) -> str:
    """Which line a page in the assembled tree belongs to, from where it sits.

    The root serves the current line, and every other subtree is named by its
    line id. `scripts/check-version-selector.py` decides the same question the
    same way against the same trees; the suite pins that the two agree, because
    two gates disagreeing about which line a page is in would each be answering
    a coherent question about a different page.
    """
    first = relative.split("/", 1)[0]
    return first if first in ids else current


def verdicts(
    local: dict,
    published: dict | None,
    ages: dict[str, float | None],
    lag_hours: float,
) -> dict[str, str]:
    """What may be concluded about each declared line, before any page is fetched.

    Pure, and separated from everything that touches the network, because this
    workflow has no pull_request trigger on purpose -- on a branch the site
    SHOULD differ from the build -- so this is the part the suite can exercise
    and the only part a change can be reviewed against.
    """
    if published is None:
        # No manifest on the site: it predates versioned publishing, or the file
        # did not come back. Either way there is no provenance to reason with,
        # and the honest fallback is the behaviour that existed before there was
        # any -- compare, and report what differs.
        return {version["id"]: STRICT for version in local.get("versions", [])}

    by_id = {version.get("id"): version for version in published.get("versions", [])}
    out: dict[str, str] = {}
    for version in local.get("versions", []):
        identifier = version["id"]
        there = by_id.get(identifier)
        if there is None:
            out[identifier] = ABSENT
            continue
        here, theirs = version.get("commit", ""), there.get("commit", "")
        if not here or not theirs or here == theirs:
            # No provenance on either side is the legacy case again: compare.
            out[identifier] = STRICT
            continue
        age = ages.get(identifier)
        out[identifier] = STALLED if age is not None and age > lag_hours else LAGGING
    return out


def page_urls(site: pathlib.Path, base: str) -> list[tuple[str, str, pathlib.Path]]:
    """Every built page as ``(url, relative-path, file)``.

    Directory-style URLs: ``public/tools/index.html`` is served at ``…/tools/``,
    ``public/2.5/tools/index.html`` at ``…/2.5/tools/``, and the tree root at the
    base itself.
    """
    base = base if base.endswith("/") else base + "/"
    pairs: list[tuple[str, str, pathlib.Path]] = []
    for path in sorted(site.rglob("index.html")):
        relative = path.parent.relative_to(site).as_posix()
        url = base if relative == "." else f"{base}{relative}/"
        pairs.append((url, "" if relative == "." else relative, path))
    return pairs


def compare(
    site: pathlib.Path,
    base: str,
    jobs: int,
    line_verdicts: dict[str, str],
    current: str,
    fetcher=None,
) -> dict[str, list[str]]:
    """Fetch and classify every page of a line the verdicts say to compare.

    `fetcher` resolves at call time rather than defaulting to the module-level
    `fetch`: a default argument binds once, at definition, so a caller that
    replaced the module attribute would still be measured against the real
    network while believing it had substituted for it.
    """
    fetcher = fetcher or fetch
    ids = set(line_verdicts)
    findings: dict[str, list[str]] = {"drifted": [], "missing": [], "unreachable": []}
    checked: list[str] = []

    def one(pair: tuple[str, str, pathlib.Path]) -> None:
        url, relative, path = pair
        if line_verdicts.get(line_of(relative, ids, current), STRICT) != STRICT:
            # A lagging, stalled or absent line is already reported as one
            # finding about the line. Adding one per page would bury it under
            # its own consequences.
            return
        checked.append(url)
        body, error = fetcher(url)
        if error == "HTTP 404":
            findings["missing"].append(url)
            return
        if error is not None:
            findings["unreachable"].append(f"{url} ({error})")
            return
        if digest(body) != digest(path.read_bytes()):
            findings["drifted"].append(url)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, page_urls(site, base)))

    for key in findings:
        findings[key].sort()
    findings["checked"] = checked
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="public", help="assembled tree (default: public)")
    parser.add_argument("--base", default=DEFAULT_BASE, help="published base URL")
    parser.add_argument("--jobs", type=int, default=8, help="parallel fetches (default: 8)")
    parser.add_argument(
        "--lag-hours",
        type=float,
        default=DEFAULT_LAG_HOURS,
        help=f"how long a line may wait unpublished (default: {DEFAULT_LAG_HOURS})",
    )
    args = parser.parse_args(argv)

    site = pathlib.Path(args.site)
    if not site.is_dir():
        print(f"::error::no assembled tree at {site} — run build-all-versions.py first", file=sys.stderr)
        return 2

    local = read_manifest(site)
    if not local.get("versions"):
        print(
            f"::error::{site}/{MANIFEST_NAME} declares no versions — the tree was not assembled",
            file=sys.stderr,
        )
        return 2

    base = args.base if args.base.endswith("/") else args.base + "/"
    body, error = fetch(f"{base}{MANIFEST_NAME}")
    published = None
    if error is None:
        try:
            published = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"::warning::{base}{MANIFEST_NAME} is not readable JSON ({exc}) — comparing without provenance")
    else:
        print(f"::warning::could not read {base}{MANIFEST_NAME} ({error}) — comparing without provenance")

    ages = {
        version["id"]: commit_age_hours(version.get("commit", ""))
        for version in local["versions"]
        if version.get("commit")
    }
    line_verdicts = verdicts(local, published, ages, args.lag_hours)
    current = local.get("current", "")

    findings = compare(site, base, args.jobs, line_verdicts, current)
    checked = findings.pop("checked")

    lagging = sorted(i for i, v in line_verdicts.items() if v == LAGGING)
    stalled = sorted(i for i, v in line_verdicts.items() if v == STALLED)
    absent = sorted(i for i, v in line_verdicts.items() if v == ABSENT)
    comparable = sorted(i for i, v in line_verdicts.items() if v == STRICT)

    if comparable and not checked:
        # A line to compare and nothing compared means the tree has no pages
        # where they were expected, not that everything matched.
        print(
            f"::error::{site} has no pages for line(s) {', '.join(comparable)} — "
            f"the comparison would be vacuous",
            file=sys.stderr,
        )
        return 2

    print(f"compared {len(checked)} published page(s) against {site}/")
    for identifier in lagging:
        print(
            f"::notice::line {identifier} is lagging: the site was built from an earlier commit "
            f"than this tree. Publishing runs on a clock, so this is the ordinary state between "
            f"two runs and is not compared."
        )

    for url in findings["missing"]:
        print(f"::error::the site does not serve a page this tree builds: {url}")
    for url in findings["unreachable"]:
        print(f"::error::could not fetch {url}")
    for url in findings["drifted"]:
        print(f"::error::published page differs from this tree's build: {url}")
    for identifier in absent:
        print(
            f"::error::the site serves no tree for line {identifier}, which the version map "
            f"declares. A declared line that is never published is missing, not lagging."
        )
    for identifier in stalled:
        print(
            f"::error::line {identifier} has been waiting to publish for more than "
            f"{args.lag_hours:g}h. Publishing has stopped rather than not caught up: re-run the "
            f"docs workflow on the default branch and check why its schedule is not landing."
        )

    if findings["drifted"]:
        print(
            "::error::The site is not serving what it was built from. The usual cause is a "
            "deploy that never completed: the docs workflow replaces a run waiting behind "
            "another, and its triggers are path-filtered, so a following push that touches no "
            "documentation path can leave a deploy with nothing to retry it. Re-run the docs "
            "workflow on the default branch to publish the current source."
        )
    if findings["unreachable"]:
        print(
            "::error::Some pages could not be fetched. This is not a staleness finding — the "
            "check could not see the site. Confirm the site is reachable before reading the "
            "result above as drift."
        )

    if any(findings.values()) or absent or stalled:
        return 1
    print(f"docs live check: OK ({len(lagging)} line(s) lagging, not compared)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

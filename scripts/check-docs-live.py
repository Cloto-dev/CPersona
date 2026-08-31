#!/usr/bin/env python3
"""Compare the published documentation site against the one this tree builds.

Every other documentation gate reads the source. That is the right place for
almost all of them, and it is why none of them can see the failure this script
exists for: the site can stop matching the source without any source being
wrong, and then every check stays green while readers are served a stale page.

How that happens here, mechanically, rather than as a worry:

  * The docs workflow declares ``concurrency: cancel-in-progress: true``, so a
    second push to the same ref cancels the first run — including its deploy.
  * Its triggers are path-filtered, so a push that touches no documentation path
    does not start a run at all.

Put those together and a documentation change can be cancelled by a following
push that never re-triggers the workflow. Nothing retries, nothing reports, and
the source stays correct — which is precisely why no source-reading gate notices.
The published page simply keeps serving what it served before, indefinitely.

The comparison is byte-for-byte, which is possible because the generator's
output is deterministic: a page built twice from the same source is identical,
with no timestamp or build id embedded. That was measured before this script was
written, and the test suite pins it — if the output ever acquires a varying
field, this becomes a source of false drift reports and must learn to ignore
that field rather than being relaxed into uselessness.

Usage:
    check-docs-live.py --site site [--base https://host/path/] [--jobs N]

Exit codes:
    0  every published page matches what this tree builds
    1  at least one page differs, is missing, or is unreachable
    2  the check could not run (no site directory, nothing to compare)

A page that cannot be fetched is reported separately from a page that differs.
They are not the same finding: one says the site is behind, the other says the
check could not see the site at all, and reporting a network outage as "the docs
are stale" would send the reader to fix the wrong thing.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE = "https://cloto-dev.github.io/CPersona/"
TIMEOUT_SECONDS = 30


def page_urls(site: pathlib.Path, base: str) -> list[tuple[str, pathlib.Path]]:
    """Every built page, paired with the URL it is published at.

    Directory-style URLs: ``site/tools/index.html`` is served at ``…/tools/``,
    and the root ``site/index.html`` at the base itself.
    """
    base = base if base.endswith("/") else base + "/"
    pairs: list[tuple[str, pathlib.Path]] = []
    for path in sorted(site.rglob("index.html")):
        relative = path.parent.relative_to(site).as_posix()
        url = base if relative == "." else f"{base}{relative}/"
        pairs.append((url, path))
    return pairs


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def compare(site: pathlib.Path, base: str, jobs: int) -> tuple[list[str], list[str], int]:
    """Return ``(drifted, unreachable, checked)``."""
    pairs = page_urls(site, base)
    drifted: list[str] = []
    unreachable: list[str] = []

    def one(pair: tuple[str, pathlib.Path]) -> None:
        url, path = pair
        body, error = fetch(url)
        if error is not None:
            unreachable.append(f"{url} ({error})")
            return
        if digest(body) != digest(path.read_bytes()):
            drifted.append(url)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, pairs))

    return sorted(drifted), sorted(unreachable), len(pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="site", help="built site directory (default: site)")
    parser.add_argument("--base", default=DEFAULT_BASE, help="published base URL")
    parser.add_argument("--jobs", type=int, default=8, help="parallel fetches (default: 8)")
    args = parser.parse_args()

    site = pathlib.Path(args.site)
    if not site.is_dir():
        print(f"::error::no built site at {site} — run mkdocs build first", file=sys.stderr)
        return 2

    drifted, unreachable, checked = compare(site, args.base, args.jobs)
    if checked == 0:
        # An empty site directory would otherwise pass by comparing nothing.
        print(f"::error::{site} contains no pages — the comparison would be vacuous", file=sys.stderr)
        return 2

    print(f"compared {checked} published page(s) against {site}/")

    for url in unreachable:
        print(f"::error::could not fetch {url}")
    for url in drifted:
        print(f"::error::published page differs from this tree's build: {url}")

    if drifted:
        print(
            "::error::The site is not serving what this tree builds. The usual cause is a "
            "deploy that never ran: the docs workflow cancels an in-progress run when another "
            "push arrives, and its triggers are path-filtered, so a following push that touches "
            "no documentation path leaves the cancelled deploy with nothing to retry it. "
            "Re-run the docs workflow on the default branch to publish the current source."
        )
    if unreachable:
        print(
            "::error::Some pages could not be fetched. This is not a staleness finding — the "
            "check could not see the site. Confirm the site is reachable before reading the "
            "result above as drift."
        )

    if drifted or unreachable:
        return 1
    print("docs live check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

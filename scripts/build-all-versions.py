#!/usr/bin/env python3
"""Assemble the published documentation tree from every declared version line.

The site serves one subtree per line, plus a full build of the current line at
the root. The root is a build and not a redirect because absolute URLs to it are
already published in README, PyPI metadata, the bundled skill and source
comments; a redirect would change what a reader copies out of the address bar
and would not carry fragments.

Each line is built from its own branch by that branch's own build script. That
is the whole point of doing it this way: a gate or a theme bump added on the
development line never retroactively fails a frozen line, and a frozen line is
never rebuilt with a toolchain it was not tested against. What this script
guarantees is the assembly -- every declared version is present, the current
line is at the root, and the manifest describes what was actually built -- not
the internal quality of any one line, which is that line's own pull requests to
enforce.

The build is stateless: it reads branches, writes a directory, and keeps nothing
between runs. Nothing here consults the previously published site, so a run can
be repeated or replayed without a prior state to reconcile.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "docs-versions.json"
BUILD_SCRIPT = pathlib.Path("scripts") / "build-docs.sh"


class BuildError(RuntimeError):
    """A failure that is worth reporting on its own terms rather than a traceback."""


def git(*args: str, cwd: pathlib.Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ("git",) + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BuildError(
            "git " + " ".join(args) + " failed:\n" + (result.stderr.strip() or "(no output)")
        )
    return result.stdout.strip()


def load_config(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError(f"{path} does not exist") from None
    except json.JSONDecodeError as exc:
        raise BuildError(f"{path} is not valid JSON: {exc}") from None

    versions = data.get("versions")
    if not versions:
        raise BuildError(f"{path}: no versions declared")

    ids = [version.get("id") for version in versions]
    if any(not identifier for identifier in ids):
        raise BuildError(f"{path}: every version needs an id")
    if len(set(ids)) != len(ids):
        raise BuildError(f"{path}: duplicate version ids: {ids}")
    for identifier in ids:
        # The id becomes a path segment under the site root, so anything that
        # could climb out of it or collide with a built page is refused here
        # rather than discovered as a strange directory in the artifact.
        if "/" in identifier or identifier in {".", ".."}:
            raise BuildError(f"{path}: version id {identifier!r} is not a usable path segment")

    for version in versions:
        if not version.get("branch"):
            raise BuildError(f"{path}: version {version['id']} declares no branch")
        # The selector receives the version list as one delimited string, so a
        # delimiter inside a field would not corrupt the string visibly -- it
        # would silently split one entry into two, or drop the rest of a row.
        # Refused here, where the message can name the field.
        for field in ("id", "title"):
            value = str(version.get(field, ""))
            if ";" in value or "|" in value:
                raise BuildError(
                    f"{path}: version {version['id']} has {field}={value!r}, which contains a "
                    "delimiter the version list is joined with (';' or '|')"
                )

    current = data.get("current")
    if current not in ids:
        raise BuildError(f"{path}: current {current!r} is not one of {ids}")

    site_url = data.get("site_url")
    if not site_url or not site_url.endswith("/"):
        raise BuildError(f"{path}: site_url must be set and end with a slash")

    return data


def current_branch() -> str | None:
    """The branch this checkout is on, or None when HEAD is detached.

    A detached HEAD is not evidence of being on any branch, so every version
    then gets its own worktree from the ref it declares. Treating a detached
    HEAD as 'whatever branch we expected' would publish the wrong commit while
    reporting success.
    """
    name = git("rev-parse", "--abbrev-ref", "HEAD")
    return None if name == "HEAD" else name


def ref_exists(ref: str) -> bool:
    result = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def resolve_ref(branch: str) -> str:
    """The ref to build a line from: the remote-tracking branch, fetched if needed.

    The remote is what the site publishes, so it is what gets built. A local
    branch of the same name can be behind, ahead, or unrelated, and is used only
    when there is no remote one at all -- a line that exists only in this clone.

    The fetch is here rather than assumed of the caller because the failure it
    prevents is silent in the wrong direction: a checkout that did not bring the
    other branches would otherwise fail at worktree creation with a bare 'invalid
    reference', which reads like a typo in the version map rather than a missing
    fetch.
    """
    remote = f"origin/{branch}"
    if ref_exists(remote):
        return remote

    subprocess.run(
        ("git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/{remote}"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if ref_exists(remote):
        return remote

    if ref_exists(branch):
        return branch

    raise BuildError(
        f"branch {branch!r} resolves to no commit: it is neither {remote} (even after "
        f"fetching) nor a local branch. Either the line is declared in "
        f"{CONFIG_PATH.name} before its branch was created, or the name is wrong."
    )


def version_list(config: dict, here: str) -> str:
    """The version list as the selector reads it: "id|title|flag;id|title|flag".

    A string rather than JSON because it crosses into the build through the
    environment, and the template that reads it has no JSON parser -- mkdocs
    ships tojson, not fromjson. load_config refuses a delimiter inside either
    field, which is the only thing that could corrupt this quietly.

    Which version the build is travels in the third field rather than in a
    variable of its own, because mkdocs parses an environment value as YAML: a
    bare "2.5" would arrive as the float 2.5 and never equal the string "2.5".
    That failure is invisible -- the selector renders, the links work, and only
    the marker saying where the reader is goes missing. The delimiters keep this
    whole string un-numeric, so it arrives as text.
    """
    return ";".join(
        "{}|{}|{}".format(
            version["id"],
            version.get("title", version["id"]),
            "here" if version["id"] == here else "",
        )
        for version in config["versions"]
    )


def build_tree(
    source: pathlib.Path,
    out: pathlib.Path,
    site_url: str,
    version_env: dict[str, str] | None = None,
) -> None:
    script = source / BUILD_SCRIPT
    if not script.is_file():
        raise BuildError(
            f"{source} has no {BUILD_SCRIPT}.\n"
            "Every line builds with its own toolchain, so a branch that predates "
            "this script cannot be assembled here. Either add the script to that "
            f"branch or drop the line from {CONFIG_PATH.name}."
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(version_env or {})
    subprocess.run(["bash", str(script), str(out), site_url], cwd=source, check=True, env=env)


def write_manifest(out: pathlib.Path, config: dict, commits: dict[str, str]) -> None:
    """Describe what was built, including the commit each line was built from.

    The commit is what lets a reader of the published site tell "this tree is
    behind its branch" from "this tree does not match what it was built from".
    Without it the only available comparison is against the branch head as it is
    right now, which reports every unpublished commit as a defect -- and since
    publishing runs on a clock, that is the normal state between two runs rather
    than a fault. The manifest travels with the site, so the answer is read from
    the artifact itself and needs no state kept anywhere else.
    """
    current = config["current"]
    manifest = {
        "current": current,
        "versions": [
            {
                "id": version["id"],
                "title": version.get("title", version["id"]),
                "path": f"{version['id']}/",
                "current": version["id"] == current,
                "branch": version["branch"],
                "commit": commits.get(version["id"], ""),
            }
            for version in config["versions"]
        ],
    }
    (out / "versions.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def assemble(config: dict, out: pathlib.Path, as_branch: str | None = None) -> list[str]:
    base = config["site_url"]
    current = config["current"]
    here = as_branch or current_branch()

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    built: list[str] = []
    worktrees: list[pathlib.Path] = []
    with tempfile.TemporaryDirectory(prefix="docs-versions-") as scratch:
        try:
            sources: dict[str, pathlib.Path] = {}
            commits: dict[str, str] = {}
            for version in config["versions"]:
                branch = version["branch"]
                if here is not None and branch == here:
                    # Building the checkout we are already in keeps local runs
                    # honest about uncommitted work, and in CI the checkout is
                    # the branch, so the two agree.
                    sources[version["id"]] = REPO_ROOT
                    commits[version["id"]] = git("rev-parse", "HEAD")
                    continue
                path = pathlib.Path(scratch) / f"branch-{version['id']}"
                ref = resolve_ref(branch)
                git("worktree", "add", "--detach", str(path), ref)
                worktrees.append(path)
                sources[version["id"]] = path
                commits[version["id"]] = git("rev-parse", ref)

            # The root is built first. mkdocs empties its target directory, and
            # the root's target is the parent of every version subtree, so any
            # other order deletes what it just built.
            def version_env(identifier: str) -> dict[str, str]:
                # The root build is told it is the current line, because that is
                # the line it serves: a reader at the root should be shown which
                # version they are reading, not an empty marker.
                return {
                    "CPERSONA_DOC_VERSIONS": version_list(config, identifier),
                    "CPERSONA_DOC_SITE_ROOT": base,
                }

            root_source = sources[current]
            build_tree(root_source, out, base, version_env(current))
            built.append(f"(root) <- {current}")

            for version in config["versions"]:
                identifier = version["id"]
                build_tree(
                    sources[identifier],
                    out / identifier,
                    f"{base}{identifier}/",
                    version_env(identifier),
                )
                built.append(f"{identifier}/ <- {version['branch']}")

            write_manifest(out, config, commits)
        finally:
            for path in worktrees:
                subprocess.run(
                    ("git", "worktree", "remove", "--force", str(path)),
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out",
        default="public",
        help="directory to assemble the published tree into (default: public)",
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="version map to read")
    parser.add_argument(
        "--as-branch",
        default=None,
        metavar="BRANCH",
        help=(
            "treat this checkout as that branch, so the line it belongs to is built "
            "from here instead of from its remote ref. A pull request is checked out "
            "as a detached merge ref that is on no branch, so without this the "
            "assembly would be gated against the branch's published content and the "
            "change under review would not appear in it at all. Pass the branch the "
            "pull request targets."
        ),
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(pathlib.Path(args.config))
        built = assemble(config, pathlib.Path(args.out).resolve(), args.as_branch)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc.cmd[0]} exited {exc.returncode}", file=sys.stderr)
        return 1

    print(f"assembled {len(built)} tree(s) into {args.out}:")
    for line in built:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

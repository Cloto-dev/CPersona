"""Print or install the always-loaded memory policy block: ``cpersona-policy``.

The packaged agent skill (``cpersona/skills/cpersona-memory/SKILL.md``) ends its
setup with one step that is not an MCP call: a small policy block has to reach
the file the client loads on every session, or the memory triggers only fire in
the conversations that happen to activate the skill.

Why that step needs a command the *user* runs. A client may refuse to let an
agent write text that came from an external package into an always-loaded
instructions file, and refusing is the correct default — a file read as
instructions every session is exactly where injected text would want to land.
The agent must not work around such a refusal, so what is needed is a way for
the person who owns the file to perform the write themselves. Improvising a
shell one-liner for them each time does not do: it has to re-derive the
replace-between-markers rule from scratch, it depends on the marker spelling,
and a stream-editor pipeline is not something a Windows user can run at all.

What this command guarantees that a hand-written pipeline does not:

  * Nothing is written without ``--install``. Printing is the default, because
    the same rule the skill follows — show the block, then write it only when
    the owner of the file has agreed — is what a tool must default to.
  * Only the span between the markers is ever touched, byte for byte outside
    them, including line endings. That makes an upgrade from an older ``vN``
    block a replacement rather than a second copy.
  * Markers that do not form exactly one well-formed block are refused, never
    repaired. A file in that state was edited by hand or by two tools, and
    guessing which half to keep can only destroy instructions.
  * A symlinked target stays a symlink, and its real file is what changes:
    this file is commonly kept in a dotfiles repository and linked into place.

Exit codes:
  0  printed, installed, or already up to date
  1  the packaged skill is missing, or holds no single policy block
  2  usage error, or a refusal (bad agent id, ambiguous target, broken markers)
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from cpersona import fileperms

#: The block inside the skill. Deliberately the same shape the mirror test uses
#: to compare the skill against the documentation copies, so all three readers
#: agree on what "the block" is: the BEGIN marker (which must carry a version),
#: everything after it, and the END marker.
_BLOCK = re.compile(
    r"<!-- BEGIN cpersona-policy v\d+[^>]*-->\n.*?<!-- END cpersona-policy -->", re.S
)

#: The BEGIN marker as it may be found in a TARGET file, matched without
#: requiring a version. A block whose version someone edited away is still a
#: block, and treating it as absent would stack a second copy underneath it —
#: the one outcome the markers exist to prevent.
_TARGET_BEGIN = re.compile(r"<!-- BEGIN cpersona-policy[^>]*-->")
_TARGET_END = "<!-- END cpersona-policy -->"
_MARKER_VERSION = re.compile(r"<!-- BEGIN cpersona-policy (v\d+)")

_PLACEHOLDER = "<AGENT_ID>"

#: The id is substituted into a file that is read as instructions at the start
#: of every session, so it is validated rather than quoted: a newline would
#: split the line it sits on, and a quote, a backtick or a markup character
#: would end the code span that holds it and turn the remainder of the line
#: into prose the agent reads as instruction.
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_AGENT_ID_RULE = (
    "it must start with a letter or digit and may then contain only letters, "
    "digits and the characters . _ : - , up to 64 characters in total"
)

#: The user-level always-loaded file of each client that has one, matching the
#: per-client table in the Getting Started guide. Cursor and VS Code are absent
#: on purpose: their user-level instructions are a setting rather than a file,
#: so there is nothing here to write. Their project-level files, and every other
#: client's, are reachable with --target.
_CLIENT_FILES = {
    "claude-code": (".claude", "CLAUDE.md"),
    "codex": (".codex", "AGENTS.md"),
}

CREATED = "created"
APPENDED = "appended"
REPLACED = "replaced"
UNCHANGED = "unchanged"


class Refused(Exception):
    """Something the command declines to do, with the exit code it answers with."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# --- finding the block ------------------------------------------------------


def skill_path(module_dir: Path | None = None) -> Path | None:
    """Where the packaged skill is, or None.

    Two layouts, because the package is used both ways. In an installed wheel
    the skill is force-included *under* the package, so it sits beside this
    module; in a source checkout it is the repository's top-level ``skills/``
    directory, one level up. ``module_dir`` exists so the lookup can be measured
    against a synthetic tree — a test that only ever ran against this checkout
    would say nothing about the installed layout, which is the one every user
    has.
    """
    base = Path(__file__).parent if module_dir is None else Path(module_dir)
    for candidate in (base, base.parent):
        path = candidate / "skills" / "cpersona-memory" / "SKILL.md"
        if path.is_file():
            return path
    return None


def extract_block(skill_text: str) -> tuple[str, str]:
    """The one policy block in ``skill_text``, as (version, block)."""
    found = _BLOCK.findall(skill_text)
    if len(found) != 1:
        raise Refused(
            f"the packaged skill holds {len(found)} policy blocks, not one; "
            "it cannot say which block to install",
            1,
        )
    block = found[0]
    return _MARKER_VERSION.search(block).group(1), block


def policy_block(agent_id: str, module_dir: Path | None = None) -> tuple[str, str]:
    """The block as it should appear in the target file, as (version, block)."""
    path = skill_path(module_dir)
    if path is None:
        raise Refused(
            "cannot find the packaged skill (skills/cpersona-memory/SKILL.md) "
            "beside this module or in the repository above it; reinstall cpersona",
            1,
        )
    version, block = extract_block(path.read_text(encoding="utf-8"))
    filled = block.replace(_PLACEHOLDER, agent_id)
    if _PLACEHOLDER in filled:
        # Unreachable while the placeholder is one literal string, and checked
        # anyway: a block installed with a placeholder still in it tells every
        # session to call the tools with an agent id that does not exist.
        raise Refused(
            f"the block still contains {_PLACEHOLDER} after substitution", 1
        )
    return version, filled


# --- choosing the target ----------------------------------------------------


def client_target(client: str) -> Path:
    """The user-level always-loaded file of ``client``.

    The home directory is resolved on each call rather than at import, so the
    answer follows the environment the command actually runs in.
    """
    directory, name = _CLIENT_FILES[client]
    return Path.home() / directory / name


def detect_target() -> Path:
    """The target to install into when neither --client nor --target was given.

    Only an unambiguous answer counts. With both client directories present
    there is no way to tell which client's instructions the user meant, and with
    neither present any choice would create a directory for a client that is not
    installed — so both cases ask instead of guessing. The file is loaded every
    session: writing into the wrong one is not corrected by a later run, because
    the user would have no reason to look there.
    """
    home = Path.home()
    present = [name for name, (d, _) in sorted(_CLIENT_FILES.items()) if (home / d).is_dir()]
    # Spelled the way the parser spells it in --help, so the message can be
    # retyped as it stands.
    choices = ",".join(sorted(_CLIENT_FILES))
    if len(present) == 1:
        return client_target(present[0])
    if present:
        raise Refused(
            f"found more than one client directory under {home} "
            f"({', '.join(present)}), so the target is ambiguous; "
            f"pass --client {{{choices}}} or --target PATH",
            2,
        )
    raise Refused(
        f"found no client directory under {home}, so there is nothing to "
        f"detect; pass --client {{{choices}}} or --target PATH",
        2,
    )


# --- deciding what the file should say ---------------------------------------


def _separator(existing: str, body: str) -> str:
    """The line ending to build the blank-line separator from.

    Taken from the terminator the file itself used, so appending to a CRLF file
    leaves the last line it already had exactly as it was.
    """
    tail = existing[len(body):]
    if tail.startswith("\r\n"):
        return "\r\n"
    if not tail and "\r\n" in existing:
        return "\r\n"
    return "\n"


def plan(existing: str | None, block: str) -> tuple[str, str, str | None]:
    """What installing ``block`` would do, as (action, new text, old version).

    The old version is the ``vN`` of the block being replaced, None when there
    was none to replace or when the marker carried no version.
    """
    if existing is None:
        return CREATED, block + "\n", None

    begins = _TARGET_BEGIN.findall(existing)
    ends = existing.count(_TARGET_END)

    if len(begins) == 1 and ends == 1:
        begin = _TARGET_BEGIN.search(existing)
        end_at = existing.find(_TARGET_END)
        if end_at < begin.start():
            raise Refused(
                "the target's END marker comes before its BEGIN marker; "
                "the block is not repaired automatically — fix the markers by hand",
                2,
            )
        found = _MARKER_VERSION.search(begin.group(0))
        new = existing[: begin.start()] + block + existing[end_at + len(_TARGET_END) :]
        action = UNCHANGED if new == existing else REPLACED
        return action, new, found.group(1) if found else None

    if begins or ends:
        raise Refused(
            f"the target holds {len(begins)} BEGIN and {ends} END cpersona-policy "
            "markers, which is not one well-formed block; the block is not repaired "
            "automatically — fix the markers by hand",
            2,
        )

    # No markers: the block goes after what is already there. The existing bytes
    # are kept as a prefix, with one exception that is the separator itself —
    # however many newlines the file ended with (none, one, several) become
    # exactly one blank line, so a second run cannot drift the spacing.
    body = existing.rstrip("\r\n")
    if not body:
        return APPENDED, block + "\n", None
    eol = _separator(existing, body)
    return APPENDED, body + eol + eol + block + "\n", None


# --- writing -----------------------------------------------------------------


def _process_default_mode() -> int:
    """The mode a new file would get from a plain write in this process.

    A temporary file is 0600 by its own contract, which is narrower than what an
    editor would have produced for the same file — and this file is instructions
    the user reads and edits, not part of the memory corpus, so it should not
    arrive more restricted than the file they would have created by hand. The
    umask can only be read by setting it, hence the restore on the next line;
    the window is harmless here because this is a single-threaded command.
    """
    mask = os.umask(0o022)
    os.umask(mask)
    return 0o666 & ~mask


def _mode_to_keep(real: Path) -> int:
    """The permission bits the written file should end up with."""
    try:
        return stat.S_IMODE(real.stat().st_mode)
    except OSError:
        return _process_default_mode()


def _apply_mode(path: str, mode: int) -> None:
    """Best-effort chmod, for the reason cpersona/fileperms.py states: a platform
    or filesystem that will not honour a mode must not cost the caller the write.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def write_atomically(real: Path, text: str) -> None:
    """Replace ``real`` with ``text``, as UTF-8 bytes, in one step.

    ``real`` must already be the resolved path: a temporary file renamed over a
    symlink would replace the link with a regular file, silently detaching the
    file from wherever it was linked from.
    """
    parent = real.parent
    if not parent.is_dir():
        fileperms.makedirs_private(str(parent))
    mode = _mode_to_keep(real)
    # Same directory as the destination, so the rename is within one filesystem
    # and therefore atomic. mkstemp creates at 0600, which the chmod below then
    # widens to what the destination should have — never the other way round, so
    # the file is never world-readable before it holds the right bits.
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".cpersona-policy.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        _apply_mode(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(real: Path) -> str | None:
    """The target's current content, or None when it does not exist yet.

    Read as bytes and decoded, with no newline translation: a CRLF file must
    come back carrying its CRLFs, because everything outside the markers is
    written back unchanged.
    """
    try:
        raw = real.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # A directory where the file should be, or a file this user may not read.
        # Answered as a refusal rather than left to become a traceback: the person
        # running this is following a setup step, not debugging the package.
        raise Refused(f"cannot read {real}: {exc}", 2) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"{real} is not UTF-8 text, so it cannot be edited safely: {exc}", 2) from exc


# --- reporting ---------------------------------------------------------------


def describe(action: str, version: str, previous: str | None, path: Path, dry_run: bool) -> str:
    if action == UNCHANGED:
        line = f"cpersona-policy {version} already present in {path} (unchanged)"
    else:
        if action == REPLACED:
            detail = (
                f"replaced {previous}→{version}"
                if previous
                else "replaced a block whose marker carried no version"
            )
        else:
            detail = action
        verb = "would install" if dry_run else "installed"
        line = f"{verb} cpersona-policy {version} into {path} ({detail})"
    return f"dry-run: {line}" if dry_run else line


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpersona-policy",
        description=(
            "Print CPersona's always-loaded memory policy block, or install it into "
            "the instructions file your client loads every session."
        ),
        epilog=(
            "Without --install the block is printed and nothing is written. "
            "With --install, only the span between the cpersona-policy markers is "
            "touched; the rest of the file is left byte for byte as it was."
        ),
    )
    parser.add_argument(
        "--agent-id",
        required=True,
        help="The stable agent_id the block tells the agent to call the tools with",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Write the block into the target file (default: print it, write nothing)",
    )
    parser.add_argument(
        "--client",
        choices=sorted(_CLIENT_FILES),
        help=(
            "Install into this client's user-level file "
            "(claude-code: ~/.claude/CLAUDE.md, codex: ~/.codex/AGENTS.md)"
        ),
    )
    parser.add_argument(
        "--target",
        help="Install into this file instead (for a project-level file, or any other client)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what --install would do, and write nothing",
    )
    return parser


def _resolve_target(args) -> Path:
    if args.client and args.target:
        raise Refused(
            "--client and --target name two different files; pass one of them, not both",
            2,
        )
    if args.target:
        return Path(args.target).expanduser()
    if args.client:
        return client_target(args.client)
    return detect_target()


def _install(args, version: str, block: str) -> int:
    target = _resolve_target(args)
    # Resolved before anything is read or written, so a symlinked target is
    # edited in place rather than replaced by a regular file.
    real = Path(os.path.realpath(target))
    action, text, previous = plan(_read(real), block)

    if action != UNCHANGED and not args.dry_run:
        try:
            write_atomically(real, text)
        except OSError as exc:
            # write_atomically has already removed its temporary file and left the
            # old content in place; what remains is to say so instead of raising.
            raise Refused(f"cannot write {real}: {exc}; nothing was changed", 2) from exc

    # The path reported is the one the user named. Where that is a link, the real
    # file is named too — it is the file that changed, and the reader needs to
    # know which one to look at. Comparing the resolved path against the given
    # one would not do: a path whose PARENT is a symlink resolves differently
    # while the file itself is an ordinary file (/tmp on macOS is such a parent).
    print(describe(action, version, previous, target, args.dry_run))
    if target.is_symlink():
        print(f"note: {target} is a symlink; the file written is {real}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if not _AGENT_ID.match(args.agent_id):
            raise Refused(f"invalid --agent-id {args.agent_id!r}: {_AGENT_ID_RULE}", 2)
        version, block = policy_block(args.agent_id)

        if not args.install:
            for flag, given in (
                ("--client", args.client),
                ("--target", args.target),
                ("--dry-run", args.dry_run),
            ):
                if given:
                    raise Refused(
                        f"{flag} applies only together with --install; without "
                        "--install the block is printed and nothing is written",
                        2,
                    )
            print(block)
            return 0

        return _install(args, version, block)
    except Refused as refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return refusal.code


if __name__ == "__main__":
    sys.exit(main())

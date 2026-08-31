"""The alias ledger: (issuer, subject) → alias, persisted (docs/OAUTH_DESIGN.md §12).

Per-subject partitioning needs a name for "this signed-in person's memory
space", and the one identifier that is stable for a person is the (issuer,
subject) pair the verified token carries (OIDC Core §5.7). That pair is not the
name used, though: a raw subject baked into ``agent_id`` would outlive the
provider that minted it — a provider migration, a custom-domain move, or a
switch to pairwise identifiers each re-issue every subject, and data keyed by
the old values would be orphaned with no seam to repair it at. So the ledger
issues an opaque alias and owns the mapping, and repairing any of those events
is an edit to this file rather than a rewrite of the memory store.

The ledger is written by the server (first connection issues an alias) and by
the operator (pointing two (issuer, subject) rows at one alias is manual
account linking — the escape hatch for every re-issue event above). It
therefore lives beside the database, not beside ``acl.json``: the grant table
is operator-written policy the server must never touch, and on a hardened
deployment its directory is not writable by the service user at all.

Failure posture matches the ACL loader (docs/ACL_DESIGN.md §7): a ledger that
exists but cannot be parsed refuses startup. Silently starting over would
re-issue fresh aliases for every known subject, severing each person from the
memory space they already own — the quiet version of the exact loss the ledger
exists to prevent. A persist failure at issuance refuses the request the same
way: an alias that authorized a write but was never durably recorded would be
re-rolled on restart, stranding whatever the write stored.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import tempfile

logger = logging.getLogger(__name__)

#: The reserved prefix. Every alias the ledger issues starts with it, the boot
#: check refuses a database already using it for something else, and the ACL
#: boundary treats names under it as subject space — one namespace, three
#: readers, so the constant lives here and is imported, never retyped.
ALIAS_PREFIX = "u-"

#: What an alias may look like, issued or operator-written. Operators edit this
#: file (manual account linking), so the shape is validated on load rather than
#: trusted: an alias outside the reserved namespace would dodge the boot
#: collision check that keeps aliases and pre-existing agent ids apart.
_ALIAS_RE = re.compile(r"^u-[0-9a-f]{4,64}$")

_LEDGER_VERSION = 1


class AliasLedgerError(Exception):
    """A ledger defect — refuse rather than degrade (see module docstring)."""


class AliasLedger:
    """The (issuer, subject) → alias map, loaded once and persisted on issue."""

    def __init__(self, path: str):
        self._path = path
        # issuer → subject → alias. Nested rather than a joined key because a
        # subject is an arbitrary printable string (OIDC allows up to 255 ASCII
        # characters) and no separator can be guaranteed absent from it.
        self._aliases: dict[str, dict[str, str]] = {}
        if os.path.exists(path):
            self._aliases = self._load(path)

    @staticmethod
    def _load(path: str) -> dict[str, dict[str, str]]:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except OSError as e:
            raise AliasLedgerError(f"alias ledger {path!r} unreadable: {e}") from e
        except json.JSONDecodeError as e:
            raise AliasLedgerError(f"alias ledger {path!r} is not valid JSON: {e}") from e
        if not isinstance(raw, dict) or raw.get("version") != _LEDGER_VERSION:
            raise AliasLedgerError(
                f"alias ledger {path!r}: expected {{\"version\": {_LEDGER_VERSION}, "
                "\"aliases\": {...}}"
            )
        aliases = raw.get("aliases")
        if not isinstance(aliases, dict):
            raise AliasLedgerError(f"alias ledger {path!r}: \"aliases\" must be an object")
        for issuer, subjects in aliases.items():
            if not isinstance(issuer, str) or not issuer:
                raise AliasLedgerError(f"alias ledger {path!r}: empty issuer key")
            if not isinstance(subjects, dict):
                raise AliasLedgerError(
                    f"alias ledger {path!r}: issuer {issuer!r} must map subjects to aliases"
                )
            for subject, alias in subjects.items():
                if not isinstance(subject, str) or not subject:
                    raise AliasLedgerError(
                        f"alias ledger {path!r}: issuer {issuer!r} has an empty subject key"
                    )
                if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
                    raise AliasLedgerError(
                        f"alias ledger {path!r}: alias {alias!r} for issuer {issuer!r} "
                        f"is outside the reserved shape ({ALIAS_PREFIX}<hex>); an alias "
                        "elsewhere in the agent namespace would dodge the boot check "
                        "that keeps subject space and pre-existing agents apart"
                    )
        return aliases

    def peek(self, issuer: str, subject: str) -> str | None:
        """The alias already issued for this pair, or None. Never issues."""
        return self._aliases.get(issuer, {}).get(subject)

    def resolve_or_issue(self, issuer: str, subject: str) -> tuple[str, bool]:
        """The alias for this pair, issuing and persisting one on first sight.

        Returns ``(alias, issued)``; ``issued`` is True only on the call that
        minted it, which is what lets the response surface a fresh issuance to
        the caller while every later call stays quiet. Raises
        ``AliasLedgerError`` when the mint cannot be made durable.
        """
        existing = self.peek(issuer, subject)
        if existing is not None:
            return existing, False
        taken = {a for subjects in self._aliases.values() for a in subjects.values()}
        while True:
            alias = ALIAS_PREFIX + secrets.token_hex(6)
            if alias not in taken:
                break
        self._aliases.setdefault(issuer, {})[subject] = alias
        try:
            self._persist()
        except OSError as e:
            # Undo the in-memory entry: handing out an alias that survives only
            # in this process would strand the first session's writes behind a
            # different alias after restart.
            del self._aliases[issuer][subject]
            if not self._aliases[issuer]:
                del self._aliases[issuer]
            raise AliasLedgerError(
                f"alias ledger {self._path!r} could not be written: {e}"
            ) from e
        logger.info(
            "alias issued: %s for subject %r at issuer %s (ledger %s)",
            alias,
            subject,
            issuer,
            self._path,
        )
        return alias, True

    def _persist(self) -> None:
        """Write the whole ledger atomically (temp file + rename), mode 0600.

        The ledger maps identities rather than secrets, but it decides whose
        memory space a request reaches, so it gets the same file hygiene the
        grant table is warned toward.
        """
        directory = os.path.dirname(self._path) or "."
        payload = json.dumps(
            {"version": _LEDGER_VERSION, "aliases": self._aliases},
            indent=2,
            sort_keys=True,
        )
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".alias_ledger.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload + "\n")
            os.replace(tmp_path, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

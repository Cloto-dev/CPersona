"""Static credential resolution and request identity; no authorization policy."""

from __future__ import annotations

import contextvars
import hmac
import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class CredentialError(ValueError):
    """Invalid credentials. Messages never contain credential values."""


@dataclass(frozen=True)
class Principal:
    """Identity asserted by a trusted resolver, not by tool arguments."""

    client_id: str
    issuer: str = ""
    subject: str = ""


def resolve_env_token(raw: str) -> str:
    """Resolve whole ${ENV_VAR} references and reject missing/non-ASCII tokens."""
    if not isinstance(raw, str) or not raw:
        raise CredentialError("token must be a non-empty string")
    ref = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw)
    if ref:
        raw = os.environ.get(ref[1], "")
        if not raw:
            raise CredentialError("token environment variable is unset or empty")
    elif "${" in raw:
        raise CredentialError("token must use a whole-string ${ENV_VAR} reference")
    if not raw.isascii():
        raise CredentialError("bearer token contains non-ASCII characters")
    return raw


def validate_token_entries(
    entries: Iterable[tuple[str, str]],
    *,
    reserved_tokens: Iterable[str] = (),
) -> None:
    """Every credential must identify exactly one client, including legacy keys."""
    seen = set(reserved_tokens)
    for token, _client_id in entries:
        if not isinstance(token, str) or not token or not token.isascii():
            raise CredentialError("static token must be non-empty ASCII")
        if token in seen:
            raise CredentialError("credential duplicates another credential")
        seen.add(token)


def resolve_token(entries: Iterable[tuple[str, str]], presented: str) -> Principal | None:
    """Visit every entry, comparing bytes so hostile Unicode cannot raise."""
    if not presented:
        return None
    presented_bytes = presented.encode("utf-8")
    matched = None
    for token, client_id in entries:
        if hmac.compare_digest(presented_bytes, token.encode("utf-8")):
            matched = client_id
    return Principal(matched) if matched is not None else None


class PrincipalContext:
    """A consumer-owned ContextVar; separate apps need separate instances."""

    def __init__(self, name: str):
        self._var: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
            name,
            default=None,
        )

    def get(self) -> Principal | None:
        return self._var.get()

    def set(self, principal: Principal | None) -> contextvars.Token:
        return self._var.set(principal)

    def reset(self, token: contextvars.Token) -> None:
        self._var.reset(token)

    @contextmanager
    def bind(self, principal: Principal | None) -> Iterator[None]:
        token = self.set(principal)
        try:
            yield
        finally:
            self.reset(token)

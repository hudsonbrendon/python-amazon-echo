"""Exceptions raised by python-amazon-echo."""

from __future__ import annotations


class AmazonEchoError(Exception):
    """Base exception for every error raised by this library."""


class AmazonEchoConnectionError(AmazonEchoError):
    """Amazon could not be reached."""


class AmazonEchoConnectionTimeoutError(AmazonEchoConnectionError):
    """Amazon did not answer in time."""


class AmazonEchoAuthenticationError(AmazonEchoError):
    """The session was rejected and could not be refreshed."""


class AmazonEchoRateLimitError(AmazonEchoError):
    """Amazon kept answering 429/503 after the retries."""


class AmazonEchoResponseError(AmazonEchoError):
    """Amazon answered with an unexpected status or an unparsable body."""

    def __init__(self, status: int = 0, body: str = "") -> None:
        """Initialize the error with the HTTP status and (truncated) body."""
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class AmazonEchoNotSupportedError(AmazonEchoError):
    """The device or account does not expose the requested resource."""

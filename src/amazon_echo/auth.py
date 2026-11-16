"""Credentials and the external-browser login flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp


@dataclass(slots=True, kw_only=True)
class Credentials:
    """Everything needed to talk to the Alexa cloud for one account."""

    domain: str
    refresh_token: str
    cookies: dict[str, str] = field(default_factory=dict)
    serial: str | None = None
    access_token: str | None = None
    access_token_expires: float = 0.0
    customer_id: str | None = None
    customer_name: str | None = None
    email: str | None = None
    home_region: str | None = None
    source: str = "browser"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Credentials:
        """Build credentials from :meth:`to_dict` output."""
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in names})

    @classmethod
    def from_alexa_media(
        cls,
        entry_data: dict[str, Any],
        cookies: list[dict[str, Any]] | dict[str, str],
    ) -> Credentials:
        """Build credentials from an ``alexa_media`` config entry and its cookies."""
        raise NotImplementedError


@dataclass(slots=True, kw_only=True)
class AuthorizeRequest:
    """A pending external-browser login."""

    url: str
    serial: str
    code_verifier: str


def build_authorize_url(*, language: str = "en-US") -> AuthorizeRequest:
    """Return the URL the user has to open in a browser to log in."""
    raise NotImplementedError


async def register_device(
    request: AuthorizeRequest,
    redirect_url: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> Credentials:
    """Exchange the redirect URL of a finished login for credentials."""
    raise NotImplementedError

"""Credentials, token refresh and the external-browser login flow.

Nothing here ever sees the Amazon password: the user logs in in their own
browser (passkeys, 2FA and captchas just work) and pastes the URL the browser
ended on. That URL carries a one-time authorization code which is exchanged for
a device registration (refresh token + cookies).
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import aiohttp

from .const import (
    APP_BUNDLE_ID,
    APP_DEVICE_TYPE,
    APP_NAME,
    APP_OS_VERSION,
    APP_SOFTWARE_VERSION,
    APP_USER_AGENT,
    APP_VERSION,
    AUTH_HOST,
    REGION_BY_DOMAIN,
    SDK_VERSION,
)
from .exceptions import (
    AmazonEchoAuthenticationError,
    AmazonEchoConnectionError,
    AmazonEchoConnectionTimeoutError,
    AmazonEchoRateLimitError,
    AmazonEchoResponseError,
)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_LOGIN_SITE = "https://www.amazon.com"
_CODE_PARAM = "openid.oa2.authorization_code"
_TERMINAL_ERRORS = ("invalid_grant", "invalid_token", "unauthorized_client")


def region_for_domain(domain: str) -> str:
    """Return the push region ("NA", "EU" or "FE") of a retail domain."""
    return REGION_BY_DOMAIN.get(domain, "EU")


@dataclass(slots=True)
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
        """Build credentials from an ``alexa_media`` config entry and its cookies.

        Args:
            entry_data: The ``data`` of the ``alexa_media`` config entry.
            cookies: The ``cookies`` list of
                ``.storage/alexa_media.<email>.cookies`` or a name → value dict.

        Raises:
            AmazonEchoAuthenticationError: The entry holds no refresh token.

        """
        domain = str(entry_data.get("url") or "amazon.com").lower()
        domain = domain.removeprefix("https://").removeprefix("www.").strip("/")
        if not domain.startswith("amazon."):
            domain = f"amazon.{domain}"
        oauth = entry_data.get("oauth") or {}
        if not oauth.get("refresh_token"):
            raise AmazonEchoAuthenticationError(
                "alexa_media entry has no refresh token"
            )
        if isinstance(cookies, dict):
            jar = dict(cookies)
        else:
            jar = {
                c["name"]: c["value"]
                for c in cookies
                if domain.endswith(str(c.get("domain") or domain).lstrip("."))
            }
        return cls(
            domain=domain,
            refresh_token=oauth["refresh_token"],
            cookies=jar,
            access_token=oauth.get("access_token"),
            # alexa_media stores the expiry as an epoch timestamp
            access_token_expires=float(oauth.get("expires_in") or 0.0),
            email=entry_data.get("email"),
            home_region=region_for_domain(domain),
            source="alexa_media",
        )


@dataclass(slots=True)
class AuthorizeRequest:
    """A pending external-browser login."""

    url: str
    serial: str
    code_verifier: str


def _client_id(serial: str) -> str:
    return f"{serial}#{APP_DEVICE_TYPE}".encode().hex()


def build_authorize_url(*, language: str = "en-US") -> AuthorizeRequest:
    """Return the URL the user has to open in a browser to log in.

    The login always starts on amazon.com; the account's own marketplace is
    discovered after the registration.
    """
    serial = uuid.uuid4().hex.upper()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
    params = {
        "openid.return_to": f"{_LOGIN_SITE}/ap/maplanding",
        "openid.oa2.code_challenge_method": "S256",
        "openid.assoc_handle": "amzn_dp_project_dee_ios",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "pageId": "amzn_dp_project_dee_ios",
        "accountStatusPolicy": "P1",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.mode": "checkid_setup",
        "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
        "openid.oa2.client_id": f"device:{_client_id(serial)}",
        "language": language,
        "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
        "openid.oa2.code_challenge": challenge.decode(),
        "openid.oa2.scope": "device_auth_access",
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.pape.max_auth_age": "0",
        "openid.oa2.response_type": "code",
    }
    return AuthorizeRequest(
        url=f"{_LOGIN_SITE}/ap/signin?{urlencode(params)}",
        serial=serial,
        code_verifier=verifier.decode(),
    )


async def _post_auth(
    session: aiohttp.ClientSession,
    path: str,
    *,
    form: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST to api.amazon.com and return the decoded JSON answer."""
    headers = {
        "User-Agent": APP_USER_AGENT,
        "Accept": "application/json",
        "x-amzn-identity-auth-domain": AUTH_HOST,
    }
    try:
        async with session.post(
            f"https://{AUTH_HOST}{path}",
            data=form,
            json=body,
            headers=headers,
            timeout=_TIMEOUT,
        ) as resp:
            status = resp.status
            text = await resp.text()
    except TimeoutError as err:
        raise AmazonEchoConnectionTimeoutError(f"Timeout calling {path}") from err
    except aiohttp.ClientError as err:
        raise AmazonEchoConnectionError(f"Cannot reach {AUTH_HOST}: {err}") from err
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if status == 200 and isinstance(data, dict):
        return data
    if status in (429, 503):
        raise AmazonEchoRateLimitError(f"{path} answered HTTP {status}")
    message = _error_message(data)
    if status in (400, 401, 403):
        # Never echo the body: it can contain the tokens that were sent.
        raise AmazonEchoAuthenticationError(f"{path} refused: {message}")
    raise AmazonEchoResponseError(status, message)


def _error_message(data: Any) -> str:
    if not isinstance(data, dict):
        return "unexpected answer"
    error = data.get("response", {}).get("error") or data
    code = error.get("code") or error.get("error") or "error"
    text = error.get("message") or error.get("error_description") or ""
    return f"{code}: {text}".strip(": ")


def _token_form(credentials: Credentials, kind: str) -> dict[str, str]:
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "di.sdk.version": SDK_VERSION,
        "source_token": credentials.refresh_token,
        "package_name": APP_BUNDLE_ID,
        "di.hw.version": "iPhone",
        "platform": "iOS",
        "requested_token_type": kind,
        "source_token_type": "refresh_token",
        "di.os.name": "iOS",
        "di.os.version": APP_OS_VERSION,
        "current_version": SDK_VERSION,
        "previous_version": SDK_VERSION,
        "domain": f"www.{credentials.domain}",
    }


async def refresh_access_token(
    session: aiohttp.ClientSession, credentials: Credentials
) -> None:
    """Fetch a new access token and store it in ``credentials``.

    Raises:
        AmazonEchoAuthenticationError: The refresh token was rejected.

    """
    data = await _post_auth(
        session, "/auth/token", form=_token_form(credentials, "access_token")
    )
    token = data.get("access_token")
    if not token:
        raise AmazonEchoAuthenticationError("No access token in the answer")
    credentials.access_token = token
    credentials.access_token_expires = time.time() + float(
        data.get("expires_in") or 3600
    )


async def exchange_cookies(
    session: aiohttp.ClientSession, credentials: Credentials
) -> None:
    """Trade the refresh token for fresh website cookies of the account domain.

    The ``csrf`` cookie is not part of the exchange; the one already stored is
    kept (the Alexa site hands out a new one when it is missing).

    Raises:
        AmazonEchoAuthenticationError: The refresh token was rejected.

    """
    data = await _post_auth(
        session, "/auth/token", form=_token_form(credentials, "auth_cookies")
    )
    by_domain = data.get("response", {}).get("tokens", {}).get("cookies") or {}
    fresh = {
        cookie["Name"]: str(cookie["Value"]).replace('"', "")
        for cookies in by_domain.values()
        for cookie in cookies
    }
    if not fresh:
        raise AmazonEchoAuthenticationError("No cookies in the answer")
    credentials.cookies = {**credentials.cookies, **fresh}


async def register_device(
    request: AuthorizeRequest,
    redirect_url: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> Credentials:
    """Exchange the redirect URL of a finished login for credentials.

    Args:
        request: What :func:`build_authorize_url` returned.
        redirect_url: The full URL the browser ended on
            (``…/ap/maplanding?…openid.oa2.authorization_code=…``).
        session: Optional aiohttp session to use.

    Raises:
        AmazonEchoAuthenticationError: The URL carries no authorization code or
            Amazon refused the registration.

    """
    codes = parse_qs(urlsplit(redirect_url.strip()).query).get(_CODE_PARAM)
    if not codes:
        raise AmazonEchoAuthenticationError(
            "The pasted URL has no authorization code; copy the address of the "
            "page the browser ended on (it contains /ap/maplanding)"
        )
    body = {
        "requested_extensions": ["device_info", "customer_info"],
        "cookies": {"website_cookies": [], "domain": ".amazon.com"},
        "registration_data": {
            "domain": "Device",
            "app_version": APP_VERSION,
            "device_type": APP_DEVICE_TYPE,
            "device_name": f"%FIRST_NAME%'s%DUPE_STRATEGY_1ST%{APP_NAME}",
            "os_version": APP_OS_VERSION,
            "device_serial": request.serial,
            "device_model": "iPhone",
            "app_name": APP_NAME,
            "software_version": APP_SOFTWARE_VERSION,
        },
        "auth_data": {
            "use_global_authentication": "true",
            "client_id": _client_id(request.serial),
            "authorization_code": codes[0],
            "code_verifier": request.code_verifier,
            "code_algorithm": "SHA-256",
            "client_domain": "DeviceLegacy",
        },
        "user_context_map": {
            "frc": base64.b64encode(secrets.token_bytes(313)).decode().rstrip("=")
        },
        "requested_token_type": [
            "bearer",
            "mac_dms",
            "website_cookies",
            "store_authentication_cookie",
        ],
    }
    own_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        data = await _post_auth(session, "/auth/register", body=body)
        try:
            success = data["response"]["success"]
            bearer = success["tokens"]["bearer"]
            customer = success.get("extensions", {}).get("customer_info") or {}
            credentials = Credentials(
                domain="amazon.com",
                refresh_token=bearer["refresh_token"],
                cookies={
                    c["Name"]: str(c["Value"]).replace('"', "")
                    for c in success["tokens"].get("website_cookies") or []
                },
                serial=request.serial,
                access_token=bearer["access_token"],
                access_token_expires=time.time() + float(bearer["expires_in"]),
                customer_id=customer.get("user_id"),
                customer_name=customer.get("name") or customer.get("given_name"),
                home_region=customer.get("home_region"),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise AmazonEchoAuthenticationError(
                f"Registration refused: {_error_message(data)}"
            ) from err
        await _finish_registration(session, credentials)
    finally:
        if own_session:
            await session.close()
    return credentials


async def _finish_registration(
    session: aiohttp.ClientSession, credentials: Credentials
) -> None:
    """Move the session to the account's own marketplace and fill the profile."""
    from .client import AmazonEchoClient  # noqa: PLC0415 - import cycle

    client = AmazonEchoClient(credentials, session=session)
    if credentials.home_region == "FE":
        host = "alexa.amazon.co.jp"
    else:
        welcome = await client.request("GET", "/api/welcome")
        host = (welcome or {}).get("alexaHostName") or "alexa.amazon.com"
    domain = host.removeprefix("alexa.")
    if domain != credentials.domain:
        credentials.domain = domain
        credentials.cookies.pop("csrf", None)
        await exchange_cookies(session, credentials)
        client = AmazonEchoClient(credentials, session=session)
    credentials.home_region = credentials.home_region or region_for_domain(domain)
    account = await client.get_account()
    credentials.email = account.email
    credentials.customer_id = account.customer_id
    credentials.customer_name = credentials.customer_name or account.name

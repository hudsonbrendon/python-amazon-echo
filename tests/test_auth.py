"""Tests for credentials, token refresh and the external-browser login."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from amazon_echo import (
    AmazonEchoAuthenticationError,
    AmazonEchoConnectionError,
    AmazonEchoConnectionTimeoutError,
    AmazonEchoRateLimitError,
    AmazonEchoResponseError,
    AuthorizeRequest,
    Credentials,
    build_authorize_url,
    register_device,
)
from amazon_echo.auth import exchange_cookies, refresh_access_token, region_for_domain

from .conftest import FakeResponse

ENTRY = {
    "email": "user@example.com",
    "url": "amazon.com.br",
    "oauth": {
        "access_token": "Atna|old",
        "refresh_token": "Atnr|refresh",
        "expires_in": 1_800_000_000.5,
        "mac_dms": {"adp_token": "x", "device_private_key": "y"},
    },
}
REDIRECT = (
    "https://www.amazon.com/ap/maplanding?openid.assoc_handle=amzn_dp_project_dee_ios"
    "&openid.oa2.authorization_code=ANcode123&openid.mode=id_res"
)
REGISTRATION = {
    "response": {
        "success": {
            "tokens": {
                "bearer": {
                    "access_token": "Atna|registered",
                    "refresh_token": "Atnr|registered",
                    "expires_in": "3600",
                },
                "mac_dms": {"adp_token": "x", "device_private_key": "y"},
                "website_cookies": [
                    {"Name": "session-id", "Value": "123-456"},
                    {"Name": "x-main", "Value": '"quoted"'},
                ],
            },
            "extensions": {
                "device_info": {
                    "device_serial_number": "SERIAL",
                    "device_type": "A2IVLV5VM2W81",
                },
                "customer_info": {
                    "user_id": "amzn1.account.TEST",
                    "name": "Test User",
                    "home_region": "NA",
                },
            },
        }
    }
}
BR_COOKIES = {
    "response": {
        "tokens": {
            "cookies": {
                ".amazon.com.br": [
                    {"Name": "session-id", "Value": "777-888"},
                    {"Name": "at-acbbr", "Value": "Atza|br"},
                ]
            }
        }
    }
}
ME = {
    "id": "A0TESTCUSTOMER1",
    "fullName": "Test User",
    "email": "user@example.com",
    "countryOfResidence": "BR",
    "marketPlaceLocale": "pt-BR",
}


def test_credentials_round_trip(credentials):
    data = credentials.to_dict()
    assert data["domain"] == "amazon.com.br"
    assert data["cookies"]["csrf"] == "424242"
    assert Credentials.from_dict({**data, "unknown": 1}) == credentials
    assert credentials.source == "browser"


def test_credentials_positional():
    creds = Credentials("amazon.com", "token", {"a": "b"})
    assert (creds.domain, creds.refresh_token, creds.cookies) == (
        "amazon.com",
        "token",
        {"a": "b"},
    )
    assert creds.serial is None
    assert creds.access_token_expires == 0.0


def test_from_alexa_media_cookie_list():
    cookies = [
        {"name": "session-id", "value": "1", "domain": "amazon.com.br"},
        {"name": "csrf", "value": "2", "domain": ".amazon.com.br"},
        {"name": "other", "value": "3", "domain": "amazon.com"},
        {"name": "nodomain", "value": "4"},
    ]
    creds = Credentials.from_alexa_media(ENTRY, cookies)
    assert creds.domain == "amazon.com.br"
    assert creds.refresh_token == "Atnr|refresh"
    assert creds.access_token == "Atna|old"
    assert creds.access_token_expires == 1_800_000_000.5
    assert creds.cookies == {"session-id": "1", "csrf": "2", "nodomain": "4"}
    assert creds.email == "user@example.com"
    assert creds.home_region == "NA"
    assert creds.source == "alexa_media"
    assert creds.serial is None


@pytest.mark.parametrize(
    ("url", "domain", "region"),
    [
        ("amazon.de", "amazon.de", "EU"),
        ("https://www.amazon.co.jp/", "amazon.co.jp", "FE"),
        ("co.uk", "amazon.co.uk", "EU"),
        (None, "amazon.com", "NA"),
    ],
)
def test_from_alexa_media_domains(url, domain, region):
    creds = Credentials.from_alexa_media({**ENTRY, "url": url}, {"csrf": "1"})
    assert (creds.domain, creds.home_region) == (domain, region)
    assert creds.cookies == {"csrf": "1"}


def test_from_alexa_media_without_token():
    with pytest.raises(AmazonEchoAuthenticationError):
        Credentials.from_alexa_media({"url": "amazon.com", "oauth": {}}, {})


def test_region_for_domain():
    assert region_for_domain("amazon.com.br") == "NA"
    assert region_for_domain("amazon.in") == "FE"
    assert region_for_domain("amazon.fr") == "EU"


def test_build_authorize_url():
    request = build_authorize_url(language="pt-BR")
    parts = urlsplit(request.url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (parts.netloc, parts.path) == ("www.amazon.com", "/ap/signin")
    assert query["language"] == "pt-BR"
    assert query["openid.assoc_handle"] == "amzn_dp_project_dee_ios"
    assert query["openid.oa2.response_type"] == "code"
    assert query["openid.oa2.scope"] == "device_auth_access"
    assert query["openid.oa2.code_challenge_method"] == "S256"
    assert query["openid.return_to"] == "https://www.amazon.com/ap/maplanding"
    digest = hashlib.sha256(request.code_verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert query["openid.oa2.code_challenge"] == challenge
    client_id = query["openid.oa2.client_id"].removeprefix("device:")
    assert bytes.fromhex(client_id).decode() == f"{request.serial}#A2IVLV5VM2W81"
    assert len(request.serial) == 32
    assert request.serial == request.serial.upper()
    assert build_authorize_url().serial != request.serial


async def test_register_device_moves_to_the_account_marketplace(session):
    request = AuthorizeRequest(
        url="https://x", serial="SERIAL", code_verifier="verifier"
    )
    session.add("POST", "https://api.amazon.com/auth/register", REGISTRATION)
    session.add(
        "GET",
        "https://alexa.amazon.com/api/welcome",
        {"alexaHostName": "alexa.amazon.com.br"},
    )
    session.add("POST", "https://api.amazon.com/auth/token", BR_COOKIES)
    session.add("GET", "https://alexa.amazon.com.br/api/users/me", ME)

    creds = await register_device(request, f"  {REDIRECT}\n", session=session)

    body = session.sent("/auth/register")[0]["json"]
    assert body["auth_data"]["authorization_code"] == "ANcode123"
    assert body["auth_data"]["code_verifier"] == "verifier"
    assert body["auth_data"]["client_id"] == b"SERIAL#A2IVLV5VM2W81".hex()
    assert body["registration_data"]["device_serial"] == "SERIAL"
    assert body["registration_data"]["device_type"] == "A2IVLV5VM2W81"
    assert "website_cookies" in body["requested_token_type"]
    assert session.sent("/auth/register")[0]["headers"]["x-amzn-identity-auth-domain"]

    welcome = session.sent("/api/welcome")[0]
    assert "session-id=123-456" in welcome["headers"]["Cookie"]
    assert "x-main=quoted" in welcome["headers"]["Cookie"]
    exchange = session.sent("/auth/token")[0]["data"]
    assert exchange["requested_token_type"] == "auth_cookies"
    assert exchange["domain"] == "www.amazon.com.br"
    assert exchange["source_token"] == "Atnr|registered"

    assert creds.domain == "amazon.com.br"
    assert creds.refresh_token == "Atnr|registered"
    assert creds.access_token == "Atna|registered"
    assert creds.access_token_expires > 1_700_000_000
    assert creds.serial == "SERIAL"
    assert creds.cookies["session-id"] == "777-888"
    assert creds.cookies["at-acbbr"] == "Atza|br"
    assert creds.customer_id == "A0TESTCUSTOMER1"
    assert creds.customer_name == "Test User"
    assert creds.email == "user@example.com"
    assert creds.home_region == "NA"
    assert creds.source == "browser"
    assert not session.closed


async def test_register_device_us_account_keeps_cookies(session):
    request = AuthorizeRequest(
        url="https://x", serial="SERIAL", code_verifier="verifier"
    )
    session.add("POST", "/auth/register", REGISTRATION)
    session.add("GET", "https://alexa.amazon.com/api/welcome", {})
    session.add("GET", "https://alexa.amazon.com/api/users/me", ME)
    creds = await register_device(request, REDIRECT, session=session)
    assert creds.domain == "amazon.com"
    assert creds.cookies["session-id"] == "123-456"
    assert not session.sent("/auth/token")


async def test_register_device_far_east(session):
    request = AuthorizeRequest(
        url="https://x", serial="SERIAL", code_verifier="verifier"
    )
    registration = {
        "response": {
            "success": {
                "tokens": REGISTRATION["response"]["success"]["tokens"],
                "extensions": {"customer_info": {"user_id": "u", "home_region": "FE"}},
            }
        }
    }
    session.add("POST", "/auth/register", registration)
    session.add(
        "POST",
        "/auth/token",
        {
            "response": {
                "tokens": {"cookies": {".amazon.co.jp": [{"Name": "a", "Value": "b"}]}}
            }
        },
    )
    session.add("GET", "https://alexa.amazon.co.jp/api/users/me", ME)
    creds = await register_device(request, REDIRECT, session=session)
    assert (creds.domain, creds.home_region) == ("amazon.co.jp", "FE")
    assert not session.sent("/api/welcome")


async def test_register_device_needs_a_code(session):
    request = AuthorizeRequest(url="https://x", serial="S", code_verifier="v")
    with pytest.raises(AmazonEchoAuthenticationError, match="authorization code"):
        await register_device(
            request, "https://www.amazon.com/ap/signin?x=1", session=session
        )
    assert session.calls == []


async def test_register_device_refused(session):
    request = AuthorizeRequest(url="https://x", serial="S", code_verifier="v")
    session.add(
        "POST",
        "/auth/register",
        FakeResponse(
            401,
            {
                "response": {
                    "error": {"code": "InvalidToken", "message": "Auth time expired"}
                }
            },
        ),
    )
    with pytest.raises(AmazonEchoAuthenticationError, match="InvalidToken: Auth time"):
        await register_device(request, REDIRECT, session=session)


async def test_register_device_malformed_answer(session):
    request = AuthorizeRequest(url="https://x", serial="S", code_verifier="v")
    session.add("POST", "/auth/register", {"response": {"success": {"tokens": {}}}})
    with pytest.raises(AmazonEchoAuthenticationError, match="Registration refused"):
        await register_device(request, REDIRECT, session=session)


async def test_register_device_creates_its_own_session(monkeypatch, session):
    request = AuthorizeRequest(url="https://x", serial="S", code_verifier="v")
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    session.add("POST", "/auth/register", FakeResponse(500, text="oops"))
    with pytest.raises(AmazonEchoResponseError):
        await register_device(request, REDIRECT)
    assert session.closed


async def test_refresh_access_token(session, credentials):
    session.add("POST", "/auth/token", {"access_token": "Atna|new", "expires_in": 3600})
    await refresh_access_token(session, credentials)
    assert credentials.access_token == "Atna|new"
    form = session.calls[0]["data"]
    assert form["source_token_type"] == "refresh_token"
    assert form["requested_token_type"] == "access_token"
    assert form["app_name"]


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (FakeResponse(429), AmazonEchoRateLimitError),
        (FakeResponse(503), AmazonEchoRateLimitError),
        (FakeResponse(500, text="<html>"), AmazonEchoResponseError),
        (
            FakeResponse(403, {"error": "unauthorized_client"}),
            AmazonEchoAuthenticationError,
        ),
        (FakeResponse(200, text="not json"), AmazonEchoResponseError),
        (TimeoutError(), AmazonEchoConnectionTimeoutError),
        (aiohttp.ClientConnectionError("x"), AmazonEchoConnectionError),
    ],
)
async def test_token_endpoint_failures(session, credentials, outcome, error):
    session.add("POST", "/auth/token", outcome)
    with pytest.raises(error):
        await refresh_access_token(session, credentials)


async def test_exchange_cookies_without_cookies(session, credentials):
    session.add("POST", "/auth/token", {"response": {"tokens": {"cookies": {}}}})
    with pytest.raises(AmazonEchoAuthenticationError):
        await exchange_cookies(session, credentials)


async def test_auth_errors_never_echo_secrets(session, credentials):
    session.add(
        "POST",
        "/auth/token",
        FakeResponse(400, {"error": "invalid_grant", "source_token": "Atnr|refresh"}),
    )
    with pytest.raises(AmazonEchoAuthenticationError) as err:
        await exchange_cookies(session, credentials)
    assert "Atnr|refresh" not in str(err.value)

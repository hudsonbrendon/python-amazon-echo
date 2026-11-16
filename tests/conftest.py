"""Shared fixtures: a tiny fake aiohttp session and sanitized API answers."""

from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
import json
from pathlib import Path
from typing import Any

import pytest
from yarl import URL

from amazon_echo import AmazonEchoClient, Credentials, Device

FIXTURES = Path(__file__).parent / "fixtures"

OFFICE = Device(
    serial="G000TEST00000001",
    device_type="A18O6U1UQFJ0XK",
    name="Office Echo",
    family="ECHO",
    model="Echo Plus",
    software_version="1234567890",
    account_id="A0TESTACCOUNTID0001",
    online=True,
    capabilities=frozenset({"AUDIO_PLAYER", "VOLUME_SETTING"}),
)


def load_json(name: str) -> Any:
    """Parse a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    """Just enough of aiohttp.ClientResponse."""

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        *,
        text: str | None = None,
        url: str | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._text = (
            text if text is not None else ("" if body is None else json.dumps(body))
        )
        self.url = URL(url) if url else None
        self.cookies = SimpleCookie(cookies or {})

    async def text(self) -> str:
        return self._text

    async def json(self, content_type: str | None = None) -> Any:
        return json.loads(self._text)


class _Context:
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> FakeResponse:
        await asyncio.sleep(0)  # let concurrent requests interleave
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSession:
    """Routes requests to canned answers and records what was sent."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, list[Any]]] = []
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def add(self, method: str, fragment: str, *outcomes: Any) -> None:
        """Answer ``method`` requests whose URL contains ``fragment``.

        Outcomes are used in order; the last one repeats. A dict/list/None is
        shorthand for a 200 JSON answer.
        """
        self.routes.append((method, fragment, list(outcomes) or [None]))

    def request(self, method: str, url: str, **kwargs: Any) -> _Context:
        self.calls.append({"method": method, "url": url, **kwargs})
        matches = [r for r in self.routes if r[0] == method and r[1] in url]
        if not matches:
            raise AssertionError(f"unexpected request: {method} {url}")
        outcomes = max(matches, key=lambda r: len(r[1]))[2]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if not isinstance(outcome, (FakeResponse, BaseException)):
            outcome = FakeResponse(200, outcome)
        if isinstance(outcome, FakeResponse) and outcome.url is None:
            outcome.url = URL(url)
        return _Context(outcome)

    def get(self, url: str, **kwargs: Any) -> _Context:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _Context:
        return self.request("POST", url, **kwargs)

    async def close(self) -> None:
        self.closed = True

    def sent(self, fragment: str, method: str | None = None) -> list[dict[str, Any]]:
        """Return the recorded calls whose URL contains ``fragment``."""
        return [
            c
            for c in self.calls
            if fragment in c["url"] and (method is None or c["method"] == method)
        ]


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(
        domain="amazon.com.br",
        refresh_token="Atnr|refresh",
        cookies={"session-id": "111-222", "csrf": "424242"},
        access_token="Atna|access",
        access_token_expires=4102444800.0,
        customer_id="A0TESTCUSTOMER1",
        home_region="NA",
    )


@pytest.fixture
def updates() -> list[Credentials]:
    return []


@pytest.fixture
def client(session, credentials, updates) -> AmazonEchoClient:
    client = AmazonEchoClient(
        credentials, session=session, on_credentials_update=updates.append
    )
    client._locale = "pt-BR"
    return client


@pytest.fixture
def office() -> Device:
    return OFFICE


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Retries do not wait."""
    import amazon_echo.client as module

    monkeypatch.setattr(module, "_RETRY_DELAYS", (0.0, 0.0))

"""Async client for the Alexa cloud API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
from datetime import UTC, datetime, timedelta
import json as jsonlib
import logging
import re
import time
from typing import Any, Self
from urllib.parse import quote, urlsplit

import aiohttp

from . import routines
from .auth import (
    Credentials,
    exchange_cookies,
    refresh_access_token,
    region_for_domain,
)
from .const import (
    APP_USER_AGENT,
    COMMS_HOST,
    DROP_IN_MODES,
    GUARD_CAPABILITY,
    GUARD_MODEL,
    GUARD_SKILL_ID,
    INFO_SKILLS,
    LIST_KINDS,
    MEDIA_COMMANDS,
    MUTATION_SET_ENDPOINT_FEATURES,
    NOTIFICATION_KINDS,
    QUERY_APPLIANCES,
    QUERY_ENDPOINTS,
    USER_AGENT,
)
from .endpoints import (
    build_endpoint,
    dig as _dig,
    format_mac,
    parse_endpoint,
    parse_features,
)
from .exceptions import (
    AmazonEchoAuthenticationError,
    AmazonEchoConnectionError,
    AmazonEchoConnectionTimeoutError,
    AmazonEchoError,
    AmazonEchoNotSupportedError,
    AmazonEchoRateLimitError,
    AmazonEchoResponseError,
)
from .models import (
    Account,
    AuxPort,
    BluetoothDevice,
    Device,
    EchoList,
    Endpoint,
    Guard,
    ListItem,
    MediaState,
    Notification,
    Routine,
    VoiceRecord,
)
from .phrases import timer_request

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_RETRY_DELAYS = (1.0, 3.0)  # back-off before the 2nd and 3rd attempt on 429/503
_TIMER_POLLS = 8
_TIMER_POLL_SECONDS = 3.0
_REAUTH_COOLDOWN = 60.0  # no second cookie exchange this soon after one
_TOKEN_MARGIN = 60.0
_HISTORY_CSRF_TTL = 12 * 3600
_HISTORY_PAGES = 5
# One GraphQL answer serves the Alexa devices and the smart-home endpoints.
_ENDPOINTS_TTL = 5.0
_BACKGROUND_IMAGE_ID = "JqIFZhtBTx25wLGTJGdNGQ"  # the app's "personal photo" slot
# The Amazon Kids parent dashboard is parents.<domain>, except where translated.
_PARENTS_SUBDOMAINS = {"amazon.de": "eltern"}
_CSRF_META = re.compile(
    r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"', re.IGNORECASE
)
_NODE = "com.amazon.alexa.behaviors.model."
_SKIPPED_UTTERANCES = frozenset(
    {"ASR_TIMEOUT", "DEVICE_ARBITRATION", "NO_EXPRESSED_INTENT", "WAKE_WORD_ONLY"}
)
_LIST_NAMES = {"SHOP": "Shopping", "TODO": "To-do"}  # the built-in lists are unnamed
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# `recurringPattern` of an alarm → ISO weekdays (empty = every day).
_PATTERN_DAYS: dict[str, tuple[int, ...]] = {
    "P1D": (),
    "XXXX-WD": (1, 2, 3, 4, 5),
    "XXXX-WE": (6, 7),
    **{f"XXXX-WXX-{day}": (day,) for day in range(1, 8)},
}
_RECURRENCE_PRESETS: dict[str, list[str]] = {
    "DAILY": [],
    "EVERYDAY": [],
    "WEEKDAYS": list(_WEEKDAYS[:5]),
    "WEEKENDS": list(_WEEKDAYS[5:]),
}


def _from_ms(value: Any) -> datetime | None:
    """Return an aware datetime for an epoch in milliseconds (0/None → None)."""
    try:
        millis = float(value)
    except TypeError, ValueError:
        return None
    return datetime.fromtimestamp(millis / 1000, UTC) if millis > 0 else None


def _local(when: datetime) -> datetime:
    """Return ``when`` as wall-clock time in the system time zone.

    ponytail: Amazon reads alarm/reminder times in the Echo's own time zone and
    the API does not take one; this assumes the host shares it. Pass a naive
    datetime in the Echo's local time when it does not.
    """
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def _from_original(item: dict[str, Any]) -> datetime | None:
    """Local ``originalDate`` + ``originalTime`` (music alarms carry only these).

    Same time zone assumption as :func:`_local`; a repeating alarm whose first
    date is past moves to its next ring.
    """
    date, clock = item.get("originalDate"), item.get("originalTime")
    if not date or not clock:
        return None
    try:
        when = datetime.fromisoformat(f"{date}T{str(clock)[:8]}").astimezone()
        if item.get("recurringPattern") and when < datetime.now().astimezone():
            # _next_ring works on local wall-clock time
            ring = _next_ring(item, datetime.now())  # noqa: DTZ005
            when = datetime.fromisoformat(ring).astimezone()
    except ValueError:
        return None
    return when


def parse_notification(item: dict[str, Any]) -> Notification:
    """Build a :class:`Notification` from an ``/api/notifications`` item."""
    kind = NOTIFICATION_KINDS.get(item.get("type") or "", "")
    kind = kind or str(item.get("type") or "unknown").lower()
    if item.get("musicAlarmId") and kind == "alarm":
        kind = "music_alarm"
    remaining = item.get("remainingTime") or 0
    when = (
        _from_ms(item.get("triggerTime"))
        or _from_ms(item.get("alarmTime"))
        or (_from_original(item) if kind in ("alarm", "music_alarm") else None)
    )
    return Notification(
        id=item["id"],
        kind=kind,
        status=item.get("status") or "OFF",
        device_serial=item.get("deviceSerialNumber") or "",
        device_type=item.get("deviceType") or "",
        label=item.get("reminderLabel")
        or item.get("timerLabel")
        or item.get("alarmLabel"),
        when=when,
        remaining=timedelta(milliseconds=remaining)
        if kind == "timer" and remaining > 0
        else None,
        recurrence=item.get("recurringPattern") or None,
        sound=_dig(item, "sound", "id"),
        raw=item,
    )


def _session_as_player(now: dict[str, Any]) -> dict[str, Any]:
    """Shape a media session's ``nowPlayingData`` like ``playerInfo``.

    The session names the state ``playerState`` and counts in milliseconds.
    """
    progress = dict(now.get("progress") or {})
    for key in ("mediaLength", "mediaProgress"):
        try:
            progress[key] = float(progress[key]) / 1000
        except KeyError, TypeError, ValueError:
            progress.pop(key, None)
    return {**now, "state": now.get("playerState"), "progress": progress}


def _parse_media(info: dict[str, Any]) -> MediaState:
    """Build a :class:`MediaState` from the ``playerInfo`` of ``/api/np/player``."""
    transport = info.get("transport") or {}
    progress = info.get("progress") or {}
    volume = info.get("volume") or {}
    text = info.get("infoText") or {}
    state = str(info.get("state") or "IDLE").lower()

    def flag(name: str) -> bool | None:
        value = transport.get(name)
        return None if value in (None, "HIDDEN") else value == "SELECTED"

    def seconds(name: str) -> float | None:
        value = progress.get(name)
        return float(value) if isinstance(value, (int, float)) else None

    return MediaState(
        state=state if state in ("playing", "paused") else "idle",
        title=text.get("title"),
        artist=text.get("subText1"),
        album=text.get("subText2"),
        image_url=_dig(info, "mainArt", "url"),
        duration=seconds("mediaLength"),
        position=seconds("mediaProgress"),
        position_updated_at=datetime.now(UTC) if progress else None,
        provider=_dig(info, "provider", "providerName"),
        shuffle=flag("shuffle"),
        repeat=flag("repeat"),
        can_next=transport.get("next") == "ENABLED",
        can_previous=transport.get("previous") == "ENABLED",
        can_seek=bool(progress.get("allowScrubbing")),
        muted=bool(volume.get("muted")),
        volume=volume.get("volume"),
    )


def _replace_placeholders(node: Any, values: dict[str, str]) -> Any:
    """Return a copy of a routine node with the ``ALEXA_CURRENT_*`` markers set."""
    if isinstance(node, dict):
        return {k: _replace_placeholders(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_placeholders(v, values) for v in node]
    if isinstance(node, str):
        return values.get(node, node)
    return node


def _recurrence_rule(recurrence: str, when: datetime) -> dict[str, Any]:
    """Translate a recurrence string into the alarms API repeat rule.

    Accepts ``DAILY``, ``WEEKDAYS``, ``WEEKENDS``, ``WEEKLY`` (the weekday of
    ``when``) or a comma separated list of days such as ``MO,WE,FR``.
    """
    key = recurrence.strip().upper()
    if key in _RECURRENCE_PRESETS:
        days = _RECURRENCE_PRESETS[key]
    elif key == "WEEKLY":
        days = [_WEEKDAYS[when.weekday()]]
    else:
        days = [day.strip()[:2] for day in key.split(",") if day.strip()]
        if not days or any(day not in _WEEKDAYS for day in days):
            raise ValueError(f"Unsupported recurrence: {recurrence!r}")
    if not days:
        return {"freq": "DAILY", "byDay": [], "interval": 1}
    return {"freq": "WEEKLY", "byDay": days, "interval": 1}


def _next_ring(alarm: dict[str, Any], now: datetime) -> str:
    """Return the next ring of an alarm as local ``YYYY-MM-DDTHH:MM:SS``.

    Raises:
        ValueError: The alarm has no time, or an unknown repeat rule.

    """
    try:
        hour, minute, second = (
            int(part) for part in alarm["originalTime"][:8].split(":")
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("The alarm has no time") from err
    # recurringPattern only names the first day when an alarm repeats on a few
    days: tuple[int, ...] = tuple(
        _WEEKDAYS.index(day) + 1
        for day in _dig(alarm, "rRuleData", "byWeekDays") or ()
        if day in _WEEKDAYS
    )
    pattern = alarm.get("recurringPattern")
    if not days and pattern:
        if pattern not in _PATTERN_DAYS:
            raise ValueError(f"Unsupported repeat rule: {pattern!r}")
        days = _PATTERN_DAYS[pattern]
    when = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    while when <= now or (days and when.isoweekday() not in days):
        when += timedelta(days=1)
    return when.isoformat(timespec="seconds")


class AmazonEchoClient:
    """Client for the Alexa cloud API of one Amazon account.

    Args:
        credentials: The session to use; it is updated in place when tokens or
            cookies are renewed.
        session: Optional aiohttp session. Cookies are always sent explicitly,
            so a shared session is safe.
        on_credentials_update: Called (synchronously) whenever ``credentials``
            changed and should be persisted.

    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        session: aiohttp.ClientSession | None = None,
        on_credentials_update: Callable[[Credentials], None] | None = None,
    ) -> None:
        """Initialize the client."""
        self.credentials = credentials
        self._session = session
        self._own_session = session is None
        self._on_update = on_credentials_update
        self._auth_lock = asyncio.Lock()
        self._generation = 0
        # -inf, not 0: time.monotonic() can be below the cooldown right after boot
        self._last_exchange = float("-inf")
        self._locale: str | None = None
        self._devices: dict[str, Device] = {}
        self._service_urls: dict[str, str] | None = None
        self._history_csrf: tuple[str, float] | None = None
        self._endpoints_lock = asyncio.Lock()
        self._endpoints_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._endpoint_types: dict[str, str | None] = {}
        self._guard: Guard | None = None
        self._guard_known = False

    # ------------------------------------------------------------------ core

    @property
    def host(self) -> str:
        """Return the Alexa API host of the account."""
        return f"alexa.{self.credentials.domain}"

    @property
    def home_region(self) -> str:
        """Return the push region of the account ("NA", "EU" or "FE")."""
        return self.credentials.home_region or region_for_domain(
            self.credentials.domain
        )

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the aiohttp session, creating one when none was injected."""
        if self._session is None:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        return self._session

    async def close(self) -> None:
        """Close the session if this client created it."""
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> Self:
        """Enter the async context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit the async context."""
        await self.close()

    def _notify(self) -> None:
        if self._on_update is not None:
            self._on_update(self.credentials)

    async def async_get_access_token(self) -> str:
        """Return a bearer token, refreshing it when less than 60 s are left.

        Raises:
            AmazonEchoAuthenticationError: The refresh token was rejected.

        """
        creds = self.credentials
        if (
            not creds.access_token
            or creds.access_token_expires - _TOKEN_MARGIN < time.time()
        ):
            async with self._auth_lock:
                if (
                    not creds.access_token
                    or creds.access_token_expires - _TOKEN_MARGIN < time.time()
                ):
                    await refresh_access_token(self.session, creds)
                    self._notify()
        assert creds.access_token is not None  # noqa: S101
        return creds.access_token

    async def _session_dead(self) -> bool:
        """Return True only when Amazon refuses the refresh token itself."""
        try:
            async with self._auth_lock:
                await refresh_access_token(self.session, self.credentials)
        except AmazonEchoAuthenticationError:
            return True
        except AmazonEchoError:
            return False  # unreachable is not refused
        self._notify()
        return False

    async def _reauthorize(self, seen: int, *, bearer: bool) -> None:
        """Renew the cookies (and the bearer token) after a 401/403."""
        async with self._auth_lock:
            if self._generation != seen:
                return  # another request already renewed the session
            if time.monotonic() - self._last_exchange < _REAUTH_COOLDOWN:
                raise AmazonEchoAuthenticationError(
                    "Session rejected right after a cookie refresh"
                )
            await exchange_cookies(self.session, self.credentials)
            if bearer:
                await refresh_access_token(self.session, self.credentials)
            self._generation += 1
            self._last_exchange = time.monotonic()
            self._notify()

    async def _headers(self, url: str, *, bearer: bool, app: bool) -> dict[str, str]:
        headers = {
            "User-Agent": APP_USER_AGENT if app else USER_AGENT,
            "Accept": "application/json; charset=utf-8",
            "Accept-Language": self._locale or "en-US",
        }
        cookies = self.credentials.cookies
        # cookies only go to the account's own marketplace hosts
        if (urlsplit(url).hostname or "").endswith(self.credentials.domain):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            if cookies.get("csrf"):
                headers["csrf"] = cookies["csrf"]
        if bearer:
            headers["Authorization"] = f"Bearer {await self.async_get_access_token()}"
        return headers

    def _store_cookies(self, resp: aiohttp.ClientResponse) -> None:
        """Keep cookies the Alexa site sets (notably ``csrf``)."""
        if not (resp.url.host or "").endswith(self.credentials.domain):
            return
        changed = False
        for name, morsel in resp.cookies.items():
            if morsel.value and self.credentials.cookies.get(name) != morsel.value:
                self.credentials.cookies[name] = morsel.value
                changed = True
        if changed:
            self._notify()

    async def request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        json: Any = None,
        params: dict[str, str] | None = None,
        bearer: bool = False,
    ) -> Any:
        """Call the Alexa API and return the decoded JSON body (None if empty).

        Args:
            method: HTTP method.
            path: Path starting with ``/`` or a full ``https://`` URL.
            host: Host to use instead of ``alexa.<domain>``.
            json: JSON body.
            params: Query parameters.
            bearer: Also send the OAuth access token.

        Raises:
            AmazonEchoAuthenticationError: 401/403 that a cookie refresh did not fix.
            AmazonEchoRateLimitError: 429/503 after the retries.
            AmazonEchoConnectionError: Amazon could not be reached.
            AmazonEchoResponseError: Any other HTTP error, or a non-JSON body.

        """
        return await self._request(
            method, path, host=host, json=json, params=params, bearer=bearer
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        json: Any = None,
        params: dict[str, str] | None = None,
        bearer: bool = False,
        headers: dict[str, str] | None = None,
        app: bool = False,
        text: bool = False,
        retry: bool = True,
    ) -> Any:
        url = (
            path
            if path.startswith("https://")
            else f"https://{host or self.host}{path}"
        )
        delays = list(_RETRY_DELAYS) if retry else []
        reauthorized = False
        while True:
            seen = self._generation
            request_headers = await self._headers(url, bearer=bearer, app=app)
            request_headers.update(headers or {})
            try:
                async with self.session.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=request_headers,
                    timeout=_TIMEOUT,
                ) as resp:
                    status = resp.status
                    body = await resp.text()
                    signin = "/ap/signin" in resp.url.path
                    self._store_cookies(resp)
            except TimeoutError as err:
                raise AmazonEchoConnectionTimeoutError(
                    f"Timeout on {method} {path}"
                ) from err
            except aiohttp.ClientError as err:
                raise AmazonEchoConnectionError(
                    f"{method} {path} failed: {err}"
                ) from err

            if status in (401, 403) or signin:
                if not reauthorized:
                    reauthorized = True
                    try:
                        await self._reauthorize(seen, bearer=bearer)
                        continue
                    except AmazonEchoAuthenticationError:
                        pass  # refused or just renewed: decided below
                # A refusal of one route is not a dead session: cookies
                # propagate with a delay after a login, and some pages ask
                # for a fresh sign-in. Only a refused refresh token is final.
                if await self._session_dead():
                    raise AmazonEchoAuthenticationError(
                        f"{method} {path} rejected (HTTP {status})"
                    )
                raise AmazonEchoResponseError(
                    status, f"{method} {path} refused, the session is valid"
                )
            if status in (429, 503) or (status >= 400 and "Rate exceeded" in body):
                if not delays:
                    raise AmazonEchoRateLimitError(
                        f"{method} {path} is rate limited (HTTP {status})"
                    )
                await asyncio.sleep(delays.pop(0))
                continue
            if status >= 400:
                raise AmazonEchoResponseError(status, body[:500])
            if text:
                return body
            if not body.strip():
                return None
            try:
                return jsonlib.loads(body)
            except ValueError as err:
                # Unavailable routes answer 200 with an HTML page.
                raise AmazonEchoResponseError(status, body[:200]) from err

    async def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> Any:
        """Run a GraphQL document and return its ``data``.

        Partial answers (``data`` plus ``errors``) are accepted.
        """
        answer = await self._request(
            "POST",
            "/nexus/v1/graphql",
            json={"query": query, "variables": variables or {}},
            app=True,
        )
        data = answer.get("data") if isinstance(answer, dict) else None
        if data:
            return data
        errors = jsonlib.dumps((answer or {}).get("errors") or answer)[:500]
        if "TOO_MANY_REQUESTS" in errors:
            raise AmazonEchoRateLimitError("GraphQL is rate limited")
        raise AmazonEchoResponseError(200, errors)

    @staticmethod
    def _query(device: Device) -> dict[str, str]:
        return {"deviceSerialNumber": device.serial, "deviceType": device.device_type}

    # ------------------------------------------------- account / inventory

    async def get_account(self) -> Account:
        """Return the account behind the session."""
        data = await self._request("GET", "/api/users/me")
        account = Account(
            customer_id=data["id"],
            name=data.get("fullName"),
            email=data.get("email"),
            country=data.get("countryOfResidence"),
            marketplace_id=data.get("marketPlaceId"),
        )
        self._locale = data.get("marketPlaceLocale") or self._locale
        creds = self.credentials
        if (creds.customer_id, creds.email) != (account.customer_id, account.email):
            creds.customer_id = account.customer_id
            creds.email = account.email
            creds.customer_name = creds.customer_name or account.name
            self._notify()
        return account

    async def get_service_urls(self) -> dict[str, str]:
        """Return the service URLs Amazon assigns to the account (cached).

        Keys include ``alexaApiUrl`` (alerts API), ``bobDispatchUrl`` (HTTP/2
        push), ``websiteApiUrl`` and ``retailUrl``.
        """
        if self._service_urls is None:
            data = await self._request("GET", "/api/endpoints")
            self._service_urls = {
                key: str(value).rstrip("/")
                for key, value in (data or {}).items()
                if isinstance(value, str)
            }
        return self._service_urls

    async def _customer(self) -> tuple[str, str]:
        """Return (customer id, locale), fetching the account once."""
        if not self.credentials.customer_id or not self._locale:
            await self.get_account()
        assert self.credentials.customer_id is not None  # noqa: S101
        return self.credentials.customer_id, self._locale or "en-US"

    async def _raw_endpoints(self) -> list[dict[str, Any]]:
        """Return every GraphQL endpoint of the account.

        ponytail: the (large) answer is kept for 5 s so that the device states
        and the smart-home endpoints of one poll share a request; a feature
        call drops it.
        """
        async with self._endpoints_lock:
            cached = self._endpoints_cache
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
            data = await self._graphql(QUERY_ENDPOINTS)
            endpoints = [
                endpoint
                for endpoint in _dig(data, "listEndpoints", "endpoints") or []
                if endpoint.get("endpointId")
            ]
            self._endpoint_types = {
                endpoint["endpointId"]: _dig(endpoint, "dmsIdentifier", "deviceType")
                for endpoint in endpoints
            }
            self._endpoints_cache = (time.monotonic() + _ENDPOINTS_TTL, endpoints)
            return endpoints

    async def _endpoints(self) -> dict[str, dict[str, Any]]:
        """Return the GraphQL state of every Alexa-enabled endpoint by serial."""
        return {
            serial: parse_endpoint(endpoint)
            for endpoint in await self._raw_endpoints()
            if (serial := _dig(endpoint, "dmsIdentifier", "dsn"))
        }

    async def get_devices(self) -> list[Device]:
        """Return every Alexa device, app and speaker group of the account.

        The legacy device list is merged with the GraphQL endpoints (MAC,
        endpoint id, model name). A GraphQL failure other than a rejected
        session only costs those extra fields.
        """
        data = await self._request(
            "GET", "/api/devices-v2/device", params={"cached": "false"}
        )
        try:
            endpoints = await self._endpoints()
        except AmazonEchoAuthenticationError, AmazonEchoConnectionError:
            raise
        except AmazonEchoError as err:
            _LOGGER.debug("GraphQL endpoints unavailable: %s", err)
            endpoints = {}
        devices = []
        for item in data.get("devices") or []:
            extra = endpoints.get(item["serialNumber"], {})
            devices.append(
                Device(
                    serial=item["serialNumber"],
                    device_type=item["deviceType"],
                    name=item.get("accountName") or item["serialNumber"],
                    family=item.get("deviceFamily") or "UNKNOWN",
                    model=extra.get("model") or item.get("deviceTypeFriendlyName"),
                    software_version=item.get("softwareVersion"),
                    account_id=item.get("deviceAccountId"),
                    online=bool(item.get("online")),
                    capabilities=frozenset(item.get("capabilities") or ()),
                    cluster_members=tuple(item.get("clusterMembers") or ()),
                    parent_clusters=tuple(item.get("parentClusters") or ()),
                    mac=extra.get("mac") or format_mac(item.get("macAddress")),
                    endpoint_id=extra.get("endpoint_id"),
                    battery_level=item.get("remainingBatteryLevel"),
                    charging=item.get("charging"),
                )
            )
        self._devices = {device.serial: device for device in devices}
        return devices

    async def get_routines(self) -> list[Routine]:
        """Return the routines (automations) of the account."""
        data = await self._request(
            "GET", "/api/behaviors/v2/automations", params={"limit": "2000"}
        )
        routines = []
        for item in data or []:
            utterance = next(
                (
                    _dig(trigger, "payload", "utterance")
                    for trigger in item.get("triggers") or []
                    if _dig(trigger, "payload", "utterance")
                ),
                None,
            )
            routines.append(
                Routine(
                    id=item["automationId"],
                    name=item.get("name") or utterance or item["automationId"],
                    enabled=item.get("status") == "ENABLED",
                    raw=item,
                )
            )
        return routines

    async def get_music_providers(self) -> dict[str, str]:
        """Return the usable music providers as id → display name."""
        data = await self._request(
            "GET", "/api/behaviors/entities", params={"skillId": "amzn1.ask.1p.music"}
        )
        return {
            item["id"]: item["displayName"]
            for item in data or []
            if item.get("id")
            and item.get("displayName")
            and item.get("availability") == "AVAILABLE"
        }

    # ------------------------------------------------------------ bulk state

    async def get_dnd(self) -> dict[str, bool]:
        """Return do-not-disturb by device serial."""
        data = await self._request("GET", "/api/dnd/device-status-list")
        return {
            item["deviceSerialNumber"]: bool(item.get("enabled"))
            for item in data.get("doNotDisturbDeviceStatusList") or []
        }

    async def get_volumes(self) -> dict[str, tuple[int, bool]]:
        """Return (volume 0-100, muted) by device serial."""
        data = await self._request(
            "GET", "/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes"
        )
        return {
            item["dsn"]: (int(item["speakerVolume"]), bool(item.get("speakerMuted")))
            for item in data.get("volumes") or []
            if item.get("speakerVolume") is not None
        }

    async def get_bluetooth(self) -> dict[str, list[BluetoothDevice]]:
        """Return the paired Bluetooth devices by Echo serial."""
        data = await self._request("GET", "/api/bluetooth", params={"cached": "true"})
        return {
            state["deviceSerialNumber"]: [
                BluetoothDevice(
                    address=paired["address"],
                    name=paired.get("friendlyName"),
                    connected=bool(paired.get("connected")),
                    profiles=tuple(paired.get("profiles") or ()),
                )
                for paired in state.get("pairedDeviceList") or []
            ]
            for state in data.get("bluetoothStates") or []
        }

    async def get_notifications(self) -> list[Notification]:
        """Return every alarm, timer, reminder and music alarm.

        One (large, rate limited) call serves all kinds.
        """
        data = await self._request("GET", "/api/notifications")
        return [
            parse_notification(item)
            for item in data.get("notifications") or []
            if item.get("id")
        ]

    async def get_alarm_volumes(self) -> dict[str, int]:
        """Return the alarm/timer volume by device serial."""
        return {
            item["deviceSerialNumber"]: int(item["volumeLevel"])
            for item in await self._notification_states()
            if item.get("volumeLevel") is not None
        }

    async def _notification_states(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/device-notification-state")
        return data.get("deviceNotificationStates") or []

    async def _wake_word_models(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/wake-word", params={"cached": "true"})
        return data.get("wakeWords") or []

    async def get_wake_words(self) -> dict[str, str]:
        """Return the wake word by device serial."""
        return {
            item["deviceSerialNumber"]: item["wakeWord"]
            for item in await self._wake_word_models()
            if item.get("wakeWord")
        }

    async def get_ascending_alarms(self) -> dict[str, bool]:
        """Return whether the ascending alarm is on, by device serial."""
        data = await self._request("GET", "/api/ascending-alarm")
        return {
            item["deviceSerialNumber"]: bool(item.get("ascendingAlarmEnabled"))
            for item in data.get("ascendingAlarmModelList") or []
        }

    async def get_media(self, device: Device) -> MediaState:
        """Return what ``device`` is playing."""
        data = await self._request(
            "GET",
            "/api/np/player",
            params={**self._query(device), "screenWidth": "2560"},
        )
        info = (data or {}).get("playerInfo") or {}
        if info.get("state"):
            return _parse_media(info)
        # Spotify and other external players leave /api/np/player empty and
        # only show up as a media session (observed live).
        try:
            sessions = await self._request(
                "GET", "/api/np/list-media-sessions", params=self._query(device)
            )
        except AmazonEchoResponseError:
            return _parse_media(info)
        for session in (sessions or {}).get("mediaSessionList") or []:
            now = session.get("nowPlayingData") or {}
            if now.get("playerState"):
                return _parse_media(_session_as_player(now))
        return _parse_media(info)

    async def get_endpoint_states(self) -> dict[str, dict[str, Any]]:
        """Return the GraphQL state of every Alexa endpoint by device serial.

        Each value has ``temperature`` (°C), ``volume``, ``reachable``,
        ``remote_battery``, ``ambient_mode``, ``mac``, ``dnd``, ``model`` and
        ``endpoint_id``; values the device does not report are None. A device
        with more sensors also has ``illuminance``, ``motion``, ``humidity``
        and the air-quality keys of :func:`amazon_echo.endpoints.parse_features`.
        """
        return await self._endpoints()

    async def get_smart_home_endpoints(self) -> list[Endpoint]:
        """Return the smart-home devices linked to the account.

        That is every endpoint that is not an Alexa voice device: lamps,
        plugs, sensors and locks of other brands, plus Amazon hardware without
        a microphone (Echo Glow, Air Quality Monitor). Shares its request with
        :meth:`get_endpoint_states` when both are called within 5 s.
        """
        return [
            build_endpoint(endpoint)
            for endpoint in await self._raw_endpoints()
            if _dig(endpoint, "displayCategories", "primary", "value")
            != "ALEXA_VOICE_ENABLED"
        ]

    def parse_endpoint_update(
        self, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """Decode a smart-home push event into (endpoint id, changed values).

        ``payload`` is the one of a ``PushEvent`` whose command is
        ``EventBus:AlexaMobile::FDAL``; None when it is something else.
        """
        endpoint_id = _dig(payload, "entity", "id")
        features = _dig(payload, "data", "features")
        if not isinstance(endpoint_id, str) or not isinstance(features, list):
            return None
        return endpoint_id, parse_features(
            features, device_type=self._endpoint_types.get(endpoint_id)
        )

    # ------------------------------------------------- per-device settings

    async def get_wifi(self, device: Device) -> tuple[str | None, str | None]:
        """Return the (SSID, MAC) of the Wi-Fi connection of ``device``."""
        data = await self._request(
            "GET", "/api/device-wifi-details", params=self._query(device)
        )
        return (data or {}).get("essid"), format_mac((data or {}).get("macAddress"))

    async def get_equalizer(self, device: Device) -> dict[str, int]:
        """Return the bass/mid/treble levels (-6..6)."""
        data = await self._request(
            "GET", f"/api/equalizer/{device.serial}/{device.device_type}"
        )
        return {key: int(data[key]) for key in ("bass", "mid", "treble")}

    def _earcon_path(self, device: Device) -> str:
        return (
            f"/api/earcon/states/{device.device_type}/"
            f"{device.software_version}/{device.serial}"
        )

    async def get_earcons(self, device: Device) -> dict[str, bool]:
        """Return whether the start/end-of-request sounds are on."""
        data = await self._request("GET", self._earcon_path(device))
        return {
            "wake_word": bool(data.get("wakeWordEarconEnabled")),
            "end_of_speech": bool(data.get("endOfSpeechEarconEnabled")),
        }

    async def get_aux(self, device: Device) -> list[AuxPort]:
        """Return the line in/out ports of ``device``."""
        data = await self._request(
            "GET", f"/api/auxcontroller/{device.device_type}/{device.serial}/state"
        )
        return [
            AuxPort(
                id=port["id"],
                direction=port.get("direction") or "",
                plugged=bool(port.get("isPlugged")),
                enabled=bool(port.get("isEnabled")),
                active=port.get("inputActivity") == "ACTIVE",
            )
            for port in (data or {}).get("ports") or []
        ]

    def _setting_path(self, device: Device, key: str) -> str:
        if not device.account_id:
            raise AmazonEchoNotSupportedError(f"{device.name} has no settings")
        return f"/api/v1/devices/{device.account_id}/settings/{quote(key, safe='')}"

    async def get_setting(self, device: Device, key: str) -> Any:
        """Return the JSON-decoded value of a per-device setting.

        Raises:
            AmazonEchoNotSupportedError: The device has no such setting.

        """
        try:
            data = await self._request("GET", self._setting_path(device, key))
        except AmazonEchoResponseError as err:
            if err.status in (400, 404):
                raise AmazonEchoNotSupportedError(
                    f"{device.name} has no setting {key!r}"
                ) from err
            raise
        value = (data or {}).get("value")
        if not isinstance(value, str):
            return value
        try:
            return jsonlib.loads(value)
        except ValueError:
            return value

    async def get_available_wake_words(self, device: Device) -> list[str]:
        """Return the wake words ``device`` can use."""
        data = await self._request(
            "GET",
            "/api/wake-words",
            params={
                "deviceType": device.device_type,
                "softwareVersion": device.software_version or "",
            },
        )
        return list((data or {}).get("wakeWords") or [])

    async def get_alarm_sounds(self, device: Device) -> dict[str, str]:
        """Return the alarm sounds of ``device`` as id → display name."""
        data = await self._request(
            "GET",
            "/api/notification/sounds",
            params={
                "deviceType": device.device_type,
                "deviceSerialNumber": device.serial,
                "softwareVersion": device.software_version or "",
            },
        )
        return {
            sound["id"]: sound.get("displayName") or sound["id"]
            for sound in (data or {}).get("notificationSounds") or []
        }

    async def get_default_alarm_sound(self, device: Device) -> str | None:
        """Return the id of the default alarm sound of ``device``."""
        data = await self._request(
            "GET",
            "/api/notification/default-sound",
            params={
                "deviceType": device.device_type,
                "deviceSerialNumber": device.serial,
                "notificationType": "ALARM",
            },
        )
        sound: str | None = _dig(data, "defaultNotificationSound", "id")
        return sound

    def _comms_path(self, device: Device) -> str:
        # "devicesTypes" is Amazon's spelling
        return (
            f"/devicesTypes/{device.device_type}/deviceId/{device.serial}/preferences"
        )

    async def get_communication_preferences(self, device: Device) -> dict[str, Any]:
        """Return the communication preferences of ``device``.

        Keys, each only when Amazon allows the preference on the device:
        ``communications`` (bool), ``announcements`` (bool) and ``drop_in``
        (``all``, ``home`` or ``off``). One call per device, not retried.
        """
        data = await self._request(
            "GET",
            self._comms_path(device)
            + "?devicePreferences=communications&devicePreferences=dropin"
            "&devicePreferences=announcements",
            host=COMMS_HOST.format(domain=self.credentials.domain),
            app=True,
            retry=False,
        )
        result: dict[str, Any] = {}
        for item in (data or {}).get("devicePermissionsPreferences") or []:
            name, state = item.get("devicePreference"), item.get("state")
            if item.get("allowed") is not True or not isinstance(state, str):
                continue
            if name == "dropin":
                result["drop_in"] = state.lower()
            elif name in ("communications", "announcements"):
                result[name] = state == "ON"
        return result

    async def get_kids_mode(self, device: Device) -> bool:
        """Return whether ``device`` is in Amazon Kids mode."""
        data = await self._request(
            "GET", "/api/device/op-mode", params=self._query(device)
        )
        return bool((data or {}).get("isChildDirectedDevice"))

    @property
    def _parents_host(self) -> str:
        """Host of the Amazon Kids parent dashboard of the marketplace."""
        domain = self.credentials.domain
        return f"{_PARENTS_SUBDOMAINS.get(domain, 'parents')}.{domain}"

    async def get_kids_profile(self, device: Device) -> str | None:
        """Return the id of the child ``device`` is assigned to, if any."""
        data = await self._request(
            "GET",
            "/ajax/get-oobe-device-data",
            host=self._parents_host,
            params={"deviceId": device.serial, "deviceTypeId": device.device_type},
        )
        child: str | None = (data or {}).get("childDirectedId") or None
        return child

    # ------------------------------------------------- account preferences

    async def get_children(self) -> dict[str, str]:
        """Return the child profiles of the household as id → first name."""
        data = await self._request(
            "GET", "/ajax/get-household-with-age", host=self._parents_host
        )
        return {
            member["directedId"]: member.get("firstName") or member["directedId"]
            for member in (data or {}).get("members") or []
            if member.get("role") == "CHILD" and member.get("directedId")
        }

    async def get_device_preferences(self) -> dict[str, dict[str, Any]]:
        """Return ``locale``, ``time_zone`` and ``temperature_scale`` by serial."""
        data = await self._request(
            "GET", "/api/device-preferences", params={"cached": "true"}
        )
        return {
            item["deviceSerialNumber"]: {
                "locale": item.get("locale"),
                "time_zone": item.get("timeZoneId"),
                "temperature_scale": item.get("temperatureScale"),
            }
            for item in (data or {}).get("devicePreferences") or []
            if item.get("deviceSerialNumber")
        }

    async def get_whisper_mode(self) -> bool:
        """Return whether Alexa whispers back when whispered to."""
        data = await self._request("GET", "/api/response-modulation-preferences")
        return bool(data.get("whisperModulationEnabled"))

    async def get_adaptive_volume(self) -> bool:
        """Return whether Alexa speaks louder in noisy rooms."""
        data = await self._request("GET", "/api/adaptive-volume")
        return bool(data.get("adaptiveVolumeEnabled"))

    async def set_whisper_mode(self, enabled: bool) -> None:
        """Turn whisper mode on or off (account wide)."""
        await self._request(
            "POST",
            "/api/response-modulation-preferences",
            json={"whisperModulationEnabled": enabled},
        )

    async def set_adaptive_volume(self, enabled: bool) -> None:
        """Turn adaptive volume on or off (account wide)."""
        await self._request(
            "POST", "/api/adaptive-volume", json={"adaptiveVolumeEnabled": enabled}
        )

    # ---------------------------------------------------------------- writes

    async def set_dnd(self, device: Device, enabled: bool) -> None:
        """Turn do-not-disturb on or off."""
        await self._request(
            "PUT", "/api/dnd/status", json={**self._query(device), "enabled": enabled}
        )

    async def set_equalizer(
        self, device: Device, *, bass: int, mid: int, treble: int
    ) -> None:
        """Set the equalizer; every band is -6..6."""
        for value in (bass, mid, treble):
            if not -6 <= value <= 6:
                raise ValueError("Equalizer bands must be between -6 and 6")
        await self._request(
            "POST",
            f"/api/equalizer/{device.serial}/{device.device_type}",
            json={"bass": bass, "mid": mid, "treble": treble},
        )

    async def set_earcons(
        self, device: Device, *, wake_word: bool, end_of_speech: bool
    ) -> None:
        """Turn the start/end-of-request sounds on or off."""
        await self._request(
            "PUT",
            self._earcon_path(device),
            json={
                "wakeWordEarconEnabled": wake_word,
                "endOfSpeechEarconEnabled": end_of_speech,
            },
        )

    async def set_setting(self, device: Device, key: str, value: Any) -> None:
        """Write a per-device setting (the value is JSON-encoded for you).

        Raises:
            AmazonEchoNotSupportedError: The device has no such setting.

        """
        try:
            await self._request(
                "PUT",
                self._setting_path(device, key),
                json={"value": jsonlib.dumps(value)},
            )
        except AmazonEchoResponseError as err:
            if err.status == 400 and "does not exist" in err.body:
                raise AmazonEchoNotSupportedError(
                    f"{device.name} has no setting {key!r}"
                ) from err
            raise

    async def set_alarm_volume(self, device: Device, volume: int) -> None:
        """Set the alarm/timer volume (0-100)."""
        if not 0 <= volume <= 100:
            raise ValueError("Volume must be between 0 and 100")
        await self._request(
            "PUT",
            f"/api/device-notification-state/{device.device_type}/"
            f"{device.software_version}/{device.serial}",
            json={
                **self._query(device),
                "softwareVersion": device.software_version,
                "volumeLevel": volume,
            },
        )

    async def set_wake_word(self, device: Device, wake_word: str) -> None:
        """Change the wake word; the device applies it within about a minute.

        Raises:
            AmazonEchoNotSupportedError: The device has no wake word.

        """
        model = next(
            (
                item
                for item in await self._wake_word_models()
                if item.get("deviceSerialNumber") == device.serial
            ),
            None,
        )
        if model is None:
            raise AmazonEchoNotSupportedError(f"{device.name} has no wake word")
        await self._request(
            "PUT",
            f"/api/wake-word/{device.serial}",
            json={**model, "wakeWord": wake_word.strip().upper()},
        )

    async def set_ascending_alarm(self, device: Device, enabled: bool) -> None:
        """Turn the ascending (gradually louder) alarm on or off."""
        await self._request(
            "PUT",
            f"/api/ascending-alarm/{device.serial}",
            json={**self._query(device), "ascendingAlarmEnabled": enabled},
        )

    async def set_default_alarm_sound(self, device: Device, sound_id: str) -> None:
        """Set the default alarm sound."""
        await self._request(
            "PUT",
            "/api/notification/default-sound",
            json={
                "deviceType": device.device_type,
                "deviceSerialNumber": device.serial,
                "notificationType": "ALARM",
                "soundId": sound_id,
            },
        )

    async def set_aux_direction(
        self, device: Device, port_id: str, direction: str
    ) -> None:
        """Switch an AUX port between ``INPUT`` and ``OUTPUT``."""
        if direction not in ("INPUT", "OUTPUT"):
            raise ValueError("direction must be INPUT or OUTPUT")
        await self._request(
            "POST",
            f"/api/auxcontroller/{device.device_type}/{device.serial}"
            f"/ports/{quote(port_id, safe='')}/setDirection",
            json={"direction": direction},
        )

    async def bluetooth_connect(self, device: Device, address: str) -> None:
        """Connect ``device`` to an already paired Bluetooth device."""
        await self._request(
            "POST",
            f"/api/bluetooth/pair-sink/{device.device_type}/{device.serial}",
            json={"bluetoothDeviceAddress": address},
        )

    async def bluetooth_disconnect(self, device: Device) -> None:
        """Disconnect the current Bluetooth device of ``device``."""
        await self._request(
            "POST",
            f"/api/bluetooth/disconnect-sink/{device.device_type}/{device.serial}",
        )

    async def _set_comms(self, device: Device, preference: str, state: str) -> None:
        await self._request(
            "PATCH",
            f"{self._comms_path(device)}/{preference}",
            host=COMMS_HOST.format(domain=self.credentials.domain),
            json={"state": state},
            app=True,
        )

    async def set_communications(self, device: Device, enabled: bool) -> None:
        """Allow or block calls and messages on ``device``."""
        await self._set_comms(device, "communications", "ON" if enabled else "OFF")

    async def set_announcements(self, device: Device, enabled: bool) -> None:
        """Allow or block announcements on ``device``."""
        await self._set_comms(device, "announcements", "ON" if enabled else "OFF")

    async def set_drop_in(self, device: Device, mode: str) -> None:
        """Set who may drop in on ``device``: ``all``, ``home`` or ``off``."""
        if mode not in DROP_IN_MODES:
            raise ValueError("mode must be all, home or off")
        await self._set_comms(device, "dropin", DROP_IN_MODES[mode])

    async def set_kids_mode(self, device: Device, child_id: str | None) -> None:
        """Put ``device`` in Amazon Kids mode for a child, or take it out (None).

        ``child_id`` is a key of :meth:`get_children`.
        """
        if child_id is None:
            await self._request(
                "POST",
                f"/api/unassign-device-from-child/{device.serial}/{device.device_type}",
            )
            return
        host = self._parents_host
        start = f"/oobe/brownie/start/{device.device_type}/{device.serial}"
        # The dashboard has a CSRF token of its own, set by its setup page.
        await self._request("GET", start, host=host, text=True)
        await self._request(
            "POST",
            "/ajax/assign-device-to-child",
            host=host,
            json={
                "deviceId": device.serial,
                "deviceTypeId": device.device_type,
                "childDirectedId": child_id,
            },
            headers={
                "Referer": f"https://{host}{start}",
                "Origin": f"https://{host}",
                "x-amzn-csrf": self.credentials.cookies.get("ft-panda-csrf-token", ""),
            },
        )

    async def set_background_image(self, device: Device, url: str) -> None:
        """Show the image at ``url`` (https) as the background of an Echo Show."""
        await self._request(
            "POST",
            "/api/background-image",
            json={
                **self._query(device),
                "backgroundImageID": _BACKGROUND_IMAGE_ID,
                "backgroundImageType": "PERSONAL_PHOTOS",
                "backgroundImageURL": url,
            },
        )

    async def reboot(self, device: Device) -> None:
        """Restart ``device``."""
        await self._request(
            "POST",
            f"/v1/alexa/device/reboot/{device.device_type}/{device.serial}",
            json=self._query(device),
            bearer=True,
            app=True,
        )

    # --------------------------------------------------------- media / voice

    async def media_command(self, device: Device, command: str, **kwargs: Any) -> None:
        """Send a transport command.

        Args:
            device: The target device.
            command: ``play``, ``pause``, ``next``, ``previous``, ``forward``,
                ``rewind``, ``shuffle`` or ``repeat``.
            **kwargs: ``enabled=bool`` for ``shuffle`` and ``repeat``.

        """
        if command not in MEDIA_COMMANDS:
            raise ValueError(f"Unknown media command: {command}")
        body: dict[str, Any] = {"type": MEDIA_COMMANDS[command]}
        if command in ("shuffle", "repeat"):
            body[command] = bool(kwargs.get("enabled", True))
        await self._request(
            "POST", "/api/np/command", params=self._query(device), json=body
        )

    async def _run(self, *nodes: dict[str, Any]) -> None:
        """Run behavior nodes one after another through the preview endpoint."""
        sequence = {
            "@type": f"{_NODE}Sequence",
            "startNode": {"@type": f"{_NODE}SerialNode", "nodesToExecute": list(nodes)},
        }
        await self._request(
            "POST",
            "/api/behaviors/preview",
            json={
                "behaviorId": "PREVIEW",
                "sequenceJson": jsonlib.dumps(sequence),
                "status": "ENABLED",
            },
        )

    async def _node(self, device: Device, kind: str, **payload: Any) -> dict[str, Any]:
        customer_id, locale = await self._customer()
        return {
            "@type": f"{_NODE}OpaquePayloadOperationNode",
            "type": kind,
            "operationPayload": {
                **self._query(device),
                "locale": locale,
                "customerId": customer_id,
                **payload,
            },
        }

    async def set_volume(self, device: Device, volume: int) -> None:
        """Set the speaker volume (0-100)."""
        if not 0 <= volume <= 100:
            raise ValueError("Volume must be between 0 and 100")
        await self._run(
            await self._node(device, "Alexa.DeviceControls.Volume", value=volume)
        )

    async def stop(self, device: Device) -> None:
        """Stop whatever ``device`` is doing (audio, speech, alarms)."""
        await self._run(
            await self._node(
                device,
                "Alexa.DeviceControls.Stop",
                devices=[self._query(device)],
                skillId="amzn1.ask.1p.alexadevicecontrols",
            )
        )

    async def speak(self, device: Device, text: str) -> None:
        """Make ``device`` say ``text``."""
        customer_id, _ = await self._customer()
        await self._run(
            await self._node(
                device,
                "Alexa.Speak",
                textToSpeak=text,
                target={
                    "customerId": customer_id,
                    "devices": [
                        {
                            "deviceSerialNumber": device.serial,
                            "deviceTypeId": device.device_type,
                        }
                    ],
                },
                skillId="amzn1.ask.1p.saysomething",
            )
        )

    async def _members(self, devices: list[Device]) -> list[Device]:
        """Expand speaker groups into their member devices."""
        result: dict[str, Device] = {}
        for device in devices:
            if not device.cluster_members:
                result[device.serial] = device
                continue
            if any(serial not in self._devices for serial in device.cluster_members):
                await self.get_devices()
            for serial in device.cluster_members:
                if serial in self._devices:
                    result[serial] = self._devices[serial]
        return list(result.values())

    async def announce(
        self,
        devices: list[Device],
        text: str,
        *,
        title: str | None = None,
        method: str = "all",
    ) -> None:
        """Announce ``text`` (chime + speech + on-screen card) on ``devices``.

        ``method`` is ``all``, ``speak`` (nothing on screen) or ``show``
        (nothing spoken).
        """
        if method not in ("all", "speak", "show"):
            raise ValueError("method must be all, speak or show")
        targets = await self._members(devices)
        if not targets:
            raise ValueError("No device to announce on")
        customer_id, locale = await self._customer()
        await self._run(
            await self._node(
                targets[0],
                "AlexaAnnouncement",
                expireAfter="PT5S",
                content=[
                    {
                        "locale": locale,
                        "display": {"title": "", "body": ""}
                        if method == "speak"
                        else {"title": title or "Home Assistant", "body": text},
                        "speak": {
                            "type": "text",
                            "value": "" if method == "show" else text,
                        },
                    }
                ],
                target={
                    "customerId": customer_id,
                    "devices": [
                        {
                            "deviceSerialNumber": target.serial,
                            "deviceTypeId": target.device_type,
                        }
                        for target in targets
                    ],
                },
                skillId="amzn1.ask.1p.routines.messaging",
            )
        )

    async def text_command(self, device: Device, text: str) -> None:
        """Run ``text`` as if it had been said to ``device``."""
        await self._run(
            await self._node(
                device, "Alexa.TextCommand", skillId="amzn1.ask.1p.tellalexa", text=text
            )
        )

    async def play_sound(self, device: Device, sound_id: str) -> None:
        """Play a built-in sound (for example ``amzn_sfx_doorbell_chime_01``)."""
        await self._run(
            await self._node(
                device,
                "Alexa.Sound",
                soundStringId=sound_id,
                skillId="amzn1.ask.1p.sound",
            )
        )

    async def play_music(
        self,
        device: Device,
        provider_id: str,
        phrase: str,
        *,
        timer: int | None = None,
    ) -> None:
        """Play ``phrase`` from a music provider (see :meth:`get_music_providers`).

        Args:
            device: The device that plays.
            provider_id: A key of :meth:`get_music_providers`.
            phrase: What to search for.
            timer: Stop after this many seconds (a sleep timer).

        """
        payload: dict[str, Any] = {
            "searchPhrase": phrase,
            "sanitizedSearchPhrase": phrase,
            "musicProviderId": provider_id,
        }
        if timer:
            payload["waitTimeInSeconds"] = timer
        await self._run(
            await self._node(device, "Alexa.Music.PlaySearchPhrase", **payload)
        )

    async def launch_skill(self, device: Device, skill_id: str) -> None:
        """Open a skill on ``device``."""
        await self._run(
            await self._node(
                device,
                "Alexa.Operation.SkillConnections.Launch",
                targetDevice={
                    "deviceType": device.device_type,
                    "deviceSerialNumber": device.serial,
                },
                connectionRequest={
                    "uri": f"connection://AMAZON.Launch/{skill_id}",
                    "input": {},
                },
            )
        )

    async def send_sequence(
        self, device: Device, kind: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Run any behavior node type (``Alexa.Weather.Play``…) on ``device``."""
        await self._run(await self._node(device, kind, **(payload or {})))

    async def info_skill(self, device: Device, skill: str) -> None:
        """Make ``device`` tell something: a key of ``INFO_SKILLS``.

        ``weather``, ``traffic``, ``flash_briefing``, ``good_morning``,
        ``fun_fact``, ``joke``, ``tell_story``, ``calendar_today``…
        """
        if skill not in INFO_SKILLS:
            raise ValueError(f"Unknown info skill: {skill}")
        await self.send_sequence(device, INFO_SKILLS[skill])

    async def send_mobile_push(
        self, message: str, *, title: str | None = None, device: Device | None = None
    ) -> None:
        """Send a notification to the Alexa app on the account's phones.

        ``device`` only names the sender in the request; the notification
        always goes to the app.
        """
        customer_id, locale = await self._customer()
        payload: dict[str, Any] = {
            "customerId": customer_id,
            "notificationMessage": message,
            "alexaUrl": "#v2/behaviors",
            "title": title or "Home Assistant",
            "skillId": "amzn1.ask.1p.routines.messaging",
        }
        if device is not None:
            payload = {**self._query(device), "locale": locale, **payload}
        await self._run(
            {
                "@type": f"{_NODE}OpaquePayloadOperationNode",
                "type": "Alexa.Notifications.SendMobilePush",
                "operationPayload": payload,
            }
        )

    async def send_drop_in_notification(
        self, message: str, *, title: str | None = None
    ) -> None:
        """Send an Alexa app notification that opens the Drop In dialog."""
        customer_id, _ = await self._customer()
        await self._run(
            {
                "@type": f"{_NODE}OpaquePayloadOperationNode",
                "type": "Alexa.Notifications.DropIn",
                "skillId": "amzn1.ask.1p.action.dropin",
                "operationPayload": {
                    "customerId": customer_id,
                    "notificationMessage": message,
                    "alexaUrl": "#v2/comms/conversation-list?showDropInDialog=true",
                    "title": title or "Home Assistant",
                },
            }
        )

    async def run_routine(self, routine: Routine, device: Device | None = None) -> None:
        """Run a routine.

        Args:
            routine: The routine to run.
            device: The device that stands in for "the device I spoke to" in
                routines triggered by voice. Routines that do not use it run
                fine without.

        Raises:
            AmazonEchoNotSupportedError: The routine has no actions.

        """
        start = _dig(routine.raw, "sequence", "startNode")
        if not start:
            raise AmazonEchoNotSupportedError(f"Routine {routine.name} has no actions")
        if device is not None:
            _, locale = await self._customer()
            start = _replace_placeholders(
                start,
                {
                    "ALEXA_CURRENT_DSN": device.serial,
                    "ALEXA_CURRENT_DEVICE_TYPE": device.device_type,
                    "ALEXA_CURRENT_LOCALE": locale,
                },
            )
        await self._run(copy.deepcopy(start))

    # ------------------------------------------------------ routine editing

    async def get_routine_definition(self, routine_id: str) -> dict[str, Any]:
        """Return a routine as the editor reads it (triggers, sequence, status)."""
        data = await self._request("GET", f"/api/behaviors/automations/{routine_id}")
        if not isinstance(data, dict):
            raise AmazonEchoResponseError(200, f"Routine {routine_id} unreadable")
        return data

    @staticmethod
    def _routine_from(data: Any) -> Routine:
        if not isinstance(data, dict) or not data.get("automationId"):
            raise AmazonEchoResponseError(200, "No routine in the answer")
        return Routine(
            id=data["automationId"],
            name=data.get("name") or data["automationId"],
            enabled=data.get("status") == "ENABLED",
            raw=data,
        )

    async def create_routine(
        self,
        name: str,
        actions: list[dict[str, Any]],
        *,
        phrases: list[str] | None = None,
        triggers: list[dict[str, Any]] | None = None,
        enabled: bool = True,
    ) -> Routine:
        """Create a routine.

        Args:
            name: Name shown in the Alexa app.
            actions: Actions in order (see :mod:`amazon_echo.routines`:
                ``smart_home_action``, ``speak_action``) or one full sequence.
            phrases: "When you say" phrases (the usual trigger).
            triggers: Raw triggers, for other kinds (schedule, sensor…).
            enabled: Whether the routine starts enabled.

        """
        customer_id, locale = await self._customer()
        if phrases:
            account = await self.get_account()
            triggers = [
                *(triggers or []),
                routines.utterance_trigger(
                    phrases,
                    customer_id=customer_id,
                    locale=locale,
                    marketplace_id=account.marketplace_id,
                ),
            ]
        if not triggers:
            raise AmazonEchoNotSupportedError("A routine needs a trigger")
        body = routines.request_body(
            name=name,
            triggers=triggers,
            sequence=routines.fill_customer(
                routines.build_sequence(actions), customer_id, locale
            ),
            enabled=enabled,
        )
        return self._routine_from(
            await self._request("POST", "/api/behaviors/automations", json=body)
        )

    async def update_routine(
        self,
        routine_id: str,
        *,
        name: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        phrases: list[str] | None = None,
        enabled: bool | None = None,
        replace_targets: dict[str, str] | None = None,
    ) -> Routine:
        """Change a routine; whatever is not given stays as it is.

        Args:
            routine_id: The routine's ``automationId``.
            name: New name.
            actions: New actions (replace all of them).
            phrases: New "when you say" phrases (replace those of the
                first voice trigger).
            enabled: Enable or disable.
            replace_targets: Old → new smart-home device (endpoint ids or
                their UUIDs), e.g. to repair a routine after a lamp was
                replaced.

        """
        current = await self.get_routine_definition(routine_id)
        customer_id, locale = await self._customer()
        sequence = current.get("sequence") or {}
        if actions is not None:
            sequence = routines.fill_customer(
                routines.build_sequence(actions), customer_id, locale
            )
        if replace_targets:
            sequence = routines.replace_targets(sequence, replace_targets)
        triggers = copy.deepcopy(current.get("triggers") or [])
        if phrases:
            voice = next(
                (t for t in triggers if t.get("type") == "CustomUtterance"), None
            )
            if voice is None:
                raise AmazonEchoNotSupportedError("The routine has no voice trigger")
            voice["payload"]["utterance"] = phrases[0]
            voice["payload"]["utterances"] = phrases
        body = routines.request_body(
            name=name if name is not None else current.get("name") or "",
            triggers=triggers,
            sequence=sequence,
            enabled=current.get("status") == "ENABLED" if enabled is None else enabled,
            routine_id=routine_id,
        )
        return self._routine_from(
            await self._request(
                "PUT", f"/api/behaviors/automations/{routine_id}", json=body
            )
        )

    async def delete_routine(self, routine_id: str) -> None:
        """Delete a routine."""
        await self._request("DELETE", f"/api/behaviors/automations/{routine_id}")

    # ------------------------------------------ alarms / timers / reminders

    async def _find_new(
        self, device: Device, kind: str, known: set[str]
    ) -> Notification:
        """Return the notification that appeared on ``device`` since ``known``."""
        candidates = [
            item
            for item in await self.get_notifications()
            if item.device_serial == device.serial
            and item.kind == kind
            and item.id not in known
        ]
        if not candidates:
            raise AmazonEchoResponseError(200, f"The new {kind} did not show up")
        return max(candidates, key=lambda item: item.raw.get("createdDate") or 0)

    async def _alerts(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """Call the app's alerts API, which takes the raw access token."""
        url = (await self.get_service_urls()).get("alexaApiUrl")
        if not url:
            raise AmazonEchoNotSupportedError("The account has no alerts API")
        return await self._request(
            method,
            f"{url}/v1/alerts/{path}",
            json=body,
            headers={"authorization": await self.async_get_access_token()},
            app=True,
        )

    async def create_alarm(
        self,
        device: Device,
        when: datetime,
        *,
        label: str | None = None,
        recurrence: str | None = None,
        music: Notification | None = None,
    ) -> Notification:
        """Create an alarm.

        Args:
            device: The device that rings.
            when: First ring. Aware datetimes are converted to the system time
                zone; Amazon reads the wall-clock time in the Echo's own zone.
            label: Not supported by Amazon for alarms; accepted and ignored.
            recurrence: ``DAILY``, ``WEEKDAYS``, ``WEEKENDS``, ``WEEKLY`` or a
                day list such as ``MO,WE,FR``.
            music: An existing music alarm of the account; the new alarm
                wakes with the same station. Amazon has no public way to
                search for a station, so one that was picked in the Alexa app
                is reused (the ``MUSIC-<id>`` tone :meth:`enable_alarm` sends;
                this request has not been replayed against Amazon).

        Raises:
            AmazonEchoNotSupportedError: ``music`` is not a music alarm.

        """
        del label
        local = _local(when)
        trigger: dict[str, Any] = {"scheduledTime": local.isoformat(timespec="seconds")}
        if recurrence:
            trigger["recurrence"] = _recurrence_rule(recurrence, local)
        body: dict[str, Any] = {
            "trigger": trigger,
            "extensions": [],
            "endpointId": f"{device.serial}@{device.device_type}",
        }
        if music is not None:
            if not (music_id := music.raw.get("musicAlarmId")):
                raise AmazonEchoNotSupportedError("Not a music alarm")
            body["assets"] = [{"type": "TONE", "assetId": f"MUSIC-{music_id}"}]
        known = {item.id for item in await self.get_notifications()}
        await self._alerts("POST", "alarms", body)
        return await self._find_new(
            device, "alarm" if music is None else "music_alarm", known
        )

    async def create_reminder(
        self, device: Device, when: datetime, label: str
    ) -> Notification:
        """Create a reminder that ``device`` speaks at ``when``.

        Aware datetimes are converted to the system time zone; Amazon reads
        the wall-clock time in the Echo's own zone.
        """
        local = _local(when)
        epoch = when.timestamp() if when.tzinfo else local.astimezone().timestamp()
        data = await self._request(
            "PUT",
            "/api/notifications/createReminder",
            json={
                "type": "Reminder",
                "status": "ON",
                "alarmTime": int(epoch * 1000),
                "originalTime": local.strftime("%H:%M:%S.000"),
                "originalDate": local.strftime("%Y-%m-%d"),
                "timeZoneId": None,
                "reminderIndex": None,
                "sound": None,
                **self._query(device),
                "recurringPattern": "",
                "alarmLabel": None,
                "reminderLabel": label,
                "isSaveInFlight": True,
                "id": "createReminder",
                "isRecurring": False,
                "createdDate": int(time.time() * 1000),
            },
        )
        if isinstance(data, dict) and data.get("id"):
            return parse_notification(data)
        raise AmazonEchoResponseError(200, "No reminder in the answer")

    async def create_timer(
        self,
        device: Device,
        duration: timedelta,
        *,
        label: str | None = None,
        locale: str | None = None,
    ) -> Notification:
        """Start a timer on ``device``; Alexa confirms it out loud.

        Amazon has no API for this: the timers API of the app starts the timer
        on the app's own registration, not on an Echo. What works is a typed
        request with the duration in words (see :mod:`amazon_echo.phrases`),
        available for Portuguese and English.

        Args:
            device: The Echo that will run the timer.
            duration: More than zero and less than 24 hours.
            label: Optional name of the timer.
            locale: Locale of the device; read from Amazon when omitted.

        Raises:
            AmazonEchoNotSupportedError: No phrase for the device's language.
            AmazonEchoResponseError: Alexa did not start the timer.
            ValueError: The duration is out of range.

        """
        if locale is None:
            preferences = await self.get_device_preferences()
            locale = (preferences.get(device.serial) or {}).get("locale") or "en-US"
        request = timer_request(duration, locale, label=label)
        known = {item.id for item in await self.get_notifications()}
        await self.text_command(device, request)
        for _ in range(_TIMER_POLLS):
            await asyncio.sleep(_TIMER_POLL_SECONDS)
            try:
                return await self._find_new(device, "timer", known)
            except AmazonEchoResponseError:
                continue
        raise AmazonEchoResponseError(200, "The new timer did not show up")

    @staticmethod
    def _alarm_index(alarm: Notification) -> str:
        index = alarm.raw.get("notificationIndex")
        if alarm.kind not in ("alarm", "music_alarm") or not index:
            raise AmazonEchoNotSupportedError("Only alarms can be switched")
        return quote(str(index), safe="")

    async def enable_alarm(self, alarm: Notification) -> None:
        """Turn an alarm on; it rings at its next occurrence.

        The next ring is computed in the system time zone, which Amazon reads
        as the Echo's own (see :meth:`create_alarm`).

        Raises:
            AmazonEchoNotSupportedError: Not an alarm.
            ValueError: The alarm has no time or an unknown repeat rule.

        """
        body: dict[str, Any] = {
            "trigger": {"scheduledTime": _next_ring(alarm.raw, datetime.now())},  # noqa: DTZ005
            "extensions": [],
        }
        if music := alarm.raw.get("musicAlarmId"):
            body["assets"] = [{"type": "TONE", "assetId": f"MUSIC-{music}"}]
        elif alarm.sound:
            body["assets"] = [{"type": "TONE", "assetId": alarm.sound}]
        await self._alerts("PUT", f"alarms/{self._alarm_index(alarm)}/activate", body)

    async def disable_alarm(self, alarm: Notification) -> None:
        """Turn an alarm off without deleting it."""
        await self._alerts("PUT", f"alarms/{self._alarm_index(alarm)}/cancel")

    async def skip_next_alarm(self, alarm: Notification) -> None:
        """Skip the next occurrence of a repeating alarm."""
        await self._alerts(
            "PUT", f"alarms/{self._alarm_index(alarm)}/nextOccurrence/cancel"
        )

    async def delete_notification(self, notification: Notification) -> None:
        """Delete an alarm, timer or reminder."""
        await self._request(
            "DELETE",
            f"/api/notifications/{quote(notification.id, safe='')}",
            json=notification.raw or None,
        )

    # ----------------------------------------------------------------- lists

    async def _lists(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        return await self._request(
            method,
            f"/alexashoppinglists/api/v2/lists{path}",
            host=f"www.{self.credentials.domain}",
            json={} if json is None else json,
            params=params,
        )

    async def get_lists(self) -> list[EchoList]:
        """Return the shopping, to-do and custom lists."""
        data = await self._lists("POST", "/fetch")
        return [
            EchoList(
                id=item["listId"],
                name=item.get("listName")
                or _LIST_NAMES.get(item.get("listType") or "", item["listId"]),
                kind=LIST_KINDS.get(item.get("listType") or "", "custom"),
            )
            for item in (data or {}).get("listInfoList") or []
        ]

    async def get_list_items(self, list_id: str) -> list[ListItem]:
        """Return the items of a list (first 100)."""
        data = await self._lists(
            "POST", f"/{list_id}/items/fetch", params={"limit": "100"}
        )
        return [
            ListItem(
                id=item["itemId"],
                name=item.get("itemName") or "",
                completed=item.get("itemStatus") == "COMPLETE",
                version=int(item.get("version") or 1),
            )
            for item in (data or {}).get("itemInfoList") or []
        ]

    async def add_list_item(self, list_id: str, name: str) -> None:
        """Add an item to a list."""
        await self._lists(
            "POST",
            f"/{list_id}/items",
            json={"items": [{"itemType": "KEYWORD", "itemName": name}]},
        )

    async def update_list_item(
        self,
        list_id: str,
        item: ListItem,
        *,
        name: str | None = None,
        completed: bool | None = None,
    ) -> None:
        """Rename an item and/or mark it (not) done."""
        updates: list[dict[str, str]] = []
        if name is not None:
            updates.append({"type": "itemName", "value": name})
        if completed is not None:
            updates.append(
                {"type": "itemStatus", "value": "COMPLETE" if completed else "ACTIVE"}
            )
        if not updates:
            return
        await self._lists(
            "PUT",
            f"/{list_id}/items/{item.id}",
            params={"version": str(item.version)},
            json={"itemAttributesToUpdate": updates, "itemAttributesToRemove": []},
        )

    async def delete_list_item(self, list_id: str, item: ListItem) -> None:
        """Delete an item from a list."""
        await self._lists(
            "DELETE",
            f"/{list_id}/items/{item.id}",
            params={"version": str(item.version)},
        )

    # ------------------------------------------------ history / escape hatch

    async def _privacy_csrf(self) -> str:
        """Return the anti-CSRF token of the privacy hub (cached for 12 h)."""
        if self._history_csrf and self._history_csrf[1] > time.monotonic():
            return self._history_csrf[0]
        page = await self._request(
            "GET",
            "/alexa-privacy/apd/rvh",
            host=f"www.{self.credentials.domain}",
            text=True,
        )
        match = _CSRF_META.search(page or "")
        if not match:
            raise AmazonEchoResponseError(200, "No csrf-token in the privacy page")
        self._history_csrf = (match.group(1), time.monotonic() + _HISTORY_CSRF_TTL)
        return match.group(1)

    async def get_voice_history(
        self, *, since: datetime | None = None, limit: int = 10
    ) -> list[VoiceRecord]:
        """Return the latest voice interactions, newest first.

        Args:
            since: Only return records after this moment (default: 24 h ago).
            limit: Maximum number of records.

        """
        now = datetime.now(UTC)
        start = since or now - timedelta(days=1)
        if start.tzinfo is None:
            start = start.astimezone()
        host = f"www.{self.credentials.domain}"
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        found: dict[str, VoiceRecord] = {}  # Amazon repeats records across calls
        # Amazon serves 10 records per call, newest first. Its continuation
        # token is answered with HTTP 400, so older pages are fetched by
        # moving the end of the time window instead.
        for _ in range(_HISTORY_PAGES):
            data = await self._request(
                "POST",
                "/alexa-privacy/apd/rvh/customer-history-records-v2",
                host=host,
                params={
                    "startTime": str(start_ms),
                    "endTime": str(end_ms),
                    "recordType": "VOICE_HISTORY",
                    "maxRecordSize": "10",
                },
                json={"previousRequestToken": None},
                headers={
                    "anti-csrftoken-a2z": await self._privacy_csrf(),
                    "Referer": f"https://{host}/alexa-privacy/apd/rvh",
                },
            )
            page = (data or {}).get("customerHistoryRecords") or []
            for item in page:
                record = _parse_voice_record(item)
                if record is not None:
                    found[record.id] = record
            stamps = [
                int(item["timestamp"])
                for item in page
                if isinstance(item.get("timestamp"), (int, float))
            ]
            more = (data or {}).get("encodedRequestToken")
            if not more or not stamps or len(found) >= limit:
                break
            end_ms = min(stamps) - 1
            if end_ms <= start_ms:
                break
        records = sorted(found.values(), key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    async def call_feature(
        self,
        endpoint_id: str,
        feature: str,
        operation: str,
        *,
        instance: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Invoke a feature operation on any endpoint (GraphQL escape hatch).

        Example: ``call_feature(endpoint_id, "power", "turnOn")``.

        Raises:
            AmazonEchoResponseError: Amazon rejected the operation.

        """
        control: dict[str, Any] = {
            "endpointId": endpoint_id,
            "featureName": feature,
            "featureOperationName": operation,
        }
        if instance is not None:
            control["instance"] = instance
        if payload is not None:
            control["payload"] = payload
        self._endpoints_cache = None  # the state is about to change
        data = await self._graphql(
            MUTATION_SET_ENDPOINT_FEATURES,
            {"input": {"featureControlRequests": [control]}},
        )
        result = data.get("setEndpointFeatures") or {}
        if errors := result.get("errors"):
            raise AmazonEchoResponseError(
                200,
                "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors),
            )
        if not result.get("featureControlResponses"):
            raise AmazonEchoResponseError(200, "No response to the feature operation")

    # ----------------------------------------------------------- Alexa Guard

    async def get_guard(self) -> Guard | None:
        """Return the Alexa Guard panel with its state, None without Guard.

        ponytail: the panel is looked up once per client; an account that
        gets Guard later needs a new client (a reload of the integration).
        """
        if not self._guard_known:
            data = await self._graphql(QUERY_APPLIANCES)
            self._guard = next(
                (
                    Guard(
                        entity_id=appliance["entityId"],
                        appliance_id=appliance["applianceId"],
                    )
                    for item in _dig(data, "endpoints", "items") or []
                    if (appliance := item.get("legacyAppliance") or {}).get("modelName")
                    == GUARD_MODEL
                    and appliance.get("entityId")
                    and appliance.get("applianceId")
                ),
                None,
            )
            self._guard_known = True
        guard = self._guard
        if guard is None:
            return None
        data = await self._request(
            "POST",
            "/api/phoenix/state",
            json={
                "stateRequests": [{"entityId": guard.entity_id, "entityType": "ENTITY"}]
            },
        )
        guard.state = None
        for device_state in (data or {}).get("deviceStates") or []:
            for raw in device_state.get("capabilityStates") or []:
                try:
                    capability = jsonlib.loads(raw)
                except TypeError, ValueError:
                    continue
                if (
                    capability.get("namespace") == "Alexa.SecurityPanelController"
                    and capability.get("name") == "armState"
                ):
                    guard.state = capability.get("value")
        return guard

    async def set_guard(
        self, guard: Guard, armed: bool, device: Device | None = None
    ) -> None:
        """Arm Guard (away mode) or disarm it (home mode).

        Args:
            guard: The panel from :meth:`get_guard`.
            armed: True for away, False for home.
            device: An Echo with Guard (capability ``GUARD_EARCON``) to send
                the command through, as the Alexa app does. Without one the
                smart-home state API is used.

        """
        if device is not None and device.supports(GUARD_CAPABILITY):
            parts = guard.appliance_id.split("_")
            await self._run(
                await self._node(
                    device,
                    "controlGuardState",
                    target=parts[2] if len(parts) > 2 else guard.appliance_id,
                    operationId="controlGuardState",
                    state="AWAY" if armed else "HOME",
                    skillId=GUARD_SKILL_ID,
                )
            )
            return
        await self._request(
            "PUT",
            "/api/phoenix/state",
            json={
                "controlRequests": [
                    {
                        "entityId": guard.entity_id,
                        "entityType": "APPLIANCE",
                        "parameters": {
                            "action": "controlSecurityPanel",
                            "armState": "ARMED_AWAY" if armed else "ARMED_STAY",
                        },
                    }
                ]
            },
        )


def _parse_voice_record(item: dict[str, Any]) -> VoiceRecord | None:
    """Build a :class:`VoiceRecord` from a privacy-hub history record."""
    kind = str(item.get("utteranceType") or "")
    if kind in _SKIPPED_UTTERANCES or kind.startswith("FALSE_WAKE_WORD"):
        return None
    timestamp = _from_ms(item.get("creationTimestamp") or item.get("timestamp"))
    key = str(item.get("recordKey") or "")
    if timestamp is None or not key:
        return None
    utterance: list[str] = []
    response: list[str] = []
    for part in item.get("voiceHistoryRecordItems") or []:
        text = (part.get("transcriptText") or "").strip()
        part_kind = part.get("recordItemType") or ""
        if not text:
            continue
        if part_kind in ("CUSTOMER_TRANSCRIPT", "ASR_REPLACEMENT_TEXT"):
            utterance.append(text)
        elif part_kind in ("ALEXA_RESPONSE", "TTS_REPLACEMENT_TEXT"):
            response.append(text)
    persons = item.get("personsInfo") or []
    person = persons[0] if isinstance(persons, list) and persons else {}
    # recordKey = customerId#timestamp#deviceType#serial
    parts = key.split("#")
    return VoiceRecord(
        id=key,
        timestamp=timestamp,
        device_serial=parts[3] if len(parts) > 3 else None,
        utterance=" ".join(utterance) or None,
        response=" ".join(response) or None,
        intent=item.get("intent") if item.get("intent") != "Unknown" else None,
        person=person.get("personFirstName") if isinstance(person, dict) else None,
        person_type=person.get("personType") if isinstance(person, dict) else None,
    )

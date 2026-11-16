"""Data models returned by the client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass(slots=True, kw_only=True)
class Account:
    """The Amazon account behind the session."""

    customer_id: str
    name: str | None = None
    email: str | None = None
    country: str | None = None
    marketplace_id: str | None = None


@dataclass(slots=True, kw_only=True)
class Device:
    """An Alexa-enabled device, app or speaker group."""

    serial: str
    device_type: str
    name: str
    family: str
    model: str | None = None
    software_version: str | None = None
    account_id: str | None = None
    online: bool = False
    capabilities: frozenset[str] = frozenset()
    cluster_members: tuple[str, ...] = ()
    parent_clusters: tuple[str, ...] = ()
    mac: str | None = None
    endpoint_id: str | None = None
    battery_level: int | None = None
    charging: bool | None = None

    @property
    def is_group(self) -> bool:
        """Return True for a multi-room speaker group."""
        return self.family == "WHA"

    @property
    def is_app(self) -> bool:
        """Return True for an Alexa app instance (phone, browser)."""
        return self.family == "VOX"

    @property
    def key(self) -> str:
        """Return the stable identifier of the device."""
        return self.serial

    def supports(self, capability: str) -> bool:
        """Return True when the device advertises ``capability``."""
        return capability in self.capabilities


@dataclass(slots=True, kw_only=True)
class Notification:
    """An alarm, timer, reminder or music alarm."""

    id: str
    kind: str
    status: str
    device_serial: str
    device_type: str
    label: str | None = None
    when: datetime | None = None
    remaining: timedelta | None = None
    recurrence: str | None = None
    sound: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """Return True when the notification is armed."""
        return self.status == "ON"


@dataclass(slots=True, kw_only=True)
class BluetoothDevice:
    """A Bluetooth device paired with an Echo."""

    address: str
    name: str | None = None
    connected: bool = False
    profiles: tuple[str, ...] = ()


@dataclass(slots=True, kw_only=True)
class MediaState:
    """What a device is playing."""

    state: str = "idle"
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    image_url: str | None = None
    duration: float | None = None
    position: float | None = None
    position_updated_at: datetime | None = None
    provider: str | None = None
    shuffle: bool | None = None
    repeat: bool | None = None
    can_next: bool = False
    can_previous: bool = False
    can_seek: bool = False
    muted: bool = False
    volume: int | None = None


@dataclass(slots=True, kw_only=True)
class Routine:
    """An Alexa routine (automation)."""

    id: str
    name: str
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class EchoList:
    """A shopping, to-do or custom list."""

    id: str
    name: str
    kind: str


@dataclass(slots=True, kw_only=True)
class ListItem:
    """An item of a list."""

    id: str
    name: str
    completed: bool = False
    version: int = 1


@dataclass(slots=True, kw_only=True)
class VoiceRecord:
    """One voice interaction from the history."""

    id: str
    timestamp: datetime
    device_serial: str | None = None
    utterance: str | None = None
    response: str | None = None
    intent: str | None = None
    person: str | None = None
    person_type: str | None = None


@dataclass(slots=True, kw_only=True)
class AuxPort:
    """A line in/out port of an Echo."""

    id: str
    direction: str
    plugged: bool = False
    enabled: bool = False
    active: bool = False


@dataclass(slots=True, kw_only=True)
class Endpoint:
    """A smart-home device linked to the account (lamp, plug, sensor, lock…).

    ``state`` holds the values of ``parse_features``; ``operations`` the
    ``feature.operation`` pairs the endpoint accepts (``power.turnOn``).
    """

    id: str
    name: str
    category: str
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    device_type: str | None = None
    operations: frozenset[str] = frozenset()
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def is_amazon_device(self) -> bool:
        """Return True for Amazon hardware (Echo Glow, Air Quality Monitor)."""
        return self.serial is not None

    def supports(self, feature: str, operation: str) -> bool:
        """Return True when the endpoint accepts ``operation`` of ``feature``."""
        return f"{feature}.{operation}" in self.operations


@dataclass(slots=True, kw_only=True)
class Guard:
    """The Alexa Guard panel of the account."""

    entity_id: str
    appliance_id: str
    state: str | None = None

    @property
    def armed(self) -> bool:
        """Return True while Guard is in away mode."""
        return self.state == "ARMED_AWAY"

"""Async client for the Alexa cloud API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from .auth import Credentials
from .models import (
    Account,
    AuxPort,
    BluetoothDevice,
    Device,
    EchoList,
    ListItem,
    MediaState,
    Notification,
    Routine,
    VoiceRecord,
)


class AmazonEchoClient:
    """Client for the Alexa cloud API."""


    def __init__(self, credentials: Credentials, *, session: aiohttp.ClientSession | None = None, on_credentials_update: Callable[[Credentials], None] | None = None) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def close(self) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def request(self, method: str, path: str, *, host: str | None = None, json: Any = None, params: dict[str, str] | None = None, bearer: bool = False) -> Any:
        """Not implemented yet."""
        raise NotImplementedError


    async def async_get_access_token(self) -> str:
        """Not implemented yet."""
        raise NotImplementedError


    # --- account / inventory

    async def get_account(self) -> Account:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_devices(self) -> list[Device]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_routines(self) -> list[Routine]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_music_providers(self) -> dict[str, str]:
        """Not implemented yet."""
        raise NotImplementedError


    # --- bulk state (one call each, keyed by device serial)

    async def get_dnd(self) -> dict[str, bool]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_volumes(self) -> dict[str, tuple[int, bool]]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_bluetooth(self) -> dict[str, list[BluetoothDevice]]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_notifications(self) -> list[Notification]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_alarm_volumes(self) -> dict[str, int]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_wake_words(self) -> dict[str, str]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_ascending_alarms(self) -> dict[str, bool]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_media(self, device: Device) -> MediaState:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_endpoint_states(self) -> dict[str, dict[str, Any]]:
        """Not implemented yet."""
        raise NotImplementedError


    # --- per-device settings (slow tier)

    async def get_wifi(self, device: Device) -> tuple[str | None, str | None]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_equalizer(self, device: Device) -> dict[str, int]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_earcons(self, device: Device) -> dict[str, bool]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_aux(self, device: Device) -> list[AuxPort]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_setting(self, device: Device, key: str) -> Any:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_available_wake_words(self, device: Device) -> list[str]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_alarm_sounds(self, device: Device) -> dict[str, str]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_default_alarm_sound(self, device: Device) -> str | None:
        """Not implemented yet."""
        raise NotImplementedError


    # --- account preferences

    async def get_whisper_mode(self) -> bool:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_adaptive_volume(self) -> bool:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_whisper_mode(self, enabled: bool) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_adaptive_volume(self, enabled: bool) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    # --- writes

    async def set_dnd(self, device: Device, enabled: bool) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_equalizer(self, device: Device, *, bass: int, mid: int, treble: int) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_earcons(self, device: Device, *, wake_word: bool, end_of_speech: bool) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_setting(self, device: Device, key: str, value: Any) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_alarm_volume(self, device: Device, volume: int) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_wake_word(self, device: Device, wake_word: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_ascending_alarm(self, device: Device, enabled: bool) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_default_alarm_sound(self, device: Device, sound_id: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_aux_direction(self, device: Device, port_id: str, direction: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def bluetooth_connect(self, device: Device, address: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def bluetooth_disconnect(self, device: Device) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def reboot(self, device: Device) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    # --- media / voice (sequence API unless noted)

    async def media_command(self, device: Device, command: str, **kwargs: Any) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def set_volume(self, device: Device, volume: int) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def stop(self, device: Device) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def speak(self, device: Device, text: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def announce(self, devices: list[Device], text: str, *, title: str | None = None) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def text_command(self, device: Device, text: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def play_sound(self, device: Device, sound_id: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def play_music(self, device: Device, provider_id: str, phrase: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def launch_skill(self, device: Device, skill_id: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def run_routine(self, routine: Routine, device: Device | None = None) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    # --- alarms / timers / reminders

    async def create_alarm(self, device: Device, when: datetime, *, label: str | None = None, recurrence: str | None = None) -> Notification:
        """Not implemented yet."""
        raise NotImplementedError


    async def create_reminder(self, device: Device, when: datetime, label: str) -> Notification:
        """Not implemented yet."""
        raise NotImplementedError


    async def create_timer(self, device: Device, duration: timedelta, *, label: str | None = None) -> Notification:
        """Not implemented yet."""
        raise NotImplementedError


    async def delete_notification(self, notification: Notification) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    # --- lists

    async def get_lists(self) -> list[EchoList]:
        """Not implemented yet."""
        raise NotImplementedError


    async def get_list_items(self, list_id: str) -> list[ListItem]:
        """Not implemented yet."""
        raise NotImplementedError


    async def add_list_item(self, list_id: str, name: str) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def update_list_item(self, list_id: str, item: ListItem, *, name: str | None = None, completed: bool | None = None) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    async def delete_list_item(self, list_id: str, item: ListItem) -> None:
        """Not implemented yet."""
        raise NotImplementedError


    # --- history / escape hatch

    async def get_voice_history(self, *, since: datetime | None = None, limit: int = 10) -> list[VoiceRecord]:
        """Not implemented yet."""
        raise NotImplementedError


    async def call_feature(self, endpoint_id: str, feature: str, operation: str, *, instance: str | None = None, payload: dict[str, Any] | None = None) -> None:
        """Not implemented yet."""
        raise NotImplementedError

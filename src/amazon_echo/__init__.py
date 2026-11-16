"""Async Python client for Amazon Echo / Alexa devices."""

from __future__ import annotations

from .auth import (
    AuthorizeRequest,
    Credentials,
    build_authorize_url,
    deregister_device,
    register_device,
)
from .client import AmazonEchoClient
from .exceptions import (
    AmazonEchoAuthenticationError,
    AmazonEchoConnectionError,
    AmazonEchoConnectionTimeoutError,
    AmazonEchoError,
    AmazonEchoNotSupportedError,
    AmazonEchoRateLimitError,
    AmazonEchoResponseError,
)
from .local import LocalInfo, is_reachable, probe_local
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
from .push import PushClient, PushEvent

__version__ = "0.4.0"

__all__ = [
    "Account",
    "AmazonEchoAuthenticationError",
    "AmazonEchoClient",
    "AmazonEchoConnectionError",
    "AmazonEchoConnectionTimeoutError",
    "AmazonEchoError",
    "AmazonEchoNotSupportedError",
    "AmazonEchoRateLimitError",
    "AmazonEchoResponseError",
    "AuthorizeRequest",
    "AuxPort",
    "BluetoothDevice",
    "Credentials",
    "Device",
    "EchoList",
    "Endpoint",
    "Guard",
    "ListItem",
    "LocalInfo",
    "MediaState",
    "Notification",
    "PushClient",
    "PushEvent",
    "Routine",
    "VoiceRecord",
    "build_authorize_url",
    "deregister_device",
    "is_reachable",
    "probe_local",
    "register_device",
]

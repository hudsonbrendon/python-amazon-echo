"""Command line interface: ``python -m amazon_echo <command>``.

Credentials live in a JSON file (``--credentials``, default
``~/.config/amazon-echo/credentials.json``) that ``login`` creates.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
from typing import Any

from .auth import Credentials, build_authorize_url, register_device
from .client import AmazonEchoClient
from .exceptions import AmazonEchoError
from .local import probe_local
from .push import PushClient, PushEvent

_DEFAULT_PATH = Path.home() / ".config" / "amazon-echo" / "credentials.json"
_READS = {
    "account": "get_account",
    "devices": "get_devices",
    "states": "get_endpoint_states",
    "volumes": "get_volumes",
    "dnd": "get_dnd",
    "bluetooth": "get_bluetooth",
    "notifications": "get_notifications",
    "routines": "get_routines",
    "lists": "get_lists",
    "history": "get_voice_history",
    "providers": "get_music_providers",
}


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        data = asdict(value)
        data.pop("raw", None)
        return _plain(data)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


def _dump(value: Any) -> None:
    print(json.dumps(_plain(value), indent=2, ensure_ascii=False, default=str))


def _save(path: Path, credentials: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(credentials.to_dict()))
    path.chmod(0o600)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amazon-echo", description=__doc__)
    parser.add_argument("--credentials", type=Path, default=_DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="log in with your own browser")
    login.add_argument("--language", default="en-US")
    for name in _READS:
        sub.add_parser(name)
    sub.add_parser("push", help="print push events until interrupted")
    probe = sub.add_parser("probe", help="ask an Echo on the LAN who it is")
    probe.add_argument("host")
    return parser


async def _login(args: argparse.Namespace) -> None:
    request = build_authorize_url(language=args.language)
    print("1. Open this URL in your browser and log in to Amazon:\n")
    print(request.url)
    print(
        "\n2. The browser ends on a blank or 'page not found' page whose address "
        "contains /ap/maplanding. Copy that address and paste it here."
    )
    redirect = input("\nURL: ")
    credentials = await register_device(request, redirect)
    _save(args.credentials, credentials)
    print(f"Logged in to {credentials.domain}; saved to {args.credentials}")


async def _push(client: AmazonEchoClient) -> None:
    def on_event(event: PushEvent) -> None:
        print(event.command, event.device_serial, json.dumps(event.payload)[:200])

    push = PushClient(client, on_event, on_state=lambda up: print("connected:", up))
    await push.start()
    try:
        await asyncio.Event().wait()
    finally:
        await push.stop()


async def _run(args: argparse.Namespace) -> None:
    if args.command == "login":
        await _login(args)
        return
    if args.command == "probe":
        _dump(await probe_local(args.host))
        return
    credentials = Credentials.from_dict(json.loads(args.credentials.read_text()))
    async with AmazonEchoClient(
        credentials, on_credentials_update=lambda c: _save(args.credentials, c)
    ) as client:
        if args.command == "push":
            await _push(client)
        else:
            _dump(await getattr(client, _READS[args.command])())


def main() -> None:
    """Run the command line interface."""
    args = _build_parser().parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    except FileNotFoundError:
        sys.exit(f"No credentials at {args.credentials}; run `amazon-echo login`")
    except AmazonEchoError as err:
        sys.exit(f"error: {err}")


if __name__ == "__main__":
    main()

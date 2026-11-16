"""Print push events (volume, playback, do-not-disturb…) as they happen."""

import asyncio
import json
from pathlib import Path
import sys

from amazon_echo import AmazonEchoClient, Credentials, PushClient, PushEvent


def on_event(event: PushEvent) -> None:
    print(event.command, event.device_serial, event.payload)


async def main(path: Path) -> None:
    credentials = Credentials.from_dict(json.loads(path.read_text()))
    async with AmazonEchoClient(credentials) as client:
        push = PushClient(client, on_event, on_state=lambda up: print("up:", up))
        await push.start()
        try:
            await asyncio.sleep(300)
        finally:
            await push.stop()


if __name__ == "__main__":
    default = Path.home() / ".config" / "amazon-echo" / "credentials.json"
    asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else default))

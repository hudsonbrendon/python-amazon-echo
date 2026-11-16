"""Print every Echo with its volume, temperature and next alarm."""

import asyncio
import json
from pathlib import Path
import sys

from amazon_echo import AmazonEchoClient, Credentials


async def main(path: Path) -> None:
    credentials = Credentials.from_dict(json.loads(path.read_text()))
    async with AmazonEchoClient(credentials) as client:
        devices = await client.get_devices()
        states = await client.get_endpoint_states()
        alarms = [n for n in await client.get_notifications() if n.is_active]
        for device in devices:
            if device.is_app or device.is_group:
                continue
            state = states.get(device.serial, {})
            print(f"{device.name} ({device.model}) online={device.online}")
            print(f"  volume={state.get('volume')} temp={state.get('temperature')}")
            for alarm in alarms:
                if alarm.device_serial == device.serial:
                    print(f"  {alarm.kind} at {alarm.when} {alarm.label or ''}")


if __name__ == "__main__":
    default = Path.home() / ".config" / "amazon-echo" / "credentials.json"
    asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else default))

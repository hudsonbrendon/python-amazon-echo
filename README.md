# python-amazon-echo

[![Tests](https://github.com/hudsonbrendon/python-amazon-echo/actions/workflows/tests.yml/badge.svg)](https://github.com/hudsonbrendon/python-amazon-echo/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Async Python client for Amazon Echo / Alexa devices: device inventory, state,
settings, media, text-to-speech, alarms/timers/reminders, lists, voice history,
an HTTP/2 push channel and LAN discovery. It is the library behind the
`amazon_echo` Home Assistant integration.

It was written from a reverse-engineering pass over the Alexa app
(`com.amazon.dee.app` 2.2.730155.0), the Alexa web API and what an Echo exposes
on the local network. It is not affiliated with Amazon.

## Features

- **No password storage.** You log in in your own browser (passkeys, 2FA and
  captchas just work) and paste the final URL; the library keeps a refresh token
  and cookies. A session of the `alexa_media` integration can be imported too.
- **Self-healing session.** A 401/403 triggers one cookie exchange with the
  refresh token and a retry; 429/503 are retried with a short back-off; every
  change of tokens or cookies is reported through `on_credentials_update`.
- **Inventory and state in bulk.** Devices merged with the GraphQL endpoints
  (MAC, model, endpoint id), and one call each for volumes, do-not-disturb,
  Bluetooth, notifications, alarm volumes, wake words, ascending alarms and the
  GraphQL state (temperature, reachability, Fire TV remote battery, ambient mode).
- **Settings nobody else exposes.** Equalizer, request sounds (earcons), alarm
  volume, default alarm sound, ascending alarm, wake word, AUX direction, whisper
  mode, adaptive volume and any per-device setting key (speaking rate, time zone,
  captions, …).
- **Actions.** Volume, stop, speak, announce (spoken, shown or both), text
  command, sounds, music, skills, routines, the "tell me…" info skills (weather,
  traffic, flash briefing, a joke…), any other behavior node type, transport
  commands, Bluetooth, reboot, Echo Show background image.
- **Notifications to the Alexa app.** A mobile push, or one that opens Drop In.
- **Communication preferences.** Calls and messages, announcements and Drop In
  per device.
- **Amazon Kids.** The child profiles of the household, which one a device is
  assigned to, and switching a device into or out of Kids mode.
- **Alarms, timers and reminders.** List all, create, delete; switch an alarm
  on or off and skip its next ring.
- **Smart home.** Every device linked to the account (lamps, plugs, sensors,
  locks, Echo Glow, the Air Quality Monitor) with its state and the operations
  it accepts; sensors of the Echos themselves (illuminance, motion, humidity);
  state changes pushed by Amazon decoded into the same values.
- **Alexa Guard.** Read the panel, arm (away) and disarm (home).
- **Lists.** Shopping, to-do and custom lists with full item CRUD.
- **Voice history.**
- **Push.** One HTTP/2 stream delivers volume, playback, do-not-disturb,
  Bluetooth, notification, list and activity events; unknown events are passed
  through, never dropped.
- **LAN.** Identify an Echo by IP (name, model, multi-room groups, active Spotify
  user) and check that it is alive, without the cloud.
- Fully typed (`py.typed`), `aiohttp` with an injectable session, `httpx[http2]`
  for push.

## Installation

```bash
pip install python-amazon-echo
```

Python 3.14 or newer.

## Quick start

```python
import asyncio
import json
from pathlib import Path

from amazon_echo import AmazonEchoClient, Credentials

PATH = Path("credentials.json")


def save(credentials: Credentials) -> None:
    PATH.write_text(json.dumps(credentials.to_dict()))


async def main() -> None:
    credentials = Credentials.from_dict(json.loads(PATH.read_text()))
    async with AmazonEchoClient(credentials, on_credentials_update=save) as client:
        devices = await client.get_devices()
        volumes = await client.get_volumes()
        for device in devices:
            print(device.name, device.model, device.online, volumes.get(device.serial))

        echo = next(d for d in devices if d.family == "ECHO")
        await client.set_volume(echo, 30)
        await client.speak(echo, "Hello from Python")


asyncio.run(main())
```

Push events:

```python
from amazon_echo import PushClient, PushEvent


def on_event(event: PushEvent) -> None:
    print(event.command, event.device_serial, event.payload)


push = PushClient(client, on_event, on_state=lambda up: print("push up:", up))
await push.start()  # background task, reconnects with back-off 5·2ⁿ s (≤ 600 s)
...
await push.stop()
```

Smart home:

```python
for endpoint in await client.get_smart_home_endpoints():
    print(endpoint.name, endpoint.category, endpoint.state)

lamp = next(e for e in await client.get_smart_home_endpoints() if e.category == "LIGHT")
if lamp.supports("brightness", "setBrightness"):
    await client.call_feature(
        lamp.id, "brightness", "setBrightness", payload={"brightness": 40}
    )
```

`endpoint.state` uses the keys documented in
`amazon_echo.endpoints.parse_features` (`power`, `brightness`, `color`,
`color_temperature`, `temperature`, `humidity`, `battery`, `contact`, `motion`,
`lock`…); a key is there when the device has the feature, and is None while
Amazon cannot read it. `get_endpoint_states()` and `get_smart_home_endpoints()`
share one request when called within 5 seconds.

LAN:

```python
from amazon_echo import is_reachable, probe_local

info = await probe_local("192.168.1.50")  # None if it is not an Amazon device
print(info.name, info.model, info.groups, info.spotify_user)
print(await is_reachable("192.168.1.50"))
```

## Logging in with your own browser

```python
from amazon_echo import build_authorize_url, register_device

request = build_authorize_url(language="en-US")
print(request.url)  # 1. open it and log in to Amazon
redirect = input("Paste the final URL: ")  # 2. see below
credentials = await register_device(request, redirect)
```

1. Open `request.url` in any browser and log in as usual (password, passkey,
   2FA, captcha — whatever Amazon asks for). The login always starts on
   amazon.com, also for accounts of other marketplaces.
2. When the login is done the browser lands on a blank or "page not found" page
   whose address starts with `https://www.amazon.com/ap/maplanding?` and contains
   `openid.oa2.authorization_code=…`. That is expected. Copy the whole address.
3. Pass it to `register_device`. The one-time code is exchanged for a device
   registration ("Amazon Echo for Home Assistant" shows up in your Amazon
   account under *Manage your content and devices*), the account's own
   marketplace is discovered and cookies for it are fetched.

Keep the same `AuthorizeRequest` between steps 1 and 3 (it carries the PKCE
verifier), and persist `credentials.to_dict()`.

The command line does the same: `amazon-echo login`.

To reuse a session of the `alexa_media` integration instead:

```python
credentials = Credentials.from_alexa_media(entry_data, cookies)
# entry_data: the `data` of the alexa_media config entry
# cookies: the "cookies" list of .storage/alexa_media.<email>.cookies
```

Such a session shares one device registration with `alexa_media`. Everything
works except push while both run (see *Known limitations*).

## Command line

```bash
amazon-echo login
amazon-echo devices          # also: account, states, volumes, dnd, bluetooth,
                             # notifications, routines, lists, history, providers
amazon-echo push             # print push events
amazon-echo probe 192.168.1.50
```

Credentials are read from `~/.config/amazon-echo/credentials.json`
(`--credentials` to change).

## Verification

How each part was checked against a real amazon.com.br account on 2026-11-15
(the 0.2.0 additions on 2026-11-16). Everything is also covered by unit tests
with mocked HTTP.

- **live read** — called against Amazon through this library, answer parsed.
- **no-op write** — the value just read was written back to one Echo Plus and
  read again; Amazon accepted the request.
- **ported** — implemented from the app / existing open-source clients and unit
  tested only. Not replayed live (audible or disruptive).

| API | Status |
|---|---|
| `request`, 429/503 retry, non-JSON detection | live read |
| `async_get_access_token` (`auth/token`) | live (token refreshed) |
| Cookie exchange on 401/403 (`auth/token`, `auth_cookies`) | ported — never triggered live |
| `build_authorize_url`, `register_device` | ported (the authorize URL answers 302 as expected; no registration was made) |
| `get_account`, `get_devices`, `get_routines`, `get_music_providers`, `get_service_urls` | live read |
| `get_dnd`, `get_volumes`, `get_bluetooth`, `get_notifications`, `get_alarm_volumes`, `get_wake_words`, `get_ascending_alarms`, `get_endpoint_states` | live read |
| `get_media` | verified live on 2026-11-16 with Spotify: playing, title, provider and position, then paused when the sleep timer ran out |
| `get_wifi`, `get_equalizer`, `get_earcons`, `get_aux`, `get_setting`, `get_available_wake_words`, `get_alarm_sounds`, `get_default_alarm_sound` | live read |
| `get_whisper_mode`, `get_adaptive_volume` | live read |
| `set_whisper_mode`, `set_adaptive_volume` | no-op write during the reverse engineering (same request), not replayed by the library |
| `set_dnd`, `set_equalizer`, `set_earcons`, `set_alarm_volume`, `set_ascending_alarm`, `set_default_alarm_sound`, `set_setting` | no-op write |
| `set_aux_direction` | no-op write accepted; the body field name is unconfirmed |
| `set_volume` (sequence pipeline) | no-op write |
| `create_reminder`, `delete_notification` | live (a reminder 320 days ahead was created and deleted) |
| `create_alarm` | music alarm verified live on 2026-11-16 (`music=`, reusing a Spotify music alarm made by voice): created on an Echo Plus with the requested date and time, then deleted. A plain alarm is ported, not replayed live |
| `create_alarm(music=…)` (music alarm that reuses the station of an existing one) | ported from Alexa Media Player's request, never sent |
| `play_music(timer=…)` (stop after that many seconds) | verified live on 2026-11-16 (Spotify): the music stopped by itself, though not at the exact second (25 s asked → stopped at 26 s; 20 s asked → about 38 s) |
| `create_timer` | verified live on an Echo Plus (pt-BR): a 12-minute timer, with and without a name, created and deleted. It is a typed request with the duration in words; English phrases are unit-tested only |
| `set_wake_word` | ported |
| `speak`, `announce`, `text_command`, `play_sound`, `play_music`, `info_skill`, `set_volume` | verified live on 2026-11-16 on an Echo Plus, through Home Assistant; the voice history shows each request and Alexa's answer |
| `launch_skill`, `run_routine`, `stop`, `send_drop_in_notification` | ported, not replayed live |
| `send_mobile_push` | verified live on 2026-11-16: delivered to the owner's phone once the Alexa app was allowed to show notifications. Before that Amazon accepted every request and nothing arrived, and the app's registration listed no notification types — check that first if a notification does not show up |
| `media_command`, `bluetooth_connect`, `bluetooth_disconnect`, `reboot` | ported |
| `get_lists`, `get_list_items` | live read |
| `add_list_item`, `update_list_item`, `delete_list_item` | ported |
| `get_voice_history` | live read |
| `call_feature` | verified live on 2026-11-16 (see the two rows below) |
| `get_endpoint_states` with the 0.2.0 query (illuminance, motion, humidity, air quality) | live read: a few hundred endpoints, no GraphQL error. Four devices advertise a light sensor and report no value; no device of the account reports motion, humidity or air quality, so those are unit tested only |
| `get_smart_home_endpoints` | live read; power, brightness, colour, colour temperature, temperature, battery, contact, motion and lock states and the operation names all parsed |
| `parse_endpoint_update` (smart-home push) | ported, not replayed live — no push stream on the test session |
| Smart-home operations through `call_feature` | `turnOn`, `turnOff`, `setBrightness`, `setColor`, `setColorTemperature` and `setMode` verified live on 2026-11-16 on Tuya lamps linked to Alexa (Amazon reported each new state; the lamps were put back as they were). `lock` and `unlock` are ported, not replayed live: a door lock was not touched |
| Fire TV ambient mode (`call_feature(…, "toggle", "turnOn"/"turnOff", instance="FTV.AmbientMode")`) | verified live on 2026-11-16 on a Fire TV Stick 4K Max: ADB showed the ambient app in front after `turnOn` and the previous app back after `turnOff`. Amazon lists the feature but never reports its state, so `get_endpoint_states` gives `ambient_mode_supported` with `ambient_mode` None |
| `get_communication_preferences` | verified live on an Echo Plus. The service is flaky for this account: on 2026-11-16 it answered HTTP 503 to roughly nine requests out of ten, on both the `.com` and the marketplace host |
| `set_communications`, `set_announcements`, `set_drop_in` | verified live on 2026-11-16 with no-op writes (the value just read) on an Echo Plus, read back unchanged; each took up to six attempts because of the 503s above |
| `get_device_preferences`, `get_kids_mode`, `get_children`, `get_kids_profile` | live read |
| `set_kids_mode` | ported, not replayed live — it changes how a device behaves |
| `get_guard` | live for an account without Guard (the appliance list is read, no panel is found, None); the state read is ported — the test account has no Guard |
| `set_guard` | ported, not replayed live — Guard was not touched |
| `send_sequence`, `announce(method=…)` | ported, not replayed live |
| `enable_alarm`, `disable_alarm`, `skip_next_alarm` | ported, not replayed live |
| `set_background_image` | **does not work any more**: tried live on 2026-11-16 on an Echo Show 5, Amazon answers 200 but the background stays Amazon's (`/api/background-image` still reads `CURATIONS_SEASONAL`); a PUT answers 404. The current Alexa app uploads the picture through a native module instead. Kept for accounts where it still works |
| `PushClient` | handshake live (HTTP/2, 200, multipart boundary). the stream is then closed within ~1 s because the test session shares its registration with a running `alexa_media` (one stream per registration, see *Known limitations*); event parsing is unit tested only |
| `probe_local`, `is_reachable` | live (two Echo models answer, a smart TV of another brand returns None) |

## Known limitations

- **An Echo has no local control API.** On the LAN it only answers the Spotify
  Connect zeroconf endpoint (TCP 4070), advertises itself as a Matter controller
  and Thread border router, and accepts TCP on its multi-room audio port (55442).
  "Local" in this library therefore means discovery, name/model/group lookup and
  liveness. Every command and every other piece of state goes through Amazon's
  cloud; push only removes the polling.
- **Push with an imported `alexa_media` session.** Amazon keeps one push stream per
  app registration and the newest connection wins. Verified on 2026-11-15 with
  `alexa_media` debug logging on: opening our stream made `alexa_media` log
  `HTTP2 Connection Closed`, it reconnected at once, and that closed ours about a
  second later. So do not run `PushClient` on an imported session while
  `alexa_media` is running; poll instead, or sign in with the browser flow to get
  an own registration. `PushClient` does not re-declare AVS capabilities for
  imported sessions.
- **Alarm and reminder times** are read by Amazon in the Echo's own time zone and
  the API takes no zone. Aware datetimes are converted to the system time zone;
  pass a naive datetime in the Echo's local time if the host lives elsewhere.
- **Alarms have no label** in Amazon's API; `create_alarm(label=…)` is accepted
  and ignored.
- **Music alarms** can only reuse a station: `create_alarm(music=…)` takes a
  music alarm that already exists on the account (create one in the Alexa app
  first) and wakes with the same station. Amazon has no public search for one.
- **`create_timer`** speaks: Alexa has no API to start a timer on an Echo (the
  app's timers API starts it on the app's own registration, which was observed
  live), so the request is typed with the duration in words and Alexa confirms
  it out loud. Portuguese and English only; other languages raise
  `AmazonEchoNotSupportedError`.
- **Voice history** comes 10 records per call; at most 50 are fetched.
- **Lists** return the first 100 items.
- **`get_notifications`** is a large, rate-limited answer; call it sparingly (push
  sends `PUSH_NOTIFICATION_CHANGE`).
- `devices-v2` no longer reports MAC addresses or battery levels for most
  devices; the MAC comes from GraphQL and is missing for apps and groups.
- **Communication preferences** come from a service that fails for some
  accounts (HTTP 503 on the test account). The call is not retried and raises
  `AmazonEchoRateLimitError`; treat it as "not available for this device".
- **Guard** is looked up once per client: an account that gets Guard later is
  noticed by a new client. The free version of Guard was retired by Amazon in
  most countries; the code follows `alexapy` and could not be run.
- **Smart-home state** is what Amazon last heard from the device's own cloud.
  A value Amazon cannot read is None, and `reachable` is False for a device its
  cloud reports offline.
- **Locks.** Amazon offers `unlock` only for some locks; check
  `endpoint.supports("lock", "unlock")`.
- **Deleting voice history is not implemented.** The only endpoint the existing
  clients use for it, `/api/activities`, answers 404 since Amazon moved the
  history to the privacy hub.
- Amazon can change or remove any of this without notice.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```

Test fixtures are hand-written; no real account data is in this repository.

## License

MIT

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.3] - 2026-11-16

### Fixed

- `get_media` reported Spotify (and other external players) as idle:
  `/api/np/player` stays empty for them, so it now falls back to
  `/api/np/list-media-sessions`. Found and verified live.

## [0.3.2] - 2026-11-16

### Fixed

- Music alarms had no time (`when` was `None`): Amazon sends `alarmTime: 0` for
  them, so the time now comes from `originalDate` + `originalTime`. Found live.

## [0.3.1] - 2026-11-16

### Fixed

- `create_timer` started the timer on the app's own registration instead of the
  Echo (the app's timers API has no target device). It is now a typed request
  with the duration in words, verified live in Portuguese; `locale` argument
  added. Portuguese and English only.

## [0.3.0] - 2026-11-16

### Added

- Music alarms: `create_alarm(music=…)` takes an existing music alarm of the
  account and creates a new one with the same station.
- `play_music(timer=…)`: the music stops after that many seconds.

## [0.2.0] - 2026-11-16

### Added

- Smart home: `get_smart_home_endpoints()` returns every device linked to the
  account as an `Endpoint` (category, manufacturer, model, the operations it
  accepts and its state: power, brightness, colour, colour temperature,
  temperature, humidity, battery, contact, motion, lock, light shows).
  `get_endpoint_states()` now also reports `illuminance`, `motion`, `humidity`
  and, for the Amazon Air Quality Monitor, `pm25`, `pm10`, `voc`, `co`,
  `air_quality` and `aqm_led`. Both share one GraphQL request when called
  within 5 s. `parse_endpoint_update()` decodes the smart-home push event.
- Communication preferences per device: `get_communication_preferences`,
  `set_communications`, `set_announcements`, `set_drop_in`.
- Alexa Guard: `get_guard`, `set_guard` and the `Guard` model.
- Sequences: `info_skill` (17 "tell me…" skills, see `INFO_SKILLS`),
  `send_sequence` for any node type, `send_mobile_push`,
  `send_drop_in_notification`, and `announce(method="all" | "speak" | "show")`.
- Alarms: `enable_alarm`, `disable_alarm`, `skip_next_alarm`.
- Amazon Kids: `get_children`, `get_kids_mode`, `get_kids_profile`,
  `set_kids_mode`.
- `get_device_preferences` (locale, time zone, temperature scale by device),
  `set_background_image`.
- `VoiceRecord.person_type`.

### Changed

- A temperature of -280 (what Amazon reports while a sensor calibrates) is
  returned as None.

## [0.1.1] - 2026-11-16

### Fixed

- A 401/403 within 60 s of system boot was treated as "rejected right after a
  cookie refresh", because the cooldown started from `time.monotonic() == 0`.
  Found by CI on a freshly booted runner.

## [0.1.0] - 2026-11-15

### Added

- `AmazonEchoClient`: cookie + `csrf` authentication, one cookie exchange and
  retry on 401/403, back-off on 429/503, typed errors, `on_credentials_update`.
- Login without password storage: `build_authorize_url` + `register_device`
  (external browser, PKCE) and `Credentials.from_alexa_media`.
- Inventory: account, devices merged with the GraphQL endpoints (MAC, model,
  endpoint id), routines, music providers.
- Bulk state: do-not-disturb, volumes, Bluetooth, notifications, alarm volumes,
  wake words, ascending alarms, GraphQL endpoint state (temperature,
  reachability, Fire TV remote battery and ambient mode), now playing.
- Per-device settings: Wi-Fi, equalizer, earcons, AUX ports, alarm sounds,
  default alarm sound, available wake words and any setting key.
- Account preferences: whisper mode, adaptive volume.
- Actions through the sequence API: volume, stop, speak, announce, text command,
  sound, music, skill launch, routines; transport commands, Bluetooth
  connect/disconnect, reboot.
- Alarms, timers and reminders: list, create, delete.
- Lists: shopping, to-do and custom lists with item create/update/delete.
- Voice history.
- `call_feature`: GraphQL `setEndpointFeatures` escape hatch.
- `PushClient`: HTTP/2 push stream with reconnect back-off; every event is
  passed through.
- `probe_local` / `is_reachable`: LAN identification and liveness.
- `amazon-echo` command line interface.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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

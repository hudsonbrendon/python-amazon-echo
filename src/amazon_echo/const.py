"""Protocol constants: app identity, AVS capabilities and GraphQL documents."""

from __future__ import annotations

from typing import Any, Final

VERSION: Final = "0.1.0"

# Identity of the Alexa iOS app, which is what a registration impersonates.
APP_NAME: Final = "Amazon Echo for Home Assistant"
APP_VERSION: Final = "2.2.663733.0"
APP_BUNDLE_ID: Final = "com.amazon.echo"
APP_DEVICE_TYPE: Final = "A2IVLV5VM2W81"
APP_OS_VERSION: Final = "18.5"
APP_SOFTWARE_VERSION: Final = "35602678"
SDK_VERSION: Final = "6.12.4"

USER_AGENT: Final = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    f"(KHTML, like Gecko) Mobile/15E148 PitanguiBridge/{APP_VERSION}-"
    f"[HARDWARE=iPhone14_7][SOFTWARE={APP_OS_VERSION}][DEVICE=iPhone]"
)
APP_USER_AGENT: Final = (
    f"AmazonWebView/AmazonAlexa/{APP_VERSION}/iOS/{APP_OS_VERSION}/iPhone"
)

AUTH_HOST: Final = "api.amazon.com"
AVS_HOST: Final = "api.amazonalexa.com"
PUSH_HOST: Final = "bob-dispatch-prod-{region}.amazon.com"
PUSH_VERSION: Final = "v20160207"

# Retail domains whose accounts live outside the EU push region.
REGION_BY_DOMAIN: Final[dict[str, str]] = {
    "amazon.com": "NA",
    "amazon.ca": "NA",
    "amazon.com.mx": "NA",
    "amazon.com.br": "NA",
    "amazon.co.jp": "FE",
    "amazon.com.au": "FE",
    "amazon.in": "FE",
    "amazon.co.nz": "FE",
}

NOTIFICATION_KINDS: Final[dict[str, str]] = {
    "Alarm": "alarm",
    "Timer": "timer",
    "Reminder": "reminder",
    "MusicAlarm": "music_alarm",
}

LIST_KINDS: Final[dict[str, str]] = {
    "SHOP": "shopping",
    "TODO": "todo",
    "CUSTOM": "custom",
}

MEDIA_COMMANDS: Final[dict[str, str]] = {
    "play": "PlayCommand",
    "pause": "PauseCommand",
    "next": "NextCommand",
    "previous": "PreviousCommand",
    "forward": "ForwardCommand",
    "rewind": "RewindCommand",
    "shuffle": "ShuffleCommand",
    "repeat": "RepeatCommand",
}

# What the Alexa app declares to AVS (PUT /v1/devices/@self/capabilities).
# Without `Alexa.Mobile.Push` the HTTP/2 channel stays silent.
_LEGACY_FLAGS: Final[dict[str, Any]] = {
    "SUPPORTS_COMMS": True,
    "SUPPORTS_ARBITRATION": True,
    "SCREEN_WIDTH": 1170,
    "SUPPORTS_SCRUBBING": True,
    "SPEECH_SYNTH_SUPPORTS_TTS_URLS": False,
    "SUPPORTS_HOME_AUTOMATION": True,
    "SUPPORTS_DROPIN_OUTBOUND": True,
    "FRIENDLY_NAME_TEMPLATE": "VOX",
    "SUPPORTS_SIP_OUTBOUND_CALLING": True,
    "VOICE_PROFILE_SWITCHING_DISABLED": True,
    "SUPPORTS_LYRICS_IN_CARD": False,
    "SUPPORTS_DATAMART_NAMESPACE": "Vox",
    "SUPPORTS_VIDEO_CALLING": True,
    "SUPPORTS_PFM_CHANGED": True,
    "SUPPORTS_TARGET_PLATFORM": "TABLET",
    "SUPPORTS_SECURE_LOCKSCREEN": False,
    "AUDIO_PLAYER_SUPPORTS_TTS_URLS": False,
    "SUPPORTS_KEYS_IN_HEADER": False,
    "SUPPORTS_MIXING_BEHAVIOR_FOR_AUDIO_PLAYER": False,
    "AXON_SUPPORT": True,
    "SUPPORTS_TTS_SPEECHMARKS": True,
}

_INTERFACES: Final[list[dict[str, Any]]] = [
    {"interface": "CardRenderer", "version": "0.1"},
    {"interface": "Navigation", "version": "1.1"},
    {"interface": "Alexa.Comms.PhoneCallController", "version": "2.0"},
    {"interface": "ExternalMediaPlayer", "version": "1.1"},
    {
        "interface": "Alerts",
        "version": "1.3",
        "configurations": {"maximumAlerts": {"timers": 2, "overall": 99, "alarms": 2}},
    },
    {
        "interface": "Alexa.Display.Window",
        "version": "1.0",
        "configurations": {
            "templates": [
                {
                    "type": "STANDARD",
                    "id": "app_window_template",
                    "configuration": {
                        "sizes": [
                            {
                                "id": "fullscreen",
                                "type": "DISCRETE",
                                "value": {
                                    "value": {"height": 1440, "width": 3200},
                                    "unit": "PIXEL",
                                },
                            }
                        ],
                        "interactionModes": ["mobile_mode", "auto_mode"],
                    },
                }
            ]
        },
    },
    {"interface": "AccessoryKit", "version": "0.1"},
    {
        "interface": "Alexa.AudioSignal.ActiveNoiseControl",
        "version": "1.0",
        "configurations": {
            "ambientSoundProcessingModes": [
                {"name": "ACTIVE_NOISE_CONTROL"},
                {"name": "PASSTHROUGH"},
            ]
        },
    },
    {"interface": "PlaybackController", "version": "1.0"},
    {"interface": "Speaker", "version": "1.0"},
    {"interface": "SpeechSynthesizer", "version": "1.0"},
    {"interface": "AudioActivityTracker", "version": "1.0"},
    {"interface": "Alexa.Camera.LiveViewController", "version": "1.0"},
    {"interface": "Alexa.Input.Text", "version": "1.0"},
    {"interface": "Alexa.PlaybackStateReporter", "version": "1.0"},
    {"interface": "Geolocation", "version": "1.1"},
    {"interface": "Alexa.Health.Fitness", "version": "1.0"},
    {"interface": "Settings", "version": "1.0"},
    {
        "interface": "Alexa.InteractionMode",
        "version": "1.1",
        "configurations": {
            "interactionModes": [
                {
                    "dialog": "SUPPORTED",
                    "interactionDistance": {"value": 18, "unit": "INCHES"},
                    "video": "SUPPORTED",
                    "keyboard": "SUPPORTED",
                    "id": "mobile_mode",
                    "uiMode": "MOBILE",
                    "touch": "SUPPORTED",
                },
                {
                    "video": "UNSUPPORTED",
                    "dialog": "SUPPORTED",
                    "interactionDistance": {"value": 36, "unit": "INCHES"},
                    "uiMode": "AUTO",
                    "touch": "SUPPORTED",
                    "id": "auto_mode",
                    "keyboard": "UNSUPPORTED",
                },
            ]
        },
    },
    {
        "interface": "Alexa.Launcher",
        "version": "0.2",
        "configurations": {
            "catalogs": [
                {
                    "type": "IOS_APP_STORE",
                    "identifierTypes": ["URI_HTTP_SCHEME", "URI_CUSTOM_SCHEME"],
                }
            ]
        },
    },
    {"interface": "System", "version": "1.4"},
    {"interface": "Alexa.IOComponents", "version": "1.4"},
    {"interface": "Alexa.FavoritesController", "version": "1.0"},
    {
        "interface": "Alexa.Mobile.Push",
        "version": "1.1",
        "configurations": {
            "supportedEventFormats": [{"name": "smarthome-events", "version": "1.0.0"}]
        },
    },
    {"interface": "Alexa.Notifications.Multipart", "version": "1.0"},
    {"interface": "Alexa.Notifications.External", "version": "1.0"},
    {"interface": "InteractionModel", "version": "1.2"},
    {"interface": "Alexa.PlaylistController", "version": "1.0"},
    {"interface": "SpeechRecognizer", "version": "2.1"},
    {"interface": "AudioPlayer", "version": "1.3"},
    {"interface": "Alexa.RTCSessionController", "version": "3.1"},
    {"interface": "VisualActivityTracker", "version": "1.1"},
    {"interface": "Alexa.PlaybackController", "version": "1.0"},
    {"interface": "Alexa.SeekController", "version": "1.0"},
    {"interface": "Alexa.Comms.MessagingController", "version": "1.0"},
]

DEVICE_CAPABILITIES: Final[dict[str, Any]] = {
    "legacyFlags": _LEGACY_FLAGS,
    "envelopeVersion": "20160207",
    "capabilities": [{"type": "AlexaInterface", **item} for item in _INTERFACES],
}

# `temp:` / `vol:` aliases avoid a field-type conflict between union members.
# `deviceAccountId` and `firstPartyMetadata` are left out on purpose: they make
# the server throw for speaker-group endpoints.
QUERY_ENDPOINTS: Final = """
query echoEndpoints {
  listEndpoints(listEndpointsInput: {includeHouseholdDevices: true}) {
    endpoints {
      endpointId: id
      friendlyNameObject { value { text } }
      model { value { text } }
      displayCategories { primary { value } }
      dmsIdentifier { deviceType dsn }
      connections { type macAddress }
      settings { doNotDisturb { toggleValue } }
      features {
        name
        instance
        properties {
          name
          type
          __typename
          ... on Battery { batteryValue { levelPercentage } }
          ... on InventoryLevel { inventoryLevelValue { type value unit } }
          ... on TemperatureSensor { temp: value { value scale } timeOfSample }
          ... on ToggleState { toggleStateValue }
          ... on Volume { vol: value { value } }
          ... on Reachability { reachabilityStatusValue }
        }
      }
    }
  }
}
"""

MUTATION_SET_ENDPOINT_FEATURES: Final = """
mutation setEndpointFeatures($input: SetEndpointFeaturesInput!) {
  setEndpointFeatures(setEndpointFeaturesInput: $input) {
    featureControlResponses {
      endpointId featureName instance featureOperationName code
    }
    errors { endpointId featureName instance featureOperationName code message }
  }
}
"""

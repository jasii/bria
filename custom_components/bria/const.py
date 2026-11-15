"""Constants for the Bria VoIP integration."""

DOMAIN = "bria"

DEFAULT_HOST = "cpclientapi.softphone.com"
DEFAULT_PORT = 9002

CONF_HOST = "host"
CONF_PORT = "port"
CONF_VERIFY_SSL = "verify_ssl"

DEFAULT_VERIFY_SSL = True

WS_PATH = "/counterpath/socketapi/v1/"

# Bria API status types
STATUS_AUTHENTICATION = "authentication"
STATUS_CALL = "call"
STATUS_CALLHISTORY = "callHistory"
STATUS_SCREENSHARE = "screenShare"
STATUS_PRESENCE = "presence"
STATUS_PHONE = "phone"
STATUS_SYSTEM_INFO = "systemInformation"
STATUS_SYSTEM_SETTINGS = "systemSettings"
STATUS_AUDIO_DEVICES = "audioDevices"
STATUS_AUDIO_PROPERTIES = "audioProperties"
STATUS_CALL_OPTIONS = "callOptions"
STATUS_MISSED_CALL = "missedCall"
STATUS_VOICEMAIL = "voiceMail"
STATUS_ACCOUNT = "account"
STATUS_SUPPORTED_PRESENCE = "supportedPresence"

# Sensor group configuration keys (stored in config entry options)
GROUP_PRESENCE = "group_presence"
GROUP_CALL_DETAILS = "group_call_details"
GROUP_AUDIO = "group_audio"
GROUP_SCREEN_SHARE = "group_screen_share"
GROUP_PHONE_INFO = "group_phone_info"
GROUP_CALL_OPTIONS = "group_call_options"
GROUP_MISSED_VOICEMAIL = "group_missed_voicemail"

SENSOR_GROUPS = (
    GROUP_PRESENCE,
    GROUP_CALL_DETAILS,
    GROUP_AUDIO,
    GROUP_SCREEN_SHARE,
    GROUP_PHONE_INFO,
    GROUP_CALL_OPTIONS,
    GROUP_MISSED_VOICEMAIL,
)

# Presence options exposed in the select entity
PRESENCE_OPTIONS = ["available", "away", "busy", "doNotDisturb", "offline"]

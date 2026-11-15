"""Common v1 limits, independent of models and runtime initialization."""

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65536
# The P02 compatibility envelope and response metadata must fit too.
MAX_PAYLOAD_BYTES = MAX_MESSAGE_BYTES - 1024
MAX_SETTINGS_SNAPSHOT_BYTES = MAX_MESSAGE_BYTES - 2048
MAX_EVENT_HISTORY = 512
MAX_COUNTER = 2**53 - 1

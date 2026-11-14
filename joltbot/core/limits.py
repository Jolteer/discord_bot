"""Discord's own size limits, named once.

Discord refuses a whole message, embed or form if any part of it is too long,
so text is cut to these sizes before it is sent, and the per-server settings
use the same numbers as their maximum lengths. See
https://discord.com/developers/docs/resources/message and
https://discord.com/developers/docs/components/reference
"""

from __future__ import annotations

# Messages
MESSAGE_MAX = 2000  # characters in a message's text
MESSAGE_MAX_FILES = 10  # attachments on one message

# Embeds. All of an embed's text together may not pass 6000 characters.
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024

# Buttons and dropdowns
BUTTON_LABEL_MAX = 80
SELECT_MAX_OPTIONS = 25
SELECT_OPTION_LABEL_MAX = 100
SELECT_PLACEHOLDER_MAX = 150

# Forms ("modals")
MODAL_TITLE_MAX = 45
MODAL_MAX_INPUTS = 5
TEXT_INPUT_LABEL_MAX = 45
TEXT_INPUT_PLACEHOLDER_MAX = 100

# Servers
CHANNEL_NAME_MAX = 100
CATEGORY_MAX_CHANNELS = 50
AUDIT_REASON_MAX = 512  # the "reason" shown in the server's audit log

"""
LINE media type safety helpers.

Provides the file-extension whitelist that channel_gw enforces in
``_prepare_line_message_for_ai`` (main.py:1300) and the hermes-agent
native LINE adapter does not yet replicate.  Keeping this in a separate
module so the adapter only needs a one-line import + check, matching the
"adapter.py adds minimal wiring" guidance.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

# Whitelist mirroring channel_gw's SUPPORTED_EXTS (main.py:1300).
# Only these document extensions are accepted for inbound LINE file
# messages.  Executable/script/archive extensions (.exe, .sh, .zip, …)
# are rejected before any bytes are downloaded, preventing malicious
# payloads from reaching the cache or the agent's tool chain.
SUPPORTED_FILE_EXTENSIONS: frozenset = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv",
})

# Human-readable list used in the "unsupported format" reply.
_SUPPORTED_FORMATS_DISPLAY = "PDF、Word、Excel、PPT、TXT、CSV"


def get_file_extension(filename: Optional[str]) -> str:
    """Return the lowercased extension (without dot) from a filename.

    Returns an empty string when the filename has no extension or is None.
    """
    if not filename:
        return ""
    _, ext = os.path.splitext(filename)
    return ext.lstrip(".").lower()


def is_supported_file_type(filename: Optional[str]) -> bool:
    """Return True when *filename* has a whitelisted document extension."""
    ext = get_file_extension(filename)
    return ext in SUPPORTED_FILE_EXTENSIONS


def unsupported_file_message(filename: Optional[str]) -> str:
    """Build the user-facing reply for an unsupported file extension."""
    ext = get_file_extension(filename)
    if ext:
        return f"不支援 .{ext} 格式，目前支援：{_SUPPORTED_FORMATS_DISPLAY}。"
    return f"不支援該檔案格式，目前支援：{_SUPPORTED_FORMATS_DISPLAY}。"


def check_file_extension(filename: Optional[str]) -> Tuple[bool, str]:
    """Check a LINE file message's extension against the whitelist.

    Returns ``(is_supported, message)`` where *message* is the
    user-facing reply text when the extension is not supported (empty
    string when supported).
    """
    if is_supported_file_type(filename):
        return True, ""
    return False, unsupported_file_message(filename)

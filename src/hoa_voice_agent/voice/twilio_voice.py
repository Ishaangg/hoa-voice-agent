"""Twilio Voice webhook helpers for the HOA phone agent."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from html import escape


def is_valid_twilio_request(
    *,
    auth_token: str,
    url: str,
    form_values: Mapping[str, str],
    signature: str | None,
) -> bool:
    """Validate Twilio's signed HTTP webhook request without logging its secret."""

    if not signature:
        return False
    payload = url + "".join(f"{key}{form_values[key]}" for key in sorted(form_values))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def is_valid_twilio_websocket_request(
    *, auth_token: str, stream_url: str, signature: str | None
) -> bool:
    """Validate Twilio's signed Media Stream WebSocket handshake.

    Twilio documents a trailing slash as a compatibility fallback for Voice WSS
    signature validation, so accept either canonical form without weakening the
    signature requirement.
    """

    candidates = (stream_url.rstrip("/"), f"{stream_url.rstrip('/')}/")
    return any(
        is_valid_twilio_request(
            auth_token=auth_token,
            url=candidate,
            form_values={},
            signature=signature,
        )
        for candidate in candidates
    )


def twiml_for_media_stream(*, stream_url: str, from_number: str, to_number: str) -> str:
    """Create TwiML for one authenticated, bidirectional voice stream."""

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f'    <Stream url="{escape(stream_url, quote=True)}">\n'
        f'      <Parameter name="from_number" value="{escape(from_number, quote=True)}"/>\n'
        f'      <Parameter name="to_number" value="{escape(to_number, quote=True)}"/>\n'
        "    </Stream>\n"
        "  </Connect>\n"
        "</Response>"
    )

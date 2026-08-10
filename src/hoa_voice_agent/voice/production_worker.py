"""Production media-worker entry point for the Pipecat voice service.

Pipecat's local runner is intentionally kept as the HTTP/WebSocket transport
adapter, but it is launched in its own deployable process. The API and outbox
worker never share this process. In a managed deployment, run several copies
of this service behind the telephony/media ingress or use Pipecat Cloud's
dispatcher; each copy owns only live media sessions.
"""

from __future__ import annotations

from contextlib import suppress

from hoa_voice_agent.voice.bot import bot  # noqa: F401  # Pipecat discovery hook
from hoa_voice_agent.voice.bot import main as _runner_main


def main() -> None:
    # Reuse the signed-webhook/config validation in the existing bot entrypoint.
    # Importing ``bot`` into this __main__ module also lets Pipecat discover it.
    _runner_main()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()

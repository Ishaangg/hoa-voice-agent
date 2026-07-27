from typing import Protocol


class ConversationResponder(Protocol):
    """Generates a grounded, resident-facing response for a non-emergency turn."""

    async def reply(
        self,
        *,
        text: str,
        community_name: str,
        faqs: dict[str, str],
    ) -> str | None: ...

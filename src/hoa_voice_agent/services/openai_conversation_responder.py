from collections.abc import Callable

import httpx
from loguru import logger

from hoa_voice_agent.domain.conversation_responder import ConversationResponder

_SYSTEM_PROMPT = """You are the friendly voice concierge for an HOA community.
Reply in one or two short, natural sentences suitable for text-to-speech.

You may greet the resident and help with general conversation. For HOA facts, use
only the supplied community information. Do not invent policies, hours, fees,
phone numbers, or work-order status. If the information is unavailable, say you
can help with a community question or a maintenance report. Never give emergency
instructions or promise dispatch; those are handled by a separate safety system.
"""


class OpenAIConversationResponder(ConversationResponder):
    """OpenAI-compatible responder used with Groq-hosted Qwen."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=5.0))

    async def reply(
        self,
        *,
        text: str,
        community_name: str,
        faqs: dict[str, str],
    ) -> str | None:
        community_context = "\n".join(
            f"- {question}: {answer}" for question, answer in faqs.items()
        )
        prompt = (
            f"Community: {community_name}\n"
            f"Approved community information:\n{community_context or '- None'}\n\n"
            f"Resident said: {text}"
        )
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.2,
                        "top_k": 1,
                        "max_tokens": 80,
                        "stream": False,
                        # Qwen supports explicit non-thinking mode. Hiding reasoning
                        # keeps internal tokens out of the resident's voice response.
                        "reasoning_effort": "none",
                        "reasoning_format": "hidden",
                    },
                )
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Qwen returned an empty conversational response")
            return content.strip()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Qwen conversational response failed; using local fallback: {}", exc)
            return None

import json
from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger

from hoa_voice_agent.domain.incident_extractor import IncidentExtractor, RuleBasedIncidentExtractor
from hoa_voice_agent.domain.models import IncidentReport

_EXTRACTION_PROMPT = """You extract facts from HOA maintenance calls.
Return one JSON object only. Do not include markdown, explanations, or a safety decision.

Allowed incident_type values: fire_or_smoke, gas_smell, active_flooding,
elevator_entrapment, access_failure, hvac, cosmetic, unknown.

Return exactly these keys:
incident_type, location, water_active, source_known, resident_safe.

Use null for facts not stated. Do not infer that a resident is safe. """


class NvidiaIncidentExtractor(IncidentExtractor):
    """Use Nemotron Nano for facts, then defer all safety decisions to Python policy."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        fallback: IncidentExtractor | None = None,
        prefer_local_for_known_incidents: bool = False,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=8.0))
        self._fallback = fallback or RuleBasedIncidentExtractor()
        self._prefer_local_for_known_incidents = prefer_local_for_known_incidents

    async def extract(self, description: str) -> IncidentReport:
        if self._prefer_local_for_known_incidents:
            local_report = await self._fallback.extract(description)
            if local_report.incident_type.value != "unknown":
                return local_report
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _EXTRACTION_PROMPT},
                            {"role": "user", "content": description},
                        ],
                        "temperature": 0,
                        "top_k": 1,
                        "max_tokens": 180,
                        "stream": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                response.raise_for_status()
            return self._to_incident_report(response.json(), description)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Losing extraction must not leave a resident in silence during an active call.
            logger.warning("Nemotron incident extraction failed; using local fallback: {}", exc)
            return await self._fallback.extract(description)

    @staticmethod
    def _to_incident_report(payload: dict[str, Any], description: str) -> IncidentReport:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Nemotron returned a non-text completion")
        parsed = json.loads(NvidiaIncidentExtractor._json_object(content))
        if not isinstance(parsed, dict):
            raise ValueError("Nemotron did not return a JSON object")
        allowed = {
            "incident_type",
            "location",
            "water_active",
            "source_known",
            "resident_safe",
        }
        facts = {key: value for key, value in parsed.items() if key in allowed}
        return IncidentReport(description=description, **facts)

    @staticmethod
    def _json_object(content: str) -> str:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Nemotron response did not contain JSON")
        return cleaned[start : end + 1]

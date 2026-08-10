import json
from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger

from hoa_voice_agent.domain.incident_extractor import IncidentExtractor, RuleBasedIncidentExtractor
from hoa_voice_agent.domain.models import IncidentReport

_EXTRACTION_PROMPT = """You are the structured router for an HOA resident voice agent.
Determine whether the caller is reporting a maintenance or operational issue, including
Hindi-English code switching. Do not decide emergency priority, pricing, vendor, or dispatch.
Return one JSON object only. Do not include markdown or explanations.

Allowed incident_type values: fire_or_smoke, gas_smell, active_flooding, plumbing,
elevator_entrapment, access_failure, hvac, cosmetic, unknown.

Allowed turn_intent values: maintenance_report, clarification, case_status, faq, general.

Return exactly these keys:
turn_intent, is_maintenance, incident_type, location, water_active, safety_hazard,
source_known, resident_safe.

Set is_maintenance to true for any repair, damage, service, access, or safety issue,
even if incident_type is unknown. Set it to false for questions, greetings, and ordinary chat.
Use clarification when the caller asks what they previously said or reported. Use case_status
when they ask for an update about an existing request. A clarification or case_status turn
must never be treated as a new maintenance report.
For plumbing reports, set safety_hazard to true only when the caller explicitly describes
rapidly spreading water, a burst or overflowing pipe, standing water near electricity, or
another immediate danger. Set it to false only when they explicitly say it is localized and
safe. Otherwise set it to null so the agent can ask one safety question.
Use null for facts not stated. Do not infer that a resident is safe. """


class OpenAIIncidentExtractor(IncidentExtractor):
    """Use an OpenAI-compatible model (Groq/Qwen) for structured turn classification."""

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
                        "response_format": {"type": "json_object"},
                        "reasoning_effort": "none",
                        "reasoning_format": "hidden",
                    },
                )
                response.raise_for_status()
            return self._to_incident_report(response.json(), description)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Losing extraction must not leave a resident in silence during an active call.
            logger.warning("Qwen incident extraction failed; using local fallback: {}", exc)
            return await self._fallback.extract(description)

    @staticmethod
    def _to_incident_report(payload: dict[str, Any], description: str) -> IncidentReport:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Qwen returned a non-text completion")
        parsed = json.loads(OpenAIIncidentExtractor._json_object(content))
        if not isinstance(parsed, dict):
            raise ValueError("Qwen did not return a JSON object")
        allowed = {
            "turn_intent",
            "is_maintenance",
            "incident_type",
            "location",
            "water_active",
            "safety_hazard",
            "source_known",
            "resident_safe",
        }
        facts = {key: value for key, value in parsed.items() if key in allowed}
        if "turn_intent" not in facts or "is_maintenance" not in facts:
            raise ValueError("Qwen router did not classify the caller intent")
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
            raise ValueError("Qwen response did not contain JSON")
        return cleaned[start : end + 1]

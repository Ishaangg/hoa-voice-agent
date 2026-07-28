import re
from typing import Protocol

from hoa_voice_agent.domain.models import IncidentReport, IncidentType


class IncidentExtractor(Protocol):
    async def extract(self, description: str) -> IncidentReport: ...


class RuleBasedIncidentExtractor:
    """Temporary Phase 1 extractor; replace with schema-constrained LLM output."""

    _rules: tuple[tuple[IncidentType, tuple[str, ...]], ...] = (
        (IncidentType.FIRE_OR_SMOKE, ("fire", "smoke", "burning", "आग", "धुआं", "धुआँ")),
        (IncidentType.GAS_SMELL, ("gas smell", "smell gas", "natural gas", "गैस")),
        (
            IncidentType.ELEVATOR_ENTRAPMENT,
            (
                "stuck in the elevator",
                "trapped in the elevator",
                "elevator trapped",
                "stuck in the lift",
                "trapped in the lift",
                "lift trapped",
                "लिफ्ट में फंसा",
                "लिफ्ट में फँसा",
            ),
        ),
        (
            IncidentType.ACTIVE_FLOODING,
            (
                "flood",
                "leak",
                "water coming",
                "burst pipe",
                "overflow",
                "लीक",
                "पानी",
                "बाढ़",
                "बाढ़",
            ),
        ),
        (
            IncidentType.ACCESS_FAILURE,
            ("locked out", "gate won't open", "gate does not open", "key fob"),
        ),
        (
            IncidentType.HVAC,
            ("no heat", "no air", "air conditioning", "heater", "hvac", "ac is"),
        ),
        (
            IncidentType.COSMETIC,
            ("paint", "scratch", "stain", "cosmetic", "crack in"),
        ),
    )

    async def extract(self, description: str) -> IncidentReport:
        normalized = " ".join(description.lower().split())
        incident_type = IncidentType.UNKNOWN
        for candidate, phrases in self._rules:
            if any(phrase in normalized for phrase in phrases):
                incident_type = candidate
                break

        location_match = re.search(
            r"\b(?:at|in|near)\s+((?:building|unit|floor|hallway|garage|lobby)[^,.!?]*)",
            description,
            flags=re.IGNORECASE,
        )
        water_active = None
        if incident_type == IncidentType.ACTIVE_FLOODING:
            water_active = not any(
                phrase in normalized
                for phrase in ("stopped", "not active", "was leaking", "no longer leaking")
            )

        return IncidentReport(
            incident_type=incident_type,
            description=description,
            location=location_match.group(1).strip() if location_match else None,
            water_active=water_active,
        )

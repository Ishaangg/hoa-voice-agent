from hoa_voice_agent.domain.incident_extractor import RuleBasedIncidentExtractor
from hoa_voice_agent.domain.models import IncidentType


async def test_extracts_active_leak_and_location() -> None:
    report = await RuleBasedIncidentExtractor().extract(
        "Water is leaking in Building A second-floor hallway."
    )

    assert report.incident_type == IncidentType.ACTIVE_FLOODING
    assert report.water_active is True
    assert report.location == "Building A second-floor hallway"


async def test_marks_a_stopped_leak_inactive() -> None:
    report = await RuleBasedIncidentExtractor().extract("The sink was leaking, but it stopped.")

    assert report.incident_type == IncidentType.ACTIVE_FLOODING
    assert report.water_active is False


async def test_fire_rule_wins_over_generic_damage() -> None:
    report = await RuleBasedIncidentExtractor().extract(
        "There is smoke and fire damage in the lobby."
    )

    assert report.incident_type == IncidentType.FIRE_OR_SMOKE


async def test_extracts_lift_entrapment() -> None:
    report = await RuleBasedIncidentExtractor().extract("I am trapped in the lift.")

    assert report.incident_type == IncidentType.ELEVATOR_ENTRAPMENT

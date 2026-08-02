import pytest

from hoa_voice_agent.domain.emergency_policy import EmergencyPolicyEngine
from hoa_voice_agent.domain.models import IncidentReport, IncidentType, Priority


@pytest.mark.parametrize(
    ("incident_type", "priority", "transfer"),
    [
        (IncidentType.FIRE_OR_SMOKE, Priority.EMERGENCY, True),
        (IncidentType.GAS_SMELL, Priority.EMERGENCY, True),
        (IncidentType.ELEVATOR_ENTRAPMENT, Priority.EMERGENCY, True),
        (IncidentType.ACCESS_FAILURE, Priority.URGENT, True),
        (IncidentType.HVAC, Priority.URGENT, False),
        (IncidentType.COSMETIC, Priority.ROUTINE, False),
        (IncidentType.UNKNOWN, Priority.ROUTINE, False),
    ],
)
def test_policy_decisions_are_deterministic(
    incident_type: IncidentType,
    priority: Priority,
    transfer: bool,
) -> None:
    report = IncidentReport(incident_type=incident_type, description="Test incident")

    decision = EmergencyPolicyEngine().decide(report)

    assert decision.priority == priority
    assert decision.transfer_to_on_call is transfer
    assert decision.create_work_order is True


def test_active_flooding_is_emergency_while_water_is_running() -> None:
    report = IncidentReport(
        incident_type=IncidentType.ACTIVE_FLOODING,
        description="Water is leaking from the ceiling",
        water_active=True,
    )

    decision = EmergencyPolicyEngine().decide(report)

    assert decision.priority == Priority.EMERGENCY
    assert decision.dispatch_vendor is True
    assert decision.transfer_to_on_call is True


def test_stopped_leak_is_urgent_but_does_not_force_transfer() -> None:
    report = IncidentReport(
        incident_type=IncidentType.ACTIVE_FLOODING,
        description="The pipe was leaking but it stopped",
        water_active=False,
    )

    decision = EmergencyPolicyEngine().decide(report)

    assert decision.priority == Priority.URGENT
    assert decision.transfer_to_on_call is False

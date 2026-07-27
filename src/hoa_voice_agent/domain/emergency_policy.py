from hoa_voice_agent.domain.models import (
    EmergencyDecision,
    IncidentReport,
    IncidentType,
    Priority,
)


class EmergencyPolicyEngine:
    """Deterministic safety policy. Model output may supply facts, never the decision."""

    def decide(self, report: IncidentReport) -> EmergencyDecision:
        match report.incident_type:
            case IncidentType.FIRE_OR_SMOKE:
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.EMERGENCY,
                    instructions=[
                        "Leave the building if it is safe to do so.",
                        "Call 911 from a safe location.",
                    ],
                    dispatch_vendor=False,
                    transfer_to_on_call=True,
                    create_work_order=True,
                )
            case IncidentType.GAS_SMELL:
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.EMERGENCY,
                    instructions=[
                        "Leave the area without operating switches or appliances.",
                        "Call 911 or the gas utility from a safe location.",
                    ],
                    dispatch_vendor=False,
                    transfer_to_on_call=True,
                    create_work_order=True,
                )
            case IncidentType.ELEVATOR_ENTRAPMENT:
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.EMERGENCY,
                    instructions=[
                        "Stay in the elevator and use its emergency call button.",
                        "Call 911 if anyone needs immediate medical help.",
                    ],
                    dispatch_vendor=True,
                    transfer_to_on_call=True,
                    create_work_order=True,
                )
            case IncidentType.ACTIVE_FLOODING:
                active = report.water_active is not False
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.EMERGENCY if active else Priority.URGENT,
                    instructions=[
                        "Avoid standing water near electrical outlets or equipment.",
                        "If it is safe, move belongings away from the water.",
                    ],
                    dispatch_vendor=True,
                    transfer_to_on_call=active,
                    create_work_order=True,
                )
            case IncidentType.ACCESS_FAILURE:
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.URGENT,
                    instructions=["Stay in a safe, well-lit location while help is arranged."],
                    dispatch_vendor=True,
                    transfer_to_on_call=True,
                    create_work_order=True,
                )
            case IncidentType.HVAC:
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.URGENT,
                    instructions=[],
                    dispatch_vendor=True,
                    transfer_to_on_call=False,
                    create_work_order=True,
                )
            case IncidentType.COSMETIC | IncidentType.UNKNOWN:
                return EmergencyDecision(
                    incident_type=report.incident_type,
                    priority=Priority.ROUTINE,
                    instructions=[],
                    dispatch_vendor=False,
                    transfer_to_on_call=False,
                    create_work_order=True,
                )


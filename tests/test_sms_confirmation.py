from uuid import UUID

from hoa_voice_agent.domain.models import IncidentType, Priority, Resident, WorkOrder
from hoa_voice_agent.services.conversation import ConversationService


def test_confirmation_sms_includes_resident_and_incident_metadata() -> None:
    work_order = WorkOrder(
        id=UUID("11111111-2222-3333-4444-555555555555"),
        conversation_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        management_company_id=UUID("10000000-0000-0000-0000-000000000001"),
        community_id=UUID("20000000-0000-0000-0000-000000000001"),
        resident_id=UUID("30000000-0000-0000-0000-000000000001"),
        incident_type=IncidentType.ACTIVE_FLOODING,
        priority=Priority.EMERGENCY,
        description="Water is leaking in the lobby.",
    )
    resident = Resident(
        id=UUID("30000000-0000-0000-0000-000000000001"),
        management_company_id=work_order.management_company_id,
        community_id=work_order.community_id,
        first_name="Alex",
        phone="+15550101111",
        unit="Building A, Unit 204",
    )

    sms = ConversationService._build_confirmation_sms(
        community_name="Pine Grove Community",
        resident=resident,
        work_order=work_order,
        transfer_to_on_call=True,
    )

    assert "Hi Alex" in sms
    assert "Building A, Unit 204" in sms
    assert "emergency active flooding report" in sms
    assert "Water is leaking in the lobby." in sms
    assert "11111111" in sms
    assert "on-call manager" in sms

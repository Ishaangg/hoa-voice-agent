from uuid import UUID

from hoa_voice_agent.adapters.protocols import (
    CallControlAdapter,
    CommunityManagementAdapter,
    MessagingAdapter,
)
from hoa_voice_agent.domain.models import Community, Resident, WorkOrder

MANAGEMENT_COMPANY_ID = UUID("10000000-0000-0000-0000-000000000001")
COMMUNITY_ID = UUID("20000000-0000-0000-0000-000000000001")
RESIDENT_ID = UUID("30000000-0000-0000-0000-000000000001")


class FakeCommunityManagementAdapter(CommunityManagementAdapter):
    def __init__(self) -> None:
        self.community = Community(
            management_company_id=MANAGEMENT_COMPANY_ID,
            community_id=COMMUNITY_ID,
            name="Pine Grove Community",
            office_phone="+15550102000",
            on_call_phone="+15550102999",
            faqs={
                "office hours": (
                    "The management office is open Monday through Friday, 9 AM to 5 PM."
                ),
                "pool hours": "The pool is open daily from 8 AM to 9 PM.",
                "trash pickup": "Trash is collected Tuesday and Friday mornings.",
                "parking": "Guest vehicles must use marked guest spaces and display a guest pass.",
            },
        )
        self.resident = Resident(
            management_company_id=MANAGEMENT_COMPANY_ID,
            community_id=COMMUNITY_ID,
            id=RESIDENT_ID,
            first_name="Alex",
            phone="+15550101111",
            unit="Building A, Unit 204",
        )
        self.work_orders: list[WorkOrder] = []

    async def get_community(self, community_id: UUID) -> Community | None:
        return self.community if community_id == self.community.community_id else None

    async def find_resident(self, phone: str) -> Resident | None:
        return self.resident if phone == self.resident.phone else None

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        self.work_orders.append(work_order)
        return work_order


class FakeMessagingAdapter(MessagingAdapter):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_sms(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        self.messages.append((phone, message))
        return f"sms-simulated-{len(self.messages)}"


class FakeCallControlAdapter(CallControlAdapter):
    def __init__(self) -> None:
        self.transfers: list[tuple[UUID, str]] = []

    async def transfer_to_on_call(self, conversation_id: UUID, phone: str) -> str:
        self.transfers.append((conversation_id, phone))
        return f"transfer-simulated-{len(self.transfers)}"

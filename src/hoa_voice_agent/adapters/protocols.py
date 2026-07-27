from typing import Protocol
from uuid import UUID

from hoa_voice_agent.domain.models import Community, Resident, WorkOrder


class CommunityManagementAdapter(Protocol):
    async def get_community(self, community_id: UUID) -> Community | None: ...

    async def find_resident(self, phone: str) -> Resident | None: ...

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder: ...


class MessagingAdapter(Protocol):
    async def send_sms(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> str: ...


class CallControlAdapter(Protocol):
    async def transfer_to_on_call(self, conversation_id: UUID, phone: str) -> str: ...

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.domain.models import (
    ConversationState,
    IncidentType,
    Priority,
    WorkOrder,
)
from hoa_voice_agent.persistence.tables import ConversationRecord, WorkOrderRecord


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        management_company_id: UUID,
        community_id: UUID,
        caller_phone: str,
        resident_id: UUID | None,
        state: ConversationState = ConversationState.READY,
    ) -> ConversationRecord:
        record = ConversationRecord(
            management_company_id=management_company_id,
            community_id=community_id,
            resident_id=resident_id,
            caller_phone=caller_phone,
            state=state.value,
        )
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def get(self, conversation_id: UUID) -> ConversationRecord | None:
        return await self.session.get(ConversationRecord, conversation_id)

    async def set_caller_details(
        self,
        conversation: ConversationRecord,
        *,
        name: str | None = None,
        unit: str | None = None,
    ) -> ConversationRecord:
        if name:
            conversation.caller_name = name
        if unit:
            conversation.caller_unit = unit
        if conversation.caller_name and conversation.caller_unit:
            conversation.state = ConversationState.READY.value
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation


class WorkOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, work_order: WorkOrder) -> WorkOrder:
        record = WorkOrderRecord(
            id=work_order.id,
            conversation_id=work_order.conversation_id,
            management_company_id=work_order.management_company_id,
            community_id=work_order.community_id,
            resident_id=work_order.resident_id,
            incident_type=work_order.incident_type.value,
            priority=work_order.priority.value,
            description=work_order.description,
            status=work_order.status,
            created_at=work_order.created_at,
        )
        self.session.add(record)
        await self.session.commit()
        return work_order

    async def list_for_conversation(self, conversation_id: UUID) -> list[WorkOrder]:
        result = await self.session.scalars(
            select(WorkOrderRecord)
            .where(WorkOrderRecord.conversation_id == conversation_id)
            .order_by(WorkOrderRecord.created_at)
        )
        return [
            WorkOrder(
                id=row.id,
                conversation_id=row.conversation_id,
                management_company_id=row.management_company_id,
                community_id=row.community_id,
                resident_id=row.resident_id,
                incident_type=IncidentType(row.incident_type),
                priority=Priority(row.priority),
                description=row.description,
                status=row.status,
                created_at=row.created_at,
            )
            for row in result
        ]

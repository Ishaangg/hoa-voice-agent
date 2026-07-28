from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.domain.models import (
    ConversationState,
    IncidentType,
    Priority,
    WorkOrder,
)
from hoa_voice_agent.persistence.tables import (
    CallerProfileHistoryRecord,
    CallerProfileRecord,
    CommunityKnowledgeRecord,
    ConversationRecord,
    ConversationTurnRecord,
    WorkflowEventRecord,
    WorkOrderRecord,
)


class ConversationTurnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, conversation_id: UUID, speaker: str, text: str) -> ConversationTurnRecord:
        record = ConversationTurnRecord(
            conversation_id=conversation_id,
            speaker=speaker,
            text=text,
        )
        self.session.add(record)
        await self.session.commit()
        return record

    async def list_for_conversation(self, conversation_id: UUID) -> list[ConversationTurnRecord]:
        result = await self.session.scalars(
            select(ConversationTurnRecord)
            .where(ConversationTurnRecord.conversation_id == conversation_id)
            .order_by(ConversationTurnRecord.created_at)
        )
        return list(result)


class WorkflowEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        conversation_id: UUID,
        event_type: str,
        detail: str,
        reference_id: UUID | None = None,
    ) -> WorkflowEventRecord:
        record = WorkflowEventRecord(
            conversation_id=conversation_id,
            event_type=event_type,
            detail=detail,
            reference_id=reference_id,
        )
        self.session.add(record)
        await self.session.commit()
        return record

    async def list_recent(self, limit: int = 100) -> list[WorkflowEventRecord]:
        result = await self.session.scalars(
            select(WorkflowEventRecord)
            .order_by(WorkflowEventRecord.created_at.desc())
            .limit(limit)
        )
        return list(result)


class CommunityKnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_facts(self, community_id: UUID) -> dict[str, str]:
        records = await self.session.scalars(
            select(CommunityKnowledgeRecord)
            .where(
                CommunityKnowledgeRecord.community_id == community_id,
                CommunityKnowledgeRecord.is_active.is_(True),
            )
            .order_by(CommunityKnowledgeRecord.topic)
        )
        return {record.topic: record.answer for record in records}

    async def seed_if_empty(
        self,
        *,
        management_company_id: UUID,
        community_id: UUID,
        facts: tuple[tuple[str, str, str], ...],
    ) -> None:
        existing = await self.session.scalar(
            select(CommunityKnowledgeRecord.id).where(
                CommunityKnowledgeRecord.community_id == community_id
            )
        )
        if existing:
            return
        self.session.add_all(
            [
                CommunityKnowledgeRecord(
                    management_company_id=management_company_id,
                    community_id=community_id,
                    category=category,
                    topic=topic,
                    answer=answer,
                )
                for category, topic, answer in facts
            ]
        )
        await self.session.commit()


class CallerProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self,
        *,
        management_company_id: UUID,
        community_id: UUID,
        caller_phone: str,
    ) -> CallerProfileRecord:
        profile = await self.session.scalar(
            select(CallerProfileRecord).where(
                CallerProfileRecord.community_id == community_id,
                CallerProfileRecord.caller_phone == caller_phone,
            )
        )
        if profile:
            return profile
        profile = CallerProfileRecord(
            management_company_id=management_company_id,
            community_id=community_id,
            caller_phone=caller_phone,
        )
        self.session.add(profile)
        await self.session.commit()
        await self.session.refresh(profile)
        return profile

    async def set_claimed_details(
        self,
        profile: CallerProfileRecord,
        *,
        name: str | None,
        unit: str | None,
    ) -> CallerProfileRecord:
        changes: list[tuple[str, str | None, str | None]] = []
        if name:
            if profile.claimed_name != name:
                changes.append(("name", profile.claimed_name, name))
            profile.claimed_name = name
        if unit:
            if profile.claimed_unit != unit:
                changes.append(("unit", profile.claimed_unit, unit))
            profile.claimed_unit = unit
        self.session.add_all(
            [
                CallerProfileHistoryRecord(
                    caller_profile_id=profile.id,
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    change_source="conversation",
                )
                for field_name, old_value, new_value in changes
            ]
        )
        await self.session.commit()
        await self.session.refresh(profile)
        return profile


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
        caller_name: str | None = None,
        caller_unit: str | None = None,
        state: ConversationState = ConversationState.READY,
    ) -> ConversationRecord:
        record = ConversationRecord(
            management_company_id=management_company_id,
            community_id=community_id,
            resident_id=resident_id,
            caller_phone=caller_phone,
            caller_name=caller_name,
            caller_unit=caller_unit,
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

    async def set_pending_caller_update(
        self,
        conversation: ConversationRecord,
        *,
        name: str | None = None,
        unit: str | None = None,
    ) -> ConversationRecord:
        conversation.pending_caller_name = name
        conversation.pending_caller_unit = unit
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation

    async def clear_pending_caller_update(
        self,
        conversation: ConversationRecord,
    ) -> ConversationRecord:
        conversation.pending_caller_name = None
        conversation.pending_caller_unit = None
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

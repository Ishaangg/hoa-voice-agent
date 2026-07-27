from uuid import UUID

from pydantic import BaseModel, Field

from hoa_voice_agent.adapters.fake import COMMUNITY_ID
from hoa_voice_agent.domain.models import (
    ConversationState,
    EmergencyDecision,
    Intent,
    WorkflowAction,
    WorkOrder,
)


class StartConversationRequest(BaseModel):
    caller_phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    community_id: UUID = COMMUNITY_ID


class StartConversationResponse(BaseModel):
    conversation_id: UUID
    community_id: UUID
    resident_id: UUID | None
    state: ConversationState
    say: str


class ConversationTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)


class ConversationTurnResponse(BaseModel):
    conversation_id: UUID
    intent: Intent
    say: str
    actions: list[WorkflowAction] = Field(default_factory=list)
    decision: EmergencyDecision | None = None
    work_order: WorkOrder | None = None


class HealthResponse(BaseModel):
    status: str
    service: str

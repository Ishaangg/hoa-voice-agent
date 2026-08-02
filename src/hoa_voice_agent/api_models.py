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


class StaffLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)


class CaseAssignRequest(BaseModel):
    assignee_type: str = Field(pattern="^(on_call|vendor|staff)$")
    assigned_to: str = Field(min_length=2, max_length=120)


class CaseResolveRequest(BaseModel):
    resolution_note: str = Field(min_length=3, max_length=2_000)


class KnowledgeUpsertRequest(BaseModel):
    category: str = Field(min_length=2, max_length=64)
    topic: str = Field(min_length=2, max_length=120)
    answer: str = Field(min_length=3, max_length=5_000)
    source_reference: str | None = Field(default=None, max_length=2_000)
    approval_status: str = Field(default="approved", pattern="^(draft|approved|archived)$")


class RoutingContactRequest(BaseModel):
    contact_type: str = Field(pattern="^(on_call|vendor|security)$")
    name: str = Field(min_length=2, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)


class VendorRouteRequest(BaseModel):
    incident_type: str = Field(min_length=2, max_length=64)
    vendor_id: UUID | None = None
    vendor_name: str | None = Field(default=None, min_length=2, max_length=160)
    contact_phone: str | None = Field(default=None, max_length=32)
    dispatch_mode: str = Field(
        default="inspection_only", pattern="^(inspection_only|auto_up_to_limit)$"
    )
    diagnostic_fee_cents: int | None = Field(default=None, ge=0)
    automatic_approval_limit_cents: int | None = Field(default=None, ge=0)
    payer_responsibility: str = Field(
        default="confirm_before_booking",
        pattern="^(association|resident|confirm_before_booking)$",
    )


class VendorCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    phone: str | None = Field(default=None, max_length=32)


class ResidentConsentRequest(BaseModel):
    status: str = Field(pattern="^(pending|accepted|declined|not_required)$")


class QuoteRequest(BaseModel):
    amount_cents: int = Field(ge=0)
    approver: str | None = Field(default=None, max_length=120)
    status: str = Field(default="received", pattern="^(received|approved|rejected)$")


class BillingRequest(BaseModel):
    final_amount_cents: int = Field(ge=0)
    billed_to: str = Field(pattern="^(association|resident|vendor|unknown)$")


class HealthResponse(BaseModel):
    status: str
    service: str

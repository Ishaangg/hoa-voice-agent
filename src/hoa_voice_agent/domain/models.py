from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ConversationState(StrEnum):
    AWAITING_RESIDENT_DETAILS = "awaiting_resident_details"
    READY = "ready"
    CLOSED = "closed"


class Intent(StrEnum):
    CONVERSATION = "conversation"
    FAQ = "faq"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


class IncidentType(StrEnum):
    FIRE_OR_SMOKE = "fire_or_smoke"
    GAS_SMELL = "gas_smell"
    ACTIVE_FLOODING = "active_flooding"
    ELEVATOR_ENTRAPMENT = "elevator_entrapment"
    ACCESS_FAILURE = "access_failure"
    HVAC = "hvac"
    COSMETIC = "cosmetic"
    UNKNOWN = "unknown"


class Priority(StrEnum):
    EMERGENCY = "emergency"
    URGENT = "urgent"
    ROUTINE = "routine"


class IncidentReport(BaseModel):
    incident_type: IncidentType
    description: str = Field(min_length=1, max_length=2_000)
    location: str | None = Field(default=None, max_length=300)
    water_active: bool | None = None
    source_known: bool | None = None
    resident_safe: bool | None = None


class EmergencyDecision(BaseModel):
    incident_type: IncidentType
    priority: Priority
    instructions: list[str]
    dispatch_vendor: bool
    transfer_to_on_call: bool
    create_work_order: bool


class TenantContext(BaseModel):
    management_company_id: UUID
    community_id: UUID


class Community(TenantContext):
    name: str
    office_phone: str
    on_call_phone: str
    faqs: dict[str, str]


class Resident(TenantContext):
    id: UUID
    first_name: str
    phone: str
    unit: str


class WorkOrder(TenantContext):
    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    resident_id: UUID | None = None
    incident_type: IncidentType
    priority: Priority
    description: str
    status: str = "open"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionType(StrEnum):
    WORK_ORDER_CREATED = "work_order_created"
    SMS_QUEUED = "sms_queued"
    SMS_FAILED = "sms_failed"
    TRANSFER_REQUESTED = "transfer_requested"


class WorkflowAction(BaseModel):
    type: ActionType
    detail: str
    reference_id: UUID | None = None

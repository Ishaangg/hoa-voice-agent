from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.fake import (
    FakeCallControlAdapter,
    FakeCommunityManagementAdapter,
    FakeMessagingAdapter,
)
from hoa_voice_agent.adapters.operations_webhook import (
    OperationsWebhookClient,
    WebhookCallControlAdapter,
    WebhookCommunityManagementAdapter,
    WebhookMessagingAdapter,
)
from hoa_voice_agent.adapters.twilio import TwilioMessagingAdapter
from hoa_voice_agent.api_models import (
    ConversationTurnRequest,
    ConversationTurnResponse,
    HealthResponse,
    StartConversationRequest,
    StartConversationResponse,
)
from hoa_voice_agent.config import Settings, get_settings
from hoa_voice_agent.domain.models import WorkOrder
from hoa_voice_agent.persistence import tables as _tables  # noqa: F401
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.repository import (
    ConversationTurnRepository,
    WorkflowEventRepository,
    WorkOrderRepository,
)
from hoa_voice_agent.persistence.seed import seed_demo_community_knowledge
from hoa_voice_agent.persistence.tables import (
    CallerProfileHistoryRecord,
    CallerProfileRecord,
    ConversationRecord,
    WorkflowEventRecord,
    WorkOrderRecord,
)
from hoa_voice_agent.services.conversation import (
    CommunityNotFoundError,
    ConversationNotFoundError,
    ConversationService,
)
from hoa_voice_agent.services.nvidia_conversation_responder import NvidiaConversationResponder
from hoa_voice_agent.services.nvidia_incident_extractor import NvidiaIncidentExtractor


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    database = Database(app_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = database
        directory = FakeCommunityManagementAdapter()
        operations: OperationsWebhookClient | None = None
        if app_settings.operations_mode == "webhook":
            if not app_settings.operations_webhook_base_url:
                raise RuntimeError(
                    "OPERATIONS_WEBHOOK_BASE_URL is required when OPERATIONS_MODE=webhook."
                )
            operations = OperationsWebhookClient(
                base_url=app_settings.operations_webhook_base_url,
                bearer_token=app_settings.operations_webhook_bearer_token,
            )
            app.state.community_management = WebhookCommunityManagementAdapter(
                directory,
                operations,
            )
            app.state.call_control = WebhookCallControlAdapter(operations)
        else:
            app.state.community_management = directory
            app.state.call_control = FakeCallControlAdapter()

        if app_settings.sms_mode == "twilio":
            if not all(
                (
                    app_settings.twilio_account_sid,
                    app_settings.twilio_auth_token,
                    app_settings.twilio_from_phone,
                )
            ):
                raise RuntimeError(
                    "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_PHONE are required "
                    "when SMS_MODE=twilio."
                )
            app.state.messaging = TwilioMessagingAdapter(
                account_sid=app_settings.twilio_account_sid,
                auth_token=app_settings.twilio_auth_token,
                from_phone=app_settings.twilio_from_phone,
            )
        elif app_settings.sms_mode == "webhook":
            if not operations:
                raise RuntimeError("SMS_MODE=webhook requires OPERATIONS_MODE=webhook.")
            app.state.messaging = WebhookMessagingAdapter(operations)
        else:
            app.state.messaging = FakeMessagingAdapter()
        await database.create_schema()
        async with database.session_factory() as seed_session:
            await seed_demo_community_knowledge(seed_session)
        try:
            yield
        finally:
            if operations:
                await operations.close()
            await database.dispose()

    app = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    demo_path = Path(__file__).parent / "static" / "demo.html"
    operations_path = Path(__file__).parent / "static" / "operations.html"

    async def get_session(request: Request):
        async for session in request.app.state.database.sessions():
            yield session

    async def get_service(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> ConversationService:
        incident_extractor = (
            NvidiaIncidentExtractor(
                api_key=app_settings.nvidia_api_key,
                base_url=app_settings.nvidia_llm_base_url,
                model=app_settings.nvidia_llm_model,
            )
            if app_settings.nvidia_api_key
            else None
        )
        conversation_responder = (
            NvidiaConversationResponder(
                api_key=app_settings.nvidia_api_key,
                base_url=app_settings.nvidia_llm_base_url,
                model=app_settings.nvidia_llm_model,
            )
            if app_settings.nvidia_api_key
            else None
        )
        return ConversationService(
            session=session,
            community_management=request.app.state.community_management,
            messaging=request.app.state.messaging,
            call_control=request.app.state.call_control,
            incident_extractor=incident_extractor,
            conversation_responder=conversation_responder,
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service=app_settings.app_name)

    @app.get("/demo", include_in_schema=False)
    async def demo() -> FileResponse:
        return FileResponse(demo_path)

    @app.get("/operations", include_in_schema=False)
    async def operations_dashboard() -> FileResponse:
        return FileResponse(operations_path)

    @app.get("/api/v1/operations/summary")
    async def operations_summary(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict[str, int]:
        async def count(model, *conditions) -> int:
            query = select(func.count()).select_from(model)
            if conditions:
                query = query.where(*conditions)
            return int(await session.scalar(query) or 0)

        return {
            "unverified_callers": await count(
                CallerProfileRecord,
                CallerProfileRecord.identity_status == "unverified",
            ),
            "calls": await count(ConversationRecord),
            "calls_today": await count(
                ConversationRecord,
                ConversationRecord.created_at >= datetime.now(UTC).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
            ),
            "open_work_orders": await count(WorkOrderRecord, WorkOrderRecord.status == "open"),
            "urgent_open_work_orders": await count(
                WorkOrderRecord,
                WorkOrderRecord.status == "open",
                WorkOrderRecord.priority.in_(["urgent", "emergency"]),
            ),
            "active_emergencies": await count(
                WorkOrderRecord,
                WorkOrderRecord.status == "open",
                WorkOrderRecord.priority == "emergency",
            ),
            "emergency_escalations": await count(
                WorkflowEventRecord,
                WorkflowEventRecord.event_type == "transfer_requested",
            ),
            "failures": await count(
                WorkflowEventRecord,
                WorkflowEventRecord.event_type.endswith("_failed"),
            ),
        }

    @app.get("/api/v1/operations/callers")
    async def operations_callers(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await session.scalars(
            select(CallerProfileRecord).order_by(CallerProfileRecord.created_at.desc()).limit(100)
        )
        return [
            {
                "id": str(row.id),
                "phone": row.caller_phone,
                "name": row.claimed_name,
                "unit": row.claimed_unit,
                "identity_status": row.identity_status,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/operations/conversations")
    async def operations_conversations(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await session.scalars(
            select(ConversationRecord)
            .order_by(ConversationRecord.created_at.desc())
            .limit(100)
        )
        return [
            {
                "id": str(row.id),
                "phone": row.caller_phone,
                "name": row.caller_name,
                "unit": row.caller_unit,
                "state": row.state,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/operations/work-orders")
    async def operations_work_orders(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await session.scalars(
            select(WorkOrderRecord).order_by(WorkOrderRecord.created_at.desc()).limit(100)
        )
        return [
            {
                "id": str(row.id),
                "conversation_id": str(row.conversation_id),
                "incident_type": row.incident_type,
                "priority": row.priority,
                "description": row.description,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/operations/events")
    async def operations_events(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await WorkflowEventRepository(session).list_recent()
        return [
            {
                "id": str(row.id),
                "conversation_id": str(row.conversation_id),
                "event_type": row.event_type,
                "detail": row.detail,
                "reference_id": str(row.reference_id) if row.reference_id else None,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/operations/transcripts/{conversation_id}")
    async def operations_transcript(
        conversation_id: UUID,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await ConversationTurnRepository(session).list_for_conversation(conversation_id)
        return [
            {
                "id": str(row.id),
                "speaker": row.speaker,
                "text": row.text,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/operations/profile-history")
    async def operations_profile_history(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await session.scalars(
            select(CallerProfileHistoryRecord)
            .order_by(CallerProfileHistoryRecord.created_at.desc())
            .limit(100)
        )
        return [
            {
                "id": str(row.id),
                "caller_profile_id": str(row.caller_profile_id),
                "field": row.field_name,
                "old_value": row.old_value,
                "new_value": row.new_value,
                "source": row.change_source,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.post(
        "/api/v1/conversations",
        response_model=StartConversationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def start_conversation(
        body: StartConversationRequest,
        service: Annotated[ConversationService, Depends(get_service)],
    ) -> StartConversationResponse:
        try:
            return await service.start(body.caller_phone, body.community_id)
        except CommunityNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Community not found") from exc

    @app.post(
        "/api/v1/conversations/{conversation_id}/turns",
        response_model=ConversationTurnResponse,
    )
    async def submit_turn(
        conversation_id: UUID,
        body: ConversationTurnRequest,
        service: Annotated[ConversationService, Depends(get_service)],
    ) -> ConversationTurnResponse:
        try:
            response = await service.handle_turn(conversation_id, body.text)
            await service.record_assistant_turn(conversation_id, response.say)
            return response
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        except CommunityNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Community not found") from exc

    @app.get(
        "/api/v1/conversations/{conversation_id}/work-orders",
        response_model=list[WorkOrder],
    )
    async def list_work_orders(
        conversation_id: UUID,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[WorkOrder]:
        return await WorkOrderRepository(session).list_for_conversation(conversation_id)

    return app


app = create_app()

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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
    BillingRequest,
    CaseAssignRequest,
    CaseResolveRequest,
    ConversationTurnRequest,
    ConversationTurnResponse,
    HealthResponse,
    KnowledgeUpsertRequest,
    QuoteRequest,
    ResidentConsentRequest,
    RoutingContactRequest,
    StaffLoginRequest,
    StartConversationRequest,
    StartConversationResponse,
    VendorCreateRequest,
    VendorRouteRequest,
)
from hoa_voice_agent.config import Settings, get_settings
from hoa_voice_agent.domain.models import WorkOrder
from hoa_voice_agent.persistence import tables as _tables  # noqa: F401
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.repository import (
    ConversationTurnRepository,
    WorkflowEventRepository,
    WorkOrderRepository,
    normalize_phone_number,
)
from hoa_voice_agent.persistence.seed import (
    seed_demo_community_knowledge,
    seed_demo_operations_console,
)
from hoa_voice_agent.persistence.tables import (
    CallerProfileHistoryRecord,
    CallerProfileRecord,
    CaseActionRecord,
    CommunityKnowledgeRecord,
    CommunityRecord,
    ConversationRecord,
    KnowledgeRevisionRecord,
    RoutingContactRecord,
    StaffMembershipRecord,
    StaffSessionRecord,
    StaffUserRecord,
    VendorDispatchRecord,
    VendorRecord,
    VendorRouteRecord,
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
from hoa_voice_agent.services.operations_console import (
    hash_session_token,
    new_session_token,
    verify_password,
)
from hoa_voice_agent.voice.twilio_voice import is_valid_twilio_request


@dataclass(frozen=True)
class ConsolePrincipal:
    user_id: UUID
    display_name: str
    email: str
    management_company_id: UUID
    role: str
    community_id: UUID | None


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
            if (
                app_settings.vendor_notification_channel == "whatsapp"
                and not app_settings.twilio_whatsapp_from
            ):
                raise RuntimeError(
                    "TWILIO_WHATSAPP_FROM is required when vendor notifications use WhatsApp."
                )
            app.state.messaging = TwilioMessagingAdapter(
                account_sid=app_settings.twilio_account_sid,
                auth_token=app_settings.twilio_auth_token,
                from_phone=app_settings.twilio_from_phone,
                whatsapp_from=app_settings.twilio_whatsapp_from,
                whatsapp_content_sid=app_settings.twilio_whatsapp_content_sid,
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
            await seed_demo_operations_console(seed_session)
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
    console_path = Path(__file__).resolve().parents[2] / "web" / "dist" / "index.html"
    if console_path.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=console_path.parent / "assets"),
            name="operations-assets",
        )

    async def get_session(request: Request):
        async for session in request.app.state.database.sessions():
            yield session

    async def get_console_principal(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> ConsolePrincipal:
        token = request.cookies.get("hoa_ops_session")
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
        staff_session = await session.scalar(
            select(StaffSessionRecord).where(
                StaffSessionRecord.token_hash == hash_session_token(token),
                StaffSessionRecord.expires_at > datetime.now(UTC),
            )
        )
        if not staff_session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
        user = await session.get(StaffUserRecord, staff_session.staff_user_id)
        membership = await session.scalar(
            select(StaffMembershipRecord).where(
                StaffMembershipRecord.staff_user_id == staff_session.staff_user_id,
                StaffMembershipRecord.management_company_id == staff_session.management_company_id,
            )
        )
        if not user or not user.is_active or not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Staff access unavailable",
            )
        return ConsolePrincipal(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            management_company_id=membership.management_company_id,
            role=membership.role,
            community_id=membership.community_id,
        )

    def require_role(principal: ConsolePrincipal, *roles: str) -> None:
        if principal.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

    def assert_community_scope(principal: ConsolePrincipal, community_id: UUID) -> None:
        if principal.role != "operations_admin" and principal.community_id not in (
            None,
            community_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Community access denied",
            )

    async def get_scoped_work_order(
        work_order_id: UUID,
        principal: ConsolePrincipal,
        session: AsyncSession,
    ) -> WorkOrderRecord:
        record = await session.get(WorkOrderRecord, work_order_id)
        if not record or record.management_company_id != principal.management_company_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        assert_community_scope(principal, record.community_id)
        if principal.role == "on_call_responder" and record.assigned_to != principal.display_name:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Case is not assigned to you",
            )
        return record

    async def record_case_action(
        session: AsyncSession,
        *,
        work_order_id: UUID,
        principal: ConsolePrincipal,
        action_type: str,
        detail: str,
    ) -> None:
        session.add(
            CaseActionRecord(
                work_order_id=work_order_id,
                actor_user_id=principal.user_id,
                action_type=action_type,
                detail=detail,
            )
        )
        await session.commit()

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
            vendor_notification_channel=app_settings.vendor_notification_channel,
            incident_extractor=incident_extractor,
            conversation_responder=conversation_responder,
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service=app_settings.app_name)

    @app.post("/api/v1/webhooks/twilio/whatsapp")
    async def incoming_vendor_whatsapp(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        """Process a registered vendor's WhatsApp action for its newest dispatch."""

        raw_body = (await request.body()).decode("utf-8")
        form_values = {key: values[-1] for key, values in parse_qs(raw_body).items()}
        if app_settings.twilio_validate_webhooks and app_settings.sms_mode == "twilio":
            webhook_url = app_settings.twilio_whatsapp_webhook_url
            if not webhook_url:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Twilio WhatsApp webhook is not configured.",
                )
            if not is_valid_twilio_request(
                auth_token=app_settings.twilio_auth_token or "",
                url=webhook_url,
                form_values=form_values,
                signature=request.headers.get("X-Twilio-Signature"),
            ):
                raise HTTPException(status_code=403, detail="Invalid Twilio webhook signature.")

        vendor_phone = normalize_phone_number(form_values.get("From", ""))
        inbound_reference = form_values.get("MessageSid") or form_values.get("SmsMessageSid")
        dispatches = WorkOrderRepository(session)
        dispatch = (
            await dispatches.get_vendor_dispatch_for_inbound_reference(
                vendor_phone, inbound_reference
            )
            if inbound_reference
            else None
        )
        if dispatch is None:
            dispatch = await dispatches.latest_vendor_dispatch_for_phone(vendor_phone)
        if dispatch is None:
            return Response(
                content=(
                    "<Response><Message>No active HOA service request was found "
                    "for this number.</Message></Response>"
                ),
                media_type="application/xml",
            )

        action_text = " ".join(
            form_values.get(key, "")
            for key in ("ButtonPayload", "ButtonText", "Body")
        ).lower()
        if any(
            term in action_text
            for term in (
                "decline",
                "cannot take",
                "can't take",
                "cannot_take",
                "unavailable",
            )
        ):
            action = "declined"
            reply = "Thanks. We marked this request unavailable for your team."
        elif any(
            term in action_text
            for term in (
                "on my way",
                "on the way",
                "on_my_way",
                "en route",
                "enroute",
            )
        ):
            action = "en_route"
            reply = "Thanks. We marked the vendor as on the way."
        elif "quote" in action_text or "request_quote" in action_text:
            action = "quote_requested"
            reply = (
                "Thanks. Please submit the quote through the secure link "
                "provided by the HOA team."
            )
        elif any(
            term in action_text
            for term in (
                "completed",
                "complete",
                "work_completed",
                "finished",
                "done",
            )
        ):
            action = "completed"
            reply = (
                "Thanks. We marked the service work completed; "
                "the HOA team will close the case."
            )
        elif any(term in action_text for term in ("accept", "accepted", "yes", "take")):
            action = "acknowledged"
            reply = "Thanks. We acknowledged your acceptance of this service request."
        else:
            return Response(
                content=(
                    "<Response><Message>Reply ACCEPT, DECLINE, ON MY WAY, "
                    "QUOTE, or COMPLETED.</Message></Response>"
                ),
                media_type="application/xml",
            )

        if inbound_reference and dispatch.last_inbound_reference == inbound_reference:
            return Response(
                content=f"<Response><Message>{escape(reply)}</Message></Response>",
                media_type="application/xml",
            )

        now = datetime.now(UTC)
        dispatch.status = action
        dispatch.last_inbound_reference = inbound_reference
        dispatch.last_action_at = now
        if action == "acknowledged":
            dispatch.acknowledged_at = dispatch.acknowledged_at or now
        elif action == "completed":
            dispatch.completed_at = dispatch.completed_at or now
        work_order = await session.get(WorkOrderRecord, dispatch.work_order_id)
        if work_order:
            if action == "declined":
                work_order.status = "open"
                work_order.assigned_to = None
                work_order.assignee_type = None
            elif action == "quote_requested":
                work_order.status = "awaiting_quote"
            elif action == "completed":
                work_order.status = "completed"
            else:
                work_order.status = "assigned"
            session.add(
                CaseActionRecord(
                    work_order_id=work_order.id,
                    actor_user_id=None,
                    action_type=f"vendor_{action}",
                    detail=f"Vendor {dispatch.vendor_name} reported action via WhatsApp.",
                )
            )
        await session.commit()
        return Response(
            content=f"<Response><Message>{escape(reply)}</Message></Response>",
            media_type="application/xml",
        )

    @app.get("/demo", include_in_schema=False)
    async def demo() -> FileResponse:
        return FileResponse(demo_path)

    @app.get("/operations", include_in_schema=False)
    async def operations_dashboard() -> FileResponse:
        return FileResponse(console_path if console_path.exists() else operations_path)

    @app.post("/api/v1/console/auth/login")
    async def console_login(
        body: StaffLoginRequest,
        response: Response,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        user = await session.scalar(
            select(StaffUserRecord).where(StaffUserRecord.email == body.email.lower())
        )
        if not user or not user.is_active or not verify_password(body.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        membership = await session.scalar(
            select(StaffMembershipRecord).where(StaffMembershipRecord.staff_user_id == user.id)
        )
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No management company membership",
            )
        token = new_session_token()
        session.add(
            StaffSessionRecord(
                token_hash=hash_session_token(token),
                staff_user_id=user.id,
                management_company_id=membership.management_company_id,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
        )
        await session.commit()
        response.set_cookie(
            "hoa_ops_session",
            token,
            httponly=True,
            samesite="lax",
            secure=app_settings.app_env == "production",
            max_age=60 * 60 * 12,
        )
        return {"display_name": user.display_name, "role": membership.role}

    @app.post("/api/v1/console/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def console_logout(response: Response) -> Response:
        response.delete_cookie("hoa_ops_session")
        return response

    @app.get("/api/v1/console/me")
    async def console_me(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
    ) -> dict:
        return {
            "id": str(principal.user_id),
            "display_name": principal.display_name,
            "email": principal.email,
            "role": principal.role,
            "management_company_id": str(principal.management_company_id),
            "community_id": str(principal.community_id) if principal.community_id else None,
        }

    @app.get("/api/v1/console/communities")
    async def console_communities(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        statement = select(CommunityRecord).where(
            CommunityRecord.management_company_id == principal.management_company_id,
            CommunityRecord.is_active.is_(True),
        )
        if principal.community_id:
            statement = statement.where(CommunityRecord.id == principal.community_id)
        rows = await session.scalars(statement.order_by(CommunityRecord.name))
        return [
            {
                "id": str(row.id),
                "name": row.name,
                "office_phone": row.office_phone,
                "emergency_policy_summary": row.emergency_policy_summary,
            }
            for row in rows
        ]

    @app.get("/api/v1/console/dashboard")
    async def console_dashboard(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        statement = select(WorkOrderRecord).where(
            WorkOrderRecord.management_company_id == principal.management_company_id
        )
        if principal.community_id:
            statement = statement.where(WorkOrderRecord.community_id == principal.community_id)
        rows = list(await session.scalars(statement.order_by(WorkOrderRecord.created_at.desc())))
        active = [row for row in rows if row.status != "resolved"]
        priority_queue = [
            row
            for row in active
            if row.priority in {"emergency", "urgent"} or row.status in {"open", "failed"}
        ][:8]
        communities = {
            row.id: row.name
            for row in await session.scalars(
                select(CommunityRecord).where(
                    CommunityRecord.management_company_id == principal.management_company_id
                )
            )
        }
        return {
            "kpis": {
                "active_emergencies": sum(row.priority == "emergency" for row in active),
                "unacknowledged_urgent": sum(
                    row.priority in {"emergency", "urgent"} and row.acknowledged_at is None
                    for row in active
                ),
                "overdue_assignments": sum(row.assigned_to is None for row in active),
                "open_cases": len(active),
                "calls_today": await session.scalar(
                    select(func.count())
                    .select_from(ConversationRecord)
                    .where(
                        ConversationRecord.management_company_id == principal.management_company_id,
                        ConversationRecord.created_at
                        >= datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
                    )
                )
                or 0,
            },
            "priority_queue": [
                {
                    "id": str(row.id),
                    "community": communities.get(row.community_id, "Unknown community"),
                    "priority": row.priority,
                    "incident_type": row.incident_type,
                    "status": row.status,
                    "assigned_to": row.assigned_to,
                    "created_at": row.created_at,
                }
                for row in priority_queue
            ],
        }

    @app.get("/api/v1/console/cases")
    async def console_cases(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
        community_id: UUID | None = None,
        priority: str | None = None,
        case_status: str | None = None,
    ) -> list[dict]:
        statement = select(WorkOrderRecord).where(
            WorkOrderRecord.management_company_id == principal.management_company_id
        )
        selected_community = community_id or principal.community_id
        if selected_community:
            assert_community_scope(principal, selected_community)
            statement = statement.where(WorkOrderRecord.community_id == selected_community)
        if priority:
            statement = statement.where(WorkOrderRecord.priority == priority)
        if case_status:
            statement = statement.where(WorkOrderRecord.status == case_status)
        if principal.role == "on_call_responder":
            statement = statement.where(WorkOrderRecord.assigned_to == principal.display_name)
        communities = {
            row.id: row.name
            for row in await session.scalars(
                select(CommunityRecord).where(
                    CommunityRecord.management_company_id == principal.management_company_id
                )
            )
        }
        rows = await session.scalars(statement.order_by(WorkOrderRecord.created_at.desc()))
        return [
            {
                "id": str(row.id),
                "community_id": str(row.community_id),
                "community": communities.get(row.community_id, "Unknown community"),
                "priority": row.priority,
                "incident_type": row.incident_type,
                "description": row.description,
                "status": row.status,
                "assigned_to": row.assigned_to,
                "assignee_type": row.assignee_type,
                "created_at": row.created_at,
                "acknowledged_at": row.acknowledged_at,
                "resolved_at": row.resolved_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/console/calls")
    async def console_calls(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
        community_id: UUID | None = None,
    ) -> list[dict]:
        """Return call history, including calls that did not create a work order."""

        statement = select(ConversationRecord).where(
            ConversationRecord.management_company_id == principal.management_company_id
        )
        selected_community = community_id or principal.community_id
        if selected_community:
            assert_community_scope(principal, selected_community)
            statement = statement.where(ConversationRecord.community_id == selected_community)
        communities = {
            row.id: row.name
            for row in await session.scalars(
                select(CommunityRecord).where(
                    CommunityRecord.management_company_id == principal.management_company_id
                )
            )
        }
        rows = await session.scalars(statement.order_by(ConversationRecord.created_at.desc()))
        calls = []
        for row in rows:
            work_order = await session.scalar(
                select(WorkOrderRecord).where(WorkOrderRecord.conversation_id == row.id)
            )
            calls.append(
                {
                    "id": str(row.id),
                    "community_id": str(row.community_id),
                    "community": communities.get(row.community_id, "Unknown community"),
                    "caller_name": row.caller_name,
                    "caller_phone": row.caller_phone,
                    "caller_unit": row.caller_unit,
                    "state": row.state,
                    "created_at": row.created_at,
                    "work_order_id": str(work_order.id) if work_order else None,
                    "work_order_status": work_order.status if work_order else None,
                }
            )
        return calls

    @app.get("/api/v1/console/calls/{conversation_id}")
    async def console_call_detail(
        conversation_id: UUID,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        conversation = await session.scalar(
            select(ConversationRecord).where(
                ConversationRecord.id == conversation_id,
                ConversationRecord.management_company_id == principal.management_company_id,
            )
        )
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
        assert_community_scope(principal, conversation.community_id)
        profile = await session.scalar(
            select(CallerProfileRecord).where(
                CallerProfileRecord.community_id == conversation.community_id,
                CallerProfileRecord.caller_phone == conversation.caller_phone,
            )
        )
        turns = await session.scalars(
            select(_tables.ConversationTurnRecord)
            .where(_tables.ConversationTurnRecord.conversation_id == conversation.id)
            .order_by(_tables.ConversationTurnRecord.created_at)
        )
        work_order = await session.scalar(
            select(WorkOrderRecord).where(WorkOrderRecord.conversation_id == conversation.id)
        )
        return {
            "id": str(conversation.id),
            "caller": {
                "name": conversation.caller_name,
                "phone": conversation.caller_phone,
                "unit": conversation.caller_unit,
                "identity_status": profile.identity_status if profile else "unknown",
            },
            "transcript": [
                {"speaker": turn.speaker, "text": turn.text, "created_at": turn.created_at}
                for turn in turns
            ],
            "work_order_id": str(work_order.id) if work_order else None,
        }

    @app.get("/api/v1/console/cases/{work_order_id}")
    async def console_case_detail(
        work_order_id: UUID,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        row = await get_scoped_work_order(work_order_id, principal, session)
        conversation = await session.get(ConversationRecord, row.conversation_id)
        profile = (
            await session.scalar(
                select(CallerProfileRecord).where(
                    CallerProfileRecord.community_id == row.community_id,
                    CallerProfileRecord.caller_phone == conversation.caller_phone
                    if conversation
                    else False,
                )
            )
            if conversation
            else None
        )
        turns = await session.scalars(
            select(_tables.ConversationTurnRecord)
            .where(_tables.ConversationTurnRecord.conversation_id == row.conversation_id)
            .order_by(_tables.ConversationTurnRecord.created_at)
        )
        actions = await session.scalars(
            select(CaseActionRecord)
            .where(CaseActionRecord.work_order_id == row.id)
            .order_by(CaseActionRecord.created_at)
        )
        dispatch = await session.scalar(
            select(VendorDispatchRecord).where(VendorDispatchRecord.work_order_id == row.id)
        )
        routes = await session.scalars(
            select(VendorRouteRecord).where(
                VendorRouteRecord.community_id == row.community_id,
                VendorRouteRecord.incident_type == row.incident_type,
                VendorRouteRecord.is_active.is_(True),
            )
        )
        contacts = await session.scalars(
            select(RoutingContactRecord).where(
                RoutingContactRecord.community_id == row.community_id,
                RoutingContactRecord.management_company_id == principal.management_company_id,
                RoutingContactRecord.is_active.is_(True),
            )
        )
        return {
            "id": str(row.id),
            "priority": row.priority,
            "incident_type": row.incident_type,
            "description": row.description,
            "status": row.status,
            "assigned_to": row.assigned_to,
            "assignee_type": row.assignee_type,
            "resolution_note": row.resolution_note,
            "cost": {
                "price_status": row.price_status,
                "price_shown_to_caller": row.price_shown_to_caller,
                "resident_consent": row.resident_consent,
                "quote_status": row.quote_status,
                "quote_amount_cents": row.quote_amount_cents,
                "quote_approver": row.quote_approver,
                "final_amount_cents": row.final_amount_cents,
                "billed_to": row.billed_to,
            },
            "vendor_dispatch": (
                {
                    "vendor_name": dispatch.vendor_name,
                    "vendor_phone": dispatch.vendor_phone,
                    "status": dispatch.status,
                    "notification_channel": dispatch.notification_channel,
                    "provider_reference": dispatch.provider_reference,
                    "attempt_count": dispatch.attempt_count,
                    "failure_detail": dispatch.failure_detail,
                    "sent_at": dispatch.sent_at,
                    "acknowledged_at": dispatch.acknowledged_at,
                    "completed_at": dispatch.completed_at,
                    "last_action_at": dispatch.last_action_at,
                }
                if dispatch
                else None
            ),
            "urgency_rationale": "Priority was set by the deterministic emergency policy.",
            "caller": {
                "name": conversation.caller_name if conversation else None,
                "phone": conversation.caller_phone if conversation else None,
                "unit": conversation.caller_unit if conversation else None,
                "identity_status": profile.identity_status if profile else "unknown",
            },
            "transcript": [
                {"speaker": turn.speaker, "text": turn.text, "created_at": turn.created_at}
                for turn in turns
            ],
            "timeline": [
                {
                    "type": action.action_type,
                    "detail": action.detail,
                    "created_at": action.created_at,
                }
                for action in actions
            ],
            "suggested_routes": [
                {
                    "vendor_name": route.vendor_name,
                    "phone": (
                        f"{route.contact_phone or 'No phone'} · dispatch {dispatch.status}"
                        if dispatch and dispatch.vendor_name == route.vendor_name
                        else route.contact_phone
                    ),
                    "diagnostic_fee_cents": route.diagnostic_fee_cents,
                    "dispatch_mode": route.dispatch_mode,
                    "payer_responsibility": route.payer_responsibility,
                }
                for route in routes
            ],
            "assignment_options": [
                {
                    "name": contact.name,
                    "type": "on_call" if contact.contact_type == "on_call" else "staff",
                    "phone": contact.phone,
                }
                for contact in contacts
            ]
            + [
                {"name": route.vendor_name, "type": "vendor", "phone": route.contact_phone}
                for route in routes
            ],
        }

    @app.post("/api/v1/console/cases/{work_order_id}/acknowledge")
    async def acknowledge_case(
        work_order_id: UUID,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        row = await get_scoped_work_order(work_order_id, principal, session)
        if row.status == "resolved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Resolved case cannot be acknowledged"
            )
        row.status = "acknowledged"
        row.acknowledged_at = datetime.now(UTC)
        await session.commit()
        await record_case_action(
            session,
            work_order_id=row.id,
            principal=principal,
            action_type="acknowledged",
            detail=f"Acknowledged by {principal.display_name}",
        )
        return {"status": row.status}

    @app.post("/api/v1/console/cases/{work_order_id}/assign")
    async def assign_case(
        work_order_id: UUID,
        body: CaseAssignRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        row = await get_scoped_work_order(work_order_id, principal, session)
        if row.status == "resolved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Resolved case cannot be assigned"
            )
        row.status = "assigned"
        row.assigned_to = body.assigned_to
        row.assignee_type = body.assignee_type
        await session.commit()
        await record_case_action(
            session,
            work_order_id=row.id,
            principal=principal,
            action_type="assigned",
            detail=f"Assigned to {body.assigned_to} ({body.assignee_type})",
        )
        return {"status": row.status, "assigned_to": row.assigned_to}

    @app.post("/api/v1/console/cases/{work_order_id}/resolve")
    async def resolve_case(
        work_order_id: UUID,
        body: CaseResolveRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        row = await get_scoped_work_order(work_order_id, principal, session)
        if row.final_amount_cents is not None and row.dispatch_route_id:
            route = await session.get(VendorRouteRecord, row.dispatch_route_id)
            if (
                route
                and route.automatic_approval_limit_cents is not None
                and row.final_amount_cents > route.automatic_approval_limit_cents
                and row.quote_status != "approved"
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Additional repair exceeds the automatic limit; "
                        "approve the vendor quote first"
                    ),
                )
        row.status = "resolved"
        row.resolved_at = datetime.now(UTC)
        row.resolution_note = body.resolution_note
        await session.commit()
        await record_case_action(
            session,
            work_order_id=row.id,
            principal=principal,
            action_type="resolved",
            detail=body.resolution_note,
        )
        return {"status": row.status}

    @app.post("/api/v1/console/cases/{work_order_id}/consent")
    async def record_case_consent(
        work_order_id: UUID,
        body: ResidentConsentRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        get_scoped = await get_scoped_work_order(work_order_id, principal, session)
        get_scoped.resident_consent = body.status
        await session.commit()
        await record_case_action(
            session,
            work_order_id=get_scoped.id,
            principal=principal,
            action_type="consent_recorded",
            detail=f"Resident consent: {body.status}",
        )
        return {"resident_consent": get_scoped.resident_consent}

    @app.post("/api/v1/console/cases/{work_order_id}/quote")
    async def record_case_quote(
        work_order_id: UUID,
        body: QuoteRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager", "on_call_responder")
        row = await get_scoped_work_order(work_order_id, principal, session)
        row.quote_amount_cents = body.amount_cents
        row.quote_status = body.status
        row.quote_approver = (
            body.approver or principal.display_name
            if body.status == "approved"
            else body.approver
        )
        await session.commit()
        await record_case_action(
            session,
            work_order_id=row.id,
            principal=principal,
            action_type="quote_updated",
            detail=f"Quote {body.status}: {body.amount_cents} cents",
        )
        return {"quote_status": row.quote_status, "quote_amount_cents": row.quote_amount_cents}

    @app.post("/api/v1/console/cases/{work_order_id}/billing")
    async def record_case_billing(
        work_order_id: UUID,
        body: BillingRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        row = await get_scoped_work_order(work_order_id, principal, session)
        row.final_amount_cents = body.final_amount_cents
        row.billed_to = body.billed_to
        await session.commit()
        await record_case_action(
            session,
            work_order_id=row.id,
            principal=principal,
            action_type="billing_recorded",
            detail=f"Final amount {body.final_amount_cents} cents billed to {body.billed_to}",
        )
        return {"final_amount_cents": row.final_amount_cents, "billed_to": row.billed_to}

    @app.get("/api/v1/console/communities/{community_id}/routing")
    async def community_routing(
        community_id: UUID,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        assert_community_scope(principal, community_id)
        contacts = await session.scalars(
            select(RoutingContactRecord).where(
                RoutingContactRecord.community_id == community_id,
                RoutingContactRecord.management_company_id == principal.management_company_id,
            )
        )
        vendors = await session.scalars(
            select(VendorRouteRecord).where(
                VendorRouteRecord.community_id == community_id,
                VendorRouteRecord.management_company_id == principal.management_company_id,
            )
        )
        vendor_registry = await session.scalars(
            select(VendorRecord).where(
                VendorRecord.management_company_id == principal.management_company_id,
                VendorRecord.is_active.is_(True),
            ).order_by(VendorRecord.name)
        )
        return {
            "contacts": [
                {
                    "id": str(row.id),
                    "type": row.contact_type,
                    "name": row.name,
                    "phone": row.phone,
                    "email": row.email,
                }
                for row in contacts
            ],
            "vendor_routes": [
                {
                    "id": str(row.id),
                    "incident_type": row.incident_type,
                    "vendor_name": row.vendor_name,
                    "phone": row.contact_phone,
                    "vendor_id": str(row.vendor_id) if row.vendor_id else None,
                    "dispatch_mode": row.dispatch_mode,
                    "diagnostic_fee_cents": row.diagnostic_fee_cents,
                    "automatic_approval_limit_cents": row.automatic_approval_limit_cents,
                    "payer_responsibility": row.payer_responsibility,
                }
                for row in vendors
            ],
            "vendor_registry": [
                {"id": str(vendor.id), "name": vendor.name, "phone": vendor.phone}
                for vendor in vendor_registry
            ],
        }

    @app.get("/api/v1/console/vendors")
    async def list_vendors(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        rows = await session.scalars(
            select(VendorRecord).where(
                VendorRecord.management_company_id == principal.management_company_id
            ).order_by(VendorRecord.name)
        )
        return [
            {"id": str(row.id), "name": row.name, "phone": row.phone, "is_active": row.is_active}
            for row in rows
        ]

    @app.post("/api/v1/console/vendors")
    async def create_vendor(
        body: VendorCreateRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        existing = await session.scalar(
            select(VendorRecord).where(
                VendorRecord.management_company_id == principal.management_company_id,
                VendorRecord.name == body.name,
            )
        )
        if existing:
            return {"id": str(existing.id), "name": existing.name, "phone": existing.phone}
        row = VendorRecord(
            management_company_id=principal.management_company_id,
            name=body.name,
            phone=body.phone,
        )
        session.add(row)
        await session.commit()
        return {"id": str(row.id), "name": row.name, "phone": row.phone}

    @app.post("/api/v1/console/communities/{community_id}/routing/contacts")
    async def create_routing_contact(
        community_id: UUID,
        body: RoutingContactRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        assert_community_scope(principal, community_id)
        record = RoutingContactRecord(
            management_company_id=principal.management_company_id,
            community_id=community_id,
            contact_type=body.contact_type,
            name=body.name,
            phone=body.phone,
            email=body.email,
        )
        session.add(record)
        await session.commit()
        return {"id": str(record.id)}

    @app.post("/api/v1/console/communities/{community_id}/routing/vendors")
    async def create_vendor_route(
        community_id: UUID,
        body: VendorRouteRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        assert_community_scope(principal, community_id)
        vendor = await session.get(VendorRecord, body.vendor_id) if body.vendor_id else None
        if vendor and vendor.management_company_id != principal.management_company_id:
            raise HTTPException(status_code=403, detail="Vendor is outside your company")
        if not vendor and body.vendor_name:
            vendor = await session.scalar(
                select(VendorRecord).where(
                    VendorRecord.management_company_id == principal.management_company_id,
                    VendorRecord.name == body.vendor_name,
                )
            )
            if not vendor:
                vendor = VendorRecord(
                    management_company_id=principal.management_company_id,
                    name=body.vendor_name,
                    phone=body.contact_phone,
                )
                session.add(vendor)
                await session.flush()
        if not vendor:
            raise HTTPException(status_code=422, detail="Select or provide a vendor")
        record = VendorRouteRecord(
            management_company_id=principal.management_company_id,
            community_id=community_id,
            incident_type=body.incident_type,
            vendor_id=vendor.id,
            vendor_name=vendor.name,
            contact_phone=body.contact_phone or vendor.phone,
            dispatch_mode=body.dispatch_mode,
            diagnostic_fee_cents=body.diagnostic_fee_cents,
            automatic_approval_limit_cents=body.automatic_approval_limit_cents,
            payer_responsibility=body.payer_responsibility,
        )
        session.add(record)
        await session.commit()
        return {"id": str(record.id)}

    @app.patch("/api/v1/console/communities/{community_id}/routing/vendors/{route_id}")
    async def update_vendor_route(
        community_id: UUID,
        route_id: UUID,
        body: VendorRouteRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        assert_community_scope(principal, community_id)
        record = await session.scalar(
            select(VendorRouteRecord).where(
                VendorRouteRecord.id == route_id,
                VendorRouteRecord.community_id == community_id,
                VendorRouteRecord.management_company_id == principal.management_company_id,
            )
        )
        if not record:
            raise HTTPException(status_code=404, detail="Vendor route not found")
        vendor = await session.get(VendorRecord, body.vendor_id) if body.vendor_id else None
        if vendor and vendor.management_company_id != principal.management_company_id:
            raise HTTPException(status_code=403, detail="Vendor is outside your company")
        record.incident_type = body.incident_type
        if vendor:
            record.vendor_id = vendor.id
            record.vendor_name = vendor.name
            record.contact_phone = body.contact_phone or vendor.phone
        elif body.vendor_name:
            record.vendor_name = body.vendor_name
            record.contact_phone = body.contact_phone
        record.dispatch_mode = body.dispatch_mode
        record.diagnostic_fee_cents = body.diagnostic_fee_cents
        record.automatic_approval_limit_cents = body.automatic_approval_limit_cents
        record.payer_responsibility = body.payer_responsibility
        await session.commit()
        return {"id": str(record.id)}

    @app.get("/api/v1/console/communities/{community_id}/knowledge")
    async def community_knowledge(
        community_id: UUID,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        assert_community_scope(principal, community_id)
        rows = await session.scalars(
            select(CommunityKnowledgeRecord)
            .where(
                CommunityKnowledgeRecord.community_id == community_id,
                CommunityKnowledgeRecord.management_company_id == principal.management_company_id,
            )
            .order_by(CommunityKnowledgeRecord.category, CommunityKnowledgeRecord.topic)
        )
        return [
            {
                "id": str(row.id),
                "category": row.category,
                "topic": row.topic,
                "answer": row.answer,
                "source_reference": row.source_reference,
                "approval_status": row.approval_status,
                "approved_by": row.approved_by,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    @app.post("/api/v1/console/communities/{community_id}/knowledge")
    async def upsert_community_knowledge(
        community_id: UUID,
        body: KnowledgeUpsertRequest,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        require_role(principal, "operations_admin", "community_manager")
        assert_community_scope(principal, community_id)
        record = await session.scalar(
            select(CommunityKnowledgeRecord).where(
                CommunityKnowledgeRecord.community_id == community_id,
                CommunityKnowledgeRecord.topic == body.topic,
            )
        )
        if not record:
            record = CommunityKnowledgeRecord(
                management_company_id=principal.management_company_id,
                community_id=community_id,
                category=body.category,
                topic=body.topic,
                answer=body.answer,
            )
            session.add(record)
            action = "created"
        else:
            record.category = body.category
            record.answer = body.answer
            action = "updated"
        record.source_reference = body.source_reference
        record.approval_status = body.approval_status
        record.is_active = body.approval_status == "approved"
        record.approved_by = principal.display_name if record.is_active else None
        record.approved_at = datetime.now(UTC) if record.is_active else None
        await session.flush()
        session.add(
            KnowledgeRevisionRecord(
                knowledge_id=record.id,
                actor_user_id=principal.user_id,
                action_type=action,
                snapshot=f"{body.category}: {body.topic}",
            )
        )
        await session.commit()
        return {"id": str(record.id), "approval_status": record.approval_status}

    @app.get("/api/v1/console/communities/{community_id}/knowledge/{knowledge_id}/revisions")
    async def knowledge_revisions(
        community_id: UUID,
        knowledge_id: UUID,
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> list[dict]:
        assert_community_scope(principal, community_id)
        record = await session.scalar(
            select(CommunityKnowledgeRecord).where(
                CommunityKnowledgeRecord.id == knowledge_id,
                CommunityKnowledgeRecord.community_id == community_id,
                CommunityKnowledgeRecord.management_company_id == principal.management_company_id,
            )
        )
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Knowledge item not found",
            )
        rows = await session.scalars(
            select(KnowledgeRevisionRecord)
            .where(KnowledgeRevisionRecord.knowledge_id == knowledge_id)
            .order_by(KnowledgeRevisionRecord.created_at.desc())
        )
        return [
            {
                "action": row.action_type,
                "snapshot": row.snapshot,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/v1/console/analytics")
    async def console_analytics(
        principal: Annotated[ConsolePrincipal, Depends(get_console_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> dict:
        cases = list(
            await session.scalars(
                select(WorkOrderRecord).where(
                    WorkOrderRecord.management_company_id == principal.management_company_id
                )
            )
        )
        total = len(cases)
        resolved = [row for row in cases if row.resolved_at]

        def average_seconds(values: list[float]) -> int | None:
            return int(sum(values) / len(values)) if values else None

        acknowledgement = [
            (row.acknowledged_at - row.created_at).total_seconds()
            for row in cases
            if row.acknowledged_at
        ]
        resolution = [
            (row.resolved_at - row.created_at).total_seconds()
            for row in resolved
            if row.resolved_at
        ]
        failures = (
            await session.scalar(
                select(func.count())
                .select_from(WorkflowEventRecord)
                .where(WorkflowEventRecord.event_type.endswith("_failed"))
            )
            or 0
        )
        return {
            "call_volume": await session.scalar(
                select(func.count())
                .select_from(ConversationRecord)
                .where(ConversationRecord.management_company_id == principal.management_company_id)
            )
            or 0,
            "emergency_rate": round(sum(row.priority == "emergency" for row in cases) / total, 2)
            if total
            else 0,
            "average_acknowledgement_seconds": average_seconds(acknowledgement),
            "average_resolution_seconds": average_seconds(resolution),
            "automation_failures": failures,
            "open_cases": sum(row.status != "resolved" for row in cases),
        }

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
                ConversationRecord.created_at
                >= datetime.now(UTC).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
            ),
            "open_work_orders": await count(WorkOrderRecord, WorkOrderRecord.status != "resolved"),
            "urgent_open_work_orders": await count(
                WorkOrderRecord,
                WorkOrderRecord.status != "resolved",
                WorkOrderRecord.priority.in_(["urgent", "emergency"]),
            ),
            "active_emergencies": await count(
                WorkOrderRecord,
                WorkOrderRecord.status != "resolved",
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
            select(ConversationRecord).order_by(ConversationRecord.created_at.desc()).limit(100)
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

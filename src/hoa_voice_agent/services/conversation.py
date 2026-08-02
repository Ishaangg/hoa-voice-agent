import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.protocols import (
    CallControlAdapter,
    CommunityManagementAdapter,
    MessagingAdapter,
)
from hoa_voice_agent.api_models import (
    ConversationTurnResponse,
    StartConversationResponse,
)
from hoa_voice_agent.domain.conversation_responder import ConversationResponder
from hoa_voice_agent.domain.emergency_policy import EmergencyPolicyEngine
from hoa_voice_agent.domain.incident_extractor import IncidentExtractor, RuleBasedIncidentExtractor
from hoa_voice_agent.domain.models import (
    ActionType,
    ConversationState,
    EmergencyDecision,
    IncidentReport,
    IncidentType,
    Intent,
    Priority,
    Resident,
    WorkflowAction,
    WorkOrder,
)
from hoa_voice_agent.persistence.repository import (
    CallerProfileRepository,
    CommunityKnowledgeRepository,
    ConversationRepository,
    ConversationTurnRepository,
    WorkflowEventRepository,
    WorkOrderRepository,
)


class ConversationNotFoundError(Exception):
    pass


class CommunityNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class VoiceTurnRoute:
    """Either a deterministic emergency response or the prompt for streamed dialogue."""

    system_prompt: str | None = None
    response: ConversationTurnResponse | None = None


class ConversationService:
    _maintenance_terms = {
        "broken",
        "damage",
        "fire",
        "flood",
        "gas",
        "heat",
        "hvac",
        "lift",
        "elevator",
        "leak",
        "maintenance",
        "repair",
        "smoke",
        "stuck",
        "trapped",
        "water",
        "locked out",
        "won't open",
        "does not work",
        "आग",
        "धुआं",
        "धुआँ",
        "गैस",
        "लीक",
        "पानी",
        "बाढ़",
        "बाढ़",
        "लिफ्ट",
        "फंसा",
        "फँसा",
        "मरम्मत",
    }
    _stop_words = {
        "a",
        "an",
        "are",
        "can",
        "do",
        "does",
        "for",
        "how",
        "i",
        "is",
        "me",
        "my",
        "of",
        "the",
        "what",
        "when",
        "where",
        "you",
    }

    def __init__(
        self,
        *,
        session: AsyncSession,
        community_management: CommunityManagementAdapter,
        messaging: MessagingAdapter,
        call_control: CallControlAdapter,
        vendor_notification_channel: str = "whatsapp",
        incident_extractor: IncidentExtractor | None = None,
        conversation_responder: ConversationResponder | None = None,
    ) -> None:
        self.conversations = ConversationRepository(session)
        self.session = session
        self.caller_profiles = CallerProfileRepository(session)
        self.community_knowledge = CommunityKnowledgeRepository(session)
        self.turns = ConversationTurnRepository(session)
        self.work_orders = WorkOrderRepository(session)
        self.workflow_events = WorkflowEventRepository(session)
        self.community_management = community_management
        self.messaging = messaging
        self.call_control = call_control
        self.vendor_notification_channel = vendor_notification_channel
        self.extractor = incident_extractor or RuleBasedIncidentExtractor()
        self.conversation_responder = conversation_responder
        self.policy = EmergencyPolicyEngine()

    async def start(
        self,
        caller_phone: str,
        community_id: UUID,
        *,
        language: str = "en",
    ) -> StartConversationResponse:
        community = await self.community_management.get_community(community_id)
        if community is None:
            raise CommunityNotFoundError

        resident = await self.community_management.find_resident(caller_phone)
        caller_profile = await self.caller_profiles.get_or_create(
            management_company_id=community.management_company_id,
            community_id=community.community_id,
            caller_phone=caller_phone,
        )
        has_claimed_context = bool(caller_profile.claimed_name and caller_profile.claimed_unit)
        conversation = await self.conversations.create(
            management_company_id=community.management_company_id,
            community_id=community.community_id,
            caller_phone=caller_phone,
            resident_id=resident.id if resident else None,
            caller_name=caller_profile.claimed_name,
            caller_unit=caller_profile.claimed_unit,
            state=(
                ConversationState.READY
                if resident or has_claimed_context
                else ConversationState.AWAITING_RESIDENT_DETAILS
            ),
        )
        if language == "hi":
            greeting = (
                f"नमस्ते {resident.first_name}, {community.name} को कॉल करने के लिए धन्यवाद। "
                "मैं आपकी कैसे मदद कर सकती हूँ?"
                if resident
                else (
                    f"वापस स्वागत है, {caller_profile.claimed_name}, यूनिट "
                    f"{caller_profile.claimed_unit}। मैं आपकी कैसे मदद कर सकती हूँ?"
                    if has_claimed_context
                    else (
                        f"{community.name} को कॉल करने के लिए धन्यवाद। सहायता करने से पहले, "
                        "कृपया अपना नाम और यूनिट नंबर बताएं।"
                    )
                )
            )
        else:
            greeting = (
                f"Hi {resident.first_name}, thank you for calling {community.name}. How can I help?"
                if resident
                else (
                    f"Welcome back, {caller_profile.claimed_name} in "
                    f"{caller_profile.claimed_unit}. "
                    "How can I help?"
                    if has_claimed_context
                    else (
                        f"Thank you for calling {community.name}. Before I help, please tell me "
                        "your name and unit number."
                    )
                )
            )
        await self.turns.add(conversation.id, "assistant", greeting)
        return StartConversationResponse(
            conversation_id=conversation.id,
            community_id=conversation.community_id,
            resident_id=conversation.resident_id,
            state=ConversationState(conversation.state),
            say=greeting,
        )

    async def handle_turn(
        self,
        conversation_id: UUID,
        text: str,
    ) -> ConversationTurnResponse:
        conversation, community = await self._load_conversation_context(conversation_id)
        await self.turns.add(conversation.id, "user", text)

        normalized = " ".join(text.lower().split())
        intake_response = await self._handle_missing_resident_details(
            conversation,
            text,
            normalized,
        )
        if intake_response:
            return intake_response
        profile_update_response = await self._handle_profile_update(conversation, text)
        if profile_update_response:
            return profile_update_response
        pending_response = await self._handle_pending_dispatch_confirmation(
            conversation, community, text
        )
        if pending_response:
            return pending_response
        if self._looks_like_maintenance(normalized):
            return await self._handle_maintenance(conversation, community, text)

        faqs = await self.community_knowledge.active_facts(community.community_id)
        if self.conversation_responder:
            answer = await self.conversation_responder.reply(
                text=text,
                community_name=community.name,
                faqs=faqs,
            )
            if answer:
                return ConversationTurnResponse(
                    conversation_id=conversation.id,
                    intent=Intent.CONVERSATION,
                    say=answer,
                )

        faq_answer = self._find_faq_answer(normalized, faqs)
        if faq_answer:
            return ConversationTurnResponse(
                conversation_id=conversation.id,
                intent=Intent.FAQ,
                say=faq_answer,
            )

        return ConversationTurnResponse(
            conversation_id=conversation.id,
            intent=Intent.UNKNOWN,
            say=(
                "I can help with community questions or report a maintenance issue. "
                "Which of those do you need?"
            ),
        )

    async def route_voice_turn(
        self,
        conversation_id: UUID,
        text: str,
        *,
        language: str = "en",
    ) -> VoiceTurnRoute:
        """Keep safety actions local; stream every ordinary turn through the LLM."""

        conversation, community = await self._load_conversation_context(conversation_id)
        await self.turns.add(conversation.id, "user", text)
        normalized = " ".join(text.lower().split())
        intake_response = await self._handle_missing_resident_details(
            conversation,
            text,
            normalized,
        )
        if intake_response:
            return VoiceTurnRoute(response=intake_response)
        profile_update_response = await self._handle_profile_update(conversation, text)
        if profile_update_response:
            return VoiceTurnRoute(response=profile_update_response)
        pending_response = await self._handle_pending_dispatch_confirmation(
            conversation, community, text, language=language
        )
        if pending_response:
            return VoiceTurnRoute(response=pending_response)
        if self._looks_like_maintenance(normalized):
            return VoiceTurnRoute(
                response=await self._handle_maintenance(
                    conversation, community, text, language=language
                )
            )
        faqs = await self.community_knowledge.active_facts(community.community_id)
        return VoiceTurnRoute(
            system_prompt=self._voice_system_prompt(community.name, faqs, language=language)
        )

    async def _load_conversation_context(self, conversation_id: UUID):
        conversation = await self.conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError

        community = await self.community_management.get_community(conversation.community_id)
        if community is None:
            raise CommunityNotFoundError
        return conversation, community

    async def _handle_missing_resident_details(
        self,
        conversation,
        text: str,
        normalized: str,
    ) -> ConversationTurnResponse | None:
        if conversation.state != ConversationState.AWAITING_RESIDENT_DETAILS.value:
            return None
        # Never delay an apparent life-safety report to collect caller metadata.
        if self._looks_like_immediate_emergency(normalized):
            return None

        name, unit = self._extract_caller_details(text)
        updated = await self.conversations.set_caller_details(conversation, name=name, unit=unit)
        await self.caller_profiles.set_claimed_details(
            await self.caller_profiles.get_or_create(
                management_company_id=updated.management_company_id,
                community_id=updated.community_id,
                caller_phone=updated.caller_phone,
            ),
            name=name,
            unit=unit,
        )
        if updated.caller_name and updated.caller_unit:
            say = f"Thanks, {updated.caller_name} in {updated.caller_unit}. How can I help today?"
        elif updated.caller_name:
            say = f"Thanks, {updated.caller_name}. What is your unit number?"
        elif updated.caller_unit:
            say = "Thanks. May I have your name as well?"
        else:
            say = "Please tell me your name and unit number, for example, Alex Smith in Unit 204."
        return ConversationTurnResponse(
            conversation_id=conversation.id,
            intent=Intent.CONVERSATION,
            say=say,
        )

    async def _handle_profile_update(
        self,
        conversation,
        text: str,
    ) -> ConversationTurnResponse | None:
        if conversation.pending_caller_name or conversation.pending_caller_unit:
            confirmation = self._normalized_confirmation(text)
            if confirmation in {
                "yes",
                "yes please",
                "yeah",
                "yep",
                "confirm",
                "correct",
                "that is correct",
                "please do",
            }:
                pending_name = conversation.pending_caller_name
                pending_unit = conversation.pending_caller_unit
                updated = await self.conversations.set_caller_details(
                    conversation,
                    name=pending_name,
                    unit=pending_unit,
                )
                profile = await self.caller_profiles.get_or_create(
                    management_company_id=updated.management_company_id,
                    community_id=updated.community_id,
                    caller_phone=updated.caller_phone,
                )
                await self.caller_profiles.set_claimed_details(
                    profile,
                    name=updated.caller_name,
                    unit=updated.caller_unit,
                )
                await self.conversations.clear_pending_caller_update(updated)
                changes = []
                if pending_name:
                    changes.append(f"your name to {updated.caller_name}")
                if pending_unit:
                    changes.append(f"your unit to {updated.caller_unit}")
                return ConversationTurnResponse(
                    conversation_id=conversation.id,
                    intent=Intent.CONVERSATION,
                    say=f"I've updated {' and '.join(changes)}. How can I help today?",
                )
            if confirmation in {"no", "no thanks", "nope", "cancel", "never mind"}:
                await self.conversations.clear_pending_caller_update(conversation)
                return ConversationTurnResponse(
                    conversation_id=conversation.id,
                    intent=Intent.CONVERSATION,
                    say="No changes were made. How can I help today?",
                )
            return ConversationTurnResponse(
                conversation_id=conversation.id,
                intent=Intent.CONVERSATION,
                say="Please say yes to confirm the update, or no to cancel it.",
            )

        name, unit = self._extract_profile_correction(text)
        if not name and not unit:
            return None
        await self.conversations.set_pending_caller_update(conversation, name=name, unit=unit)
        changes = []
        if name:
            changes.append(f"your name to {name}")
        if unit:
            changes.append(f"your unit to {unit}")
        return ConversationTurnResponse(
            conversation_id=conversation.id,
            intent=Intent.CONVERSATION,
            say=f"I heard {' and '.join(changes)}. Should I update your caller profile?",
        )

    async def _handle_pending_dispatch_confirmation(
        self,
        conversation,
        community,
        text: str,
        *,
        language: str = "en",
    ) -> ConversationTurnResponse | None:
        pending = await self.work_orders.get_pending_dispatch(conversation.id)
        if not pending:
            return None
        confirmation = self._normalized_confirmation(text)
        if self._is_negative(confirmation):
            await self.work_orders.delete_pending_dispatch(conversation.id)
            return ConversationTurnResponse(
                conversation_id=conversation.id,
                intent=Intent.MAINTENANCE,
                say=(
                    "ठीक है, मैंने कोई सेवा बुक नहीं की।"
                    if language == "hi"
                    else "Okay, I did not book the service call."
                ),
            )
        if not self._is_affirmative(confirmation):
            return ConversationTurnResponse(
                conversation_id=conversation.id,
                intent=Intent.MAINTENANCE,
                say=(
                    "कृपया हाँ या नहीं बताइए।"
                    if language == "hi"
                    else "Please say yes to book the service call, or no to cancel."
                ),
            )
        route = await self.work_orders.get_vendor_route_by_id(pending.route_id)
        await self.work_orders.delete_pending_dispatch(conversation.id)
        report = IncidentReport(
            incident_type=IncidentType(pending.incident_type),
            description=pending.description,
        )
        decision = self.policy.decide(report)
        return await self._handle_maintenance(
            conversation,
            community,
            pending.description,
            language=language,
            report_override=report,
            route_override=route,
            skip_consent=True,
            decision_override=decision,
            resident_consent="accepted",
        )

    @staticmethod
    def _is_affirmative(text: str) -> bool:
        if text in {
            "yes", "yes please", "yeah", "yep", "sure", "okay", "ok", "correct",
            "book it", "schedule it", "go ahead", "haan", "han", "हां", "हाँ",
        }:
            return True
        return text.startswith(("yes ", "yeah ", "yep ", "sure ", "okay ", "haan ", "हाँ "))

    @staticmethod
    def _is_negative(text: str) -> bool:
        if text in {"no", "no thanks", "nope", "cancel", "never mind", "not now", "nah"}:
            return True
        return text.startswith(("no ", "nope ", "cancel ", "never mind ", "not now "))

    async def _handle_maintenance(
        self,
        conversation,
        community,
        text: str,
        *,
        language: str = "en",
        report_override: IncidentReport | None = None,
        route_override=None,
        skip_consent: bool = False,
        decision_override: EmergencyDecision | None = None,
        resident_consent: str = "not_required",
    ) -> ConversationTurnResponse:
        report = report_override or await self.extractor.extract(text)
        decision = decision_override or self.policy.decide(report)
        vendor_route = route_override
        if decision.dispatch_vendor and vendor_route is None:
            vendor_probe = WorkOrder(
                conversation_id=conversation.id,
                management_company_id=conversation.management_company_id,
                community_id=conversation.community_id,
                resident_id=conversation.resident_id,
                incident_type=report.incident_type,
                priority=decision.priority,
                description=report.description,
            )
            vendor_route = await self.work_orders.find_vendor_route(vendor_probe)
        consent_required = bool(
            vendor_route
            and decision.dispatch_vendor
            and not skip_consent
            and decision.priority != Priority.EMERGENCY
            and (
                vendor_route.dispatch_mode == "inspection_only"
                or vendor_route.payer_responsibility != "association"
                or (
                    vendor_route.dispatch_mode == "auto_up_to_limit"
                    and vendor_route.automatic_approval_limit_cents is None
                )
            )
        )
        if consent_required:
            fee = vendor_route.diagnostic_fee_cents
            if fee is None:
                price_status = "unknown"
                disclosure = (
                    "The final repair cost is not available yet. I can schedule an inspection; "
                    "any repair beyond the visit requires your approval. "
                    "Would you like me to schedule it?"
                )
            else:
                price_status = "known"
                disclosure = (
                    f"The diagnostic visit costs ${fee / 100:.2f}. Any repair beyond "
                    "that requires approval. "
                    "Would you like me to schedule the visit?"
                )
            await self.work_orders.save_pending_dispatch(
                conversation_id=conversation.id,
                management_company_id=conversation.management_company_id,
                community_id=conversation.community_id,
                incident_type=report.incident_type.value,
                priority=decision.priority.value,
                description=report.description,
                route_id=vendor_route.id,
                price_status=price_status,
                price_disclosure=disclosure,
            )
            await self.workflow_events.add(
                conversation.id, "vendor_consent_requested", disclosure, None
            )
            return ConversationTurnResponse(
                conversation_id=conversation.id,
                intent=Intent.MAINTENANCE,
                say=disclosure,
                decision=decision,
            )
        work_order = WorkOrder(
            conversation_id=conversation.id,
            management_company_id=conversation.management_company_id,
            community_id=conversation.community_id,
            resident_id=conversation.resident_id,
            incident_type=report.incident_type,
            priority=decision.priority,
            description=report.description,
            price_status=(
                "known"
                if vendor_route and vendor_route.diagnostic_fee_cents is not None
                else "unknown"
            ),
            price_shown_to_caller=(
                f"${vendor_route.diagnostic_fee_cents / 100:.2f} diagnostic fee"
                if vendor_route and vendor_route.diagnostic_fee_cents is not None
                else None
            ),
            resident_consent=(
                "not_required" if decision.priority == Priority.EMERGENCY else resident_consent
            ),
            quote_status=(
                "pending"
                if vendor_route and vendor_route.dispatch_mode == "inspection_only"
                else "not_required"
            ),
            dispatch_route_id=vendor_route.id if vendor_route else None,
        )
        if vendor_route and decision.dispatch_vendor:
            work_order.assigned_to = vendor_route.vendor_name
            work_order.assignee_type = "vendor"
            work_order.status = "assigned"
        await self.community_management.create_work_order(work_order)
        await self.work_orders.create(work_order)
        if vendor_route and decision.dispatch_vendor:
            dispatch = await self.work_orders.create_vendor_dispatch(
                work_order_id=work_order.id,
                vendor_id=vendor_route.vendor_id,
                vendor_name=vendor_route.vendor_name,
                vendor_phone=vendor_route.contact_phone,
                decision="approved",
                reason=(
                    "Emergency mitigation" if decision.priority == Priority.EMERGENCY
                    else "Caller consented to the approved service call"
                ),
                notification_channel=self.vendor_notification_channel,
                status="pending",
            )
            if vendor_route.contact_phone:
                dispatch.attempt_count = 1
                try:
                    dispatch_message = (
                        f"HOA service request {work_order.id}: "
                        f"{work_order.incident_type.value} at {community.name}. "
                        f"{work_order.description} Reply ACCEPT, DECLINE, ON MY WAY, "
                        "QUOTE, or COMPLETED."
                    )
                    if self.vendor_notification_channel == "whatsapp":
                        try:
                            dispatch.provider_reference = await self.messaging.send_whatsapp(
                                vendor_route.contact_phone,
                                dispatch_message,
                                idempotency_key=f"vendor-dispatch:{work_order.id}",
                            )
                        except Exception as whatsapp_exc:
                            logger.warning(
                                "WhatsApp vendor notification failed for {}; "
                                "trying SMS fallback: {}",
                                work_order.id,
                                whatsapp_exc,
                            )
                            dispatch.provider_reference = await self.messaging.send_sms(
                                vendor_route.contact_phone,
                                dispatch_message,
                                idempotency_key=f"vendor-dispatch-sms-fallback:{work_order.id}",
                            )
                            dispatch.notification_channel = "sms_fallback"
                    else:
                        dispatch.provider_reference = await self.messaging.send_sms(
                            vendor_route.contact_phone,
                            dispatch_message,
                            idempotency_key=f"vendor-dispatch:{work_order.id}",
                        )
                    dispatch.status = "sent"
                    dispatch.sent_at = datetime.now(UTC)
                except Exception as exc:
                    dispatch.status = "failed"
                    dispatch.failure_detail = str(exc)
                    logger.error("Vendor notification failed for {}: {}", work_order.id, exc)
                await self.session.commit()
                await self.workflow_events.add(
                    conversation.id,
                    (
                        "vendor_notification_failed"
                        if dispatch.status == "failed"
                        else "vendor_notification_sent"
                    ),
                    dispatch.failure_detail or dispatch.provider_reference or dispatch.status,
                    work_order.id,
                )

        actions = [
            WorkflowAction(
                type=ActionType.WORK_ORDER_CREATED,
                detail=f"{decision.priority.value} work order created",
                reference_id=work_order.id,
            )
        ]
        await self.workflow_events.add(
            conversation.id,
            ActionType.WORK_ORDER_CREATED.value,
            f"{decision.priority.value} work order created",
            work_order.id,
        )
        if vendor_route:
            await self.workflow_events.add(
                conversation.id,
                "vendor_assigned",
                f"Automatically assigned to {vendor_route.vendor_name}",
                work_order.id,
            )

        sms_sent = False
        resident = await self.community_management.find_resident(conversation.caller_phone)
        try:
            sms_reference = await self.messaging.send_sms(
                conversation.caller_phone,
                self._build_confirmation_sms(
                    community_name=community.name,
                    resident=resident,
                    work_order=work_order,
                    transfer_to_on_call=decision.transfer_to_on_call,
                    caller_name=conversation.caller_name,
                    caller_unit=conversation.caller_unit,
                ),
                idempotency_key=f"sms:{work_order.id}",
            )
            sms_sent = True
            actions.append(
                WorkflowAction(
                    type=ActionType.SMS_QUEUED,
                    detail=sms_reference,
                    reference_id=work_order.id,
                )
            )
            await self.workflow_events.add(
                conversation.id,
                ActionType.SMS_QUEUED.value,
                sms_reference,
                work_order.id,
            )
        except Exception as exc:
            logger.error("Confirmation SMS failed for work order {}: {}", work_order.id, exc)
            actions.append(
                WorkflowAction(
                    type=ActionType.SMS_FAILED,
                    detail="confirmation SMS failed",
                    reference_id=work_order.id,
                )
            )
            await self.workflow_events.add(
                conversation.id,
                ActionType.SMS_FAILED.value,
                "confirmation SMS failed",
                work_order.id,
            )

        if decision.transfer_to_on_call:
            transfer_reference = await self.call_control.transfer_to_on_call(
                conversation.id,
                community.on_call_phone,
            )
            actions.append(
                WorkflowAction(
                    type=ActionType.TRANSFER_REQUESTED,
                    detail=transfer_reference,
                    reference_id=work_order.id,
                )
            )
            await self.workflow_events.add(
                conversation.id,
                ActionType.TRANSFER_REQUESTED.value,
                transfer_reference,
                work_order.id,
            )

        spoken_parts = self._localized_safety_instructions(decision.instructions, language)
        if (
            decision.priority == Priority.EMERGENCY
            and vendor_route
            and vendor_route.diagnostic_fee_cents is None
        ):
            spoken_parts.append(
                "The final repair price is not available yet. "
                "Emergency mitigation is being dispatched; "
                "any additional repair requires approval."
            )
        if sms_sent:
            spoken_parts.append(
                "मैंने कार्य अनुरोध बनाया है और पुष्टि संदेश भेज दिया है।"
                if language == "hi"
                else "I created a work order and sent a confirmation by text."
            )
        else:
            spoken_parts.append(
                "मैंने कार्य अनुरोध बनाया है, लेकिन पुष्टि संदेश नहीं भेज सकी।"
                if language == "hi"
                else "I created a work order, but I could not send the confirmation text."
            )
        if decision.transfer_to_on_call:
            spoken_parts.append(
                "मैं अब आपको ऑन-कॉल मैनेजर से जोड़ रही हूँ।"
                if language == "hi"
                else "I am connecting you to the on-call manager now."
            )

        return ConversationTurnResponse(
            conversation_id=conversation.id,
            intent=Intent.MAINTENANCE,
            say=" ".join(spoken_parts),
            actions=actions,
            decision=decision,
            work_order=work_order,
        )

    async def record_assistant_turn(self, conversation_id: UUID, text: str) -> None:
        await self.turns.add(conversation_id, "assistant", text)

    def _looks_like_maintenance(self, text: str) -> bool:
        return any(term in text for term in self._maintenance_terms)

    @staticmethod
    def _looks_like_immediate_emergency(text: str) -> bool:
        return any(
            term in text
            for term in (
                "fire",
                "smoke",
                "gas",
                "elevator",
                "lift",
                "stuck",
                "trapped",
                "flood",
                "water",
                "leak",
                "आग",
                "धुआं",
                "धुआँ",
                "गैस",
                "लीक",
                "पानी",
                "बाढ़",
                "बाढ़",
                "लिफ्ट",
                "फंसा",
                "फँसा",
            )
        )

    @staticmethod
    def _extract_caller_details(text: str) -> tuple[str | None, str | None]:
        name_match = re.search(
            r"\b(?:my name is|this is|i am|i'm|name is)\s+"
            r"([a-z][a-z '-]{1,70}?)(?=\s+(?:and\s+)?(?:my\s+)?(?:i am\s+)?"
            r"(?:in\s+)?(?:unit(?:\s+number)?|apt(?:artment)?|flat)\b"
            r"|\s+and\s+(?:my\s+)?number\b|[,.]|$)",
            text,
            flags=re.IGNORECASE,
        )
        unit_match = re.search(
            r"\b(?:my\s+)?(?:unit(?:\s+number)?|apt(?:artment)?|flat)\s*"
            r"(?:is|:)?\s*(?:(?:uh|um|er|erm)\b[\s,.]*)*"
            r"#?\s*([a-z0-9][a-z0-9 .-]{0,40})",
            text,
            flags=re.IGNORECASE,
        )
        name = name_match.group(1).strip(" .,-") if name_match else None
        unit = (
            f"Unit {normalized_unit}"
            if unit_match
            and (normalized_unit := ConversationService._normalize_spoken_unit(unit_match.group(1)))
            else None
        )
        return name, unit

    @staticmethod
    def _extract_profile_correction(text: str) -> tuple[str | None, str | None]:
        name_match = re.search(
            r"\b(?:change|update|correct|fix)\s+(?:my\s+)?name\s+(?:to|is)\s+"
            r"([a-z][a-z '-]{1,70}?)(?=[,.]|$)"
            r"|\b(?:please\s+)?call\s+me\s+([a-z][a-z '-]{1,70}?)(?=[,.]|$)"
            r"|\b(?:that(?:'s| is)\s+(?:not|wrong)|no),?\s*(?:my\s+)?name\s+is\s+"
            r"([a-z][a-z '-]{1,70}?)(?=[,.]|$)",
            text,
            flags=re.IGNORECASE,
        )
        unit_match = re.search(
            r"\b(?:change|update|correct|fix)\s+(?:my\s+)?"
            r"(?:unit\s+number|unit|apt(?:artment)?|flat)"
            r"(?:\s+(?:to|is)\s+|[,.]\s*(?:(?:it\s+)?is\s+)?|\s+(?:it\s+)?is\s+)"
            r"#?\s*([a-z0-9][a-z0-9 -]{0,40}?)(?=[,.]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if not unit_match:
            unit_match = re.search(
                r"\b(?:that(?:'s| is)\s+(?:not|wrong)|no)[\s,.-]*"
                r"(?:i\s+(?:live|stay|reside)\s+(?:in|at)|my\s+"
                r"(?:unit|apartment|apt|flat)\s+is)\s*"
                r"(?:unit|apartment|apt|flat)?\s*#?\s*([a-z0-9][a-z0-9 -]{0,40}?)(?=[,.]|$)"
                r"|\b(?:i\s+)?moved\s+(?:to|into)\s+"
                r"(?:unit|apartment|apt|flat)?\s*#?\s*([a-z0-9][a-z0-9 -]{0,40}?)(?=[,.]|$)",
                text,
                flags=re.IGNORECASE,
            )
        if not unit_match:
            unit_match = re.search(
                r"\b(?:my\s+)?(?:unit\s+number|unit|apartment|apt|flat)\s+"
                r"(?:is|number\s+is)\s*#?\s*([a-z0-9][a-z0-9 .-]{0,40})",
                text,
                flags=re.IGNORECASE,
            )
        name = next((group for group in name_match.groups() if group), None) if name_match else None
        if name:
            name = name.strip(" .,-")
        unit_value = (
            next((group for group in unit_match.groups() if group), None) if unit_match else None
        )
        normalized_unit = (
            ConversationService._normalize_spoken_unit(unit_value) if unit_value else None
        )
        unit = f"Unit {normalized_unit}" if normalized_unit else None
        return name, unit

    @staticmethod
    def _normalize_spoken_unit(value: str) -> str:
        digit_words = {
            "zero": "0",
            "one": "1",
            "two": "2",
            "three": "3",
            "four": "4",
            "five": "5",
            "six": "6",
            "seven": "7",
            "eight": "8",
            "nine": "9",
            "oh": "0",
            "o": "0",
        }
        words = re.findall(r"[a-z0-9]+", value.lower())
        words = [word for word in words if word not in {"uh", "um", "er", "erm"}]
        if not words:
            return ""
        if all(word in digit_words or word.isdigit() for word in words):
            return "".join(digit_words.get(word, word) for word in words)
        return value.strip(" .,-")

    @staticmethod
    def _normalized_confirmation(text: str) -> str:
        """Normalize STT punctuation without weakening the confirmation requirement."""

        return " ".join(re.findall(r"\w+", text.lower(), flags=re.UNICODE))

    @staticmethod
    def _voice_system_prompt(
        community_name: str,
        faqs: dict[str, str],
        *,
        language: str = "en",
    ) -> str:
        approved_facts = "\n".join(f"- {question}: {answer}" for question, answer in faqs.items())
        language_instruction = (
            "Reply in natural Hindi using Devanagari script. Translate approved facts faithfully; "
            "do not add information."
            if language == "hi"
            else "Reply in clear English."
        )
        return (
            f"You are the friendly voice concierge for {community_name}. "
            "Sound warm, calm, professional, and matter-of-fact; never flirtatious, intimate, "
            "seductive, or overly theatrical. "
            "Give the direct answer in the first sentence, ending it within 12 words. "
            "Use a second short sentence only when it materially helps. "
            "You may greet the resident and help with general conversation. For HOA facts, "
            "use only this approved information:\n"
            f"{approved_facts or '- No approved facts are available.'}\n"
            "Do not invent policies, hours, fees, phone numbers, or work-order status. "
            "If the information is unavailable, say you can help with a community question "
            "or a maintenance report. Do not give emergency instructions or promise dispatch. "
            f"{language_instruction}"
        )

    @staticmethod
    def _localized_safety_instructions(instructions: list[str], language: str) -> list[str]:
        if language != "hi":
            return list(instructions)
        translations = {
            "Leave the building if it is safe to do so.": (
                "अगर सुरक्षित हो, तो इमारत से बाहर निकल जाएँ।"
            ),
            "Call 911 from a safe location.": "सुरक्षित स्थान से 911 पर कॉल करें।",
            "Leave the area without operating switches or appliances.": (
                "स्विच या उपकरण चलाए बिना उस जगह से दूर हो जाएँ।"
            ),
            "Call 911 or the gas utility from a safe location.": (
                "सुरक्षित स्थान से 911 या गैस कंपनी को कॉल करें।"
            ),
            "Stay in the elevator and use its emergency call button.": (
                "लिफ्ट में रहें और उसका आपातकालीन कॉल बटन दबाएँ।"
            ),
            "Call 911 if anyone needs immediate medical help.": (
                "किसी को तुरंत चिकित्सीय मदद चाहिए तो 911 पर कॉल करें।"
            ),
            "Avoid standing water near electrical outlets or equipment.": (
                "बिजली के सॉकेट या उपकरणों के पास खड़े पानी से दूर रहें।"
            ),
            "If it is safe, move belongings away from the water.": (
                "अगर सुरक्षित हो, तो अपना सामान पानी से दूर कर दें।"
            ),
            "Stay in a safe, well-lit location while help is arranged.": (
                "मदद आने तक सुरक्षित और अच्छी रोशनी वाली जगह पर रहें।"
            ),
        }
        return [translations.get(instruction, instruction) for instruction in instructions]

    @staticmethod
    def _build_confirmation_sms(
        *,
        community_name: str,
        resident: Resident | None,
        work_order: WorkOrder,
        transfer_to_on_call: bool,
        caller_name: str | None = None,
        caller_unit: str | None = None,
    ) -> str:
        """Create a concise, useful confirmation without exposing unnecessary PII."""

        name = resident.first_name if resident else caller_name
        unit = resident.unit if resident else caller_unit
        resident_details = f"Hi {name}. " if name else ""
        if unit:
            resident_details += f"Your unit: {unit}. "
        incident = work_order.incident_type.value.replace("_", " ")
        description = " ".join(work_order.description.split())
        if len(description) > 180:
            description = f"{description[:177].rstrip()}..."
        ticket = str(work_order.id).split("-", 1)[0].upper()
        on_call = " The on-call manager is being notified." if transfer_to_on_call else ""
        return (
            f"{community_name}: {resident_details}We logged a {work_order.priority.value} "
            f"{incident} report. Issue: {description} Ticket: {ticket}.{on_call}"
        )

    def _find_faq_answer(self, text: str, faqs: dict[str, str]) -> str | None:
        for question, answer in faqs.items():
            if question in text:
                return answer

        query_tokens = self._meaningful_tokens(text)
        best_answer: str | None = None
        best_score = 0
        for question, answer in faqs.items():
            score = len(query_tokens & self._meaningful_tokens(question))
            if score > best_score:
                best_answer = answer
                best_score = score
        return best_answer if best_score >= 1 else None

    def _meaningful_tokens(self, text: str) -> set[str]:
        tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
        return tokens - self._stop_words

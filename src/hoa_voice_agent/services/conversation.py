import re
from dataclasses import dataclass
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
    Intent,
    Resident,
    WorkflowAction,
    WorkOrder,
)
from hoa_voice_agent.persistence.repository import (
    ConversationRepository,
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
        incident_extractor: IncidentExtractor | None = None,
        conversation_responder: ConversationResponder | None = None,
    ) -> None:
        self.conversations = ConversationRepository(session)
        self.work_orders = WorkOrderRepository(session)
        self.community_management = community_management
        self.messaging = messaging
        self.call_control = call_control
        self.extractor = incident_extractor or RuleBasedIncidentExtractor()
        self.conversation_responder = conversation_responder
        self.policy = EmergencyPolicyEngine()

    async def start(self, caller_phone: str, community_id: UUID) -> StartConversationResponse:
        community = await self.community_management.get_community(community_id)
        if community is None:
            raise CommunityNotFoundError

        resident = await self.community_management.find_resident(caller_phone)
        conversation = await self.conversations.create(
            management_company_id=community.management_company_id,
            community_id=community.community_id,
            caller_phone=caller_phone,
            resident_id=resident.id if resident else None,
            state=(
                ConversationState.READY
                if resident
                else ConversationState.AWAITING_RESIDENT_DETAILS
            ),
        )
        greeting = (
            f"Hi {resident.first_name}, thank you for calling {community.name}. "
            "How can I help?"
            if resident
            else (
                f"Thank you for calling {community.name}. Before I help, please tell me "
                "your name and unit number."
            )
        )
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

        normalized = " ".join(text.lower().split())
        intake_response = await self._handle_missing_resident_details(
            conversation,
            text,
            normalized,
        )
        if intake_response:
            return intake_response
        if self._looks_like_maintenance(normalized):
            return await self._handle_maintenance(conversation, community, text)

        if self.conversation_responder:
            answer = await self.conversation_responder.reply(
                text=text,
                community_name=community.name,
                faqs=community.faqs,
            )
            if answer:
                return ConversationTurnResponse(
                    conversation_id=conversation.id,
                    intent=Intent.CONVERSATION,
                    say=answer,
                )

        faq_answer = self._find_faq_answer(normalized, community.faqs)
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

    async def route_voice_turn(self, conversation_id: UUID, text: str) -> VoiceTurnRoute:
        """Keep safety actions local; stream every ordinary turn through the LLM."""

        conversation, community = await self._load_conversation_context(conversation_id)
        normalized = " ".join(text.lower().split())
        intake_response = await self._handle_missing_resident_details(
            conversation,
            text,
            normalized,
        )
        if intake_response:
            return VoiceTurnRoute(response=intake_response)
        if self._looks_like_maintenance(normalized):
            return VoiceTurnRoute(
                response=await self._handle_maintenance(conversation, community, text)
            )
        return VoiceTurnRoute(
            system_prompt=self._voice_system_prompt(community.name, community.faqs)
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
        if updated.caller_name and updated.caller_unit:
            say = (
                f"Thanks, {updated.caller_name} in {updated.caller_unit}. "
                "How can I help today?"
            )
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

    async def _handle_maintenance(
        self,
        conversation,
        community,
        text: str,
    ) -> ConversationTurnResponse:
        report = await self.extractor.extract(text)
        decision = self.policy.decide(report)
        work_order = WorkOrder(
            conversation_id=conversation.id,
            management_company_id=conversation.management_company_id,
            community_id=conversation.community_id,
            resident_id=conversation.resident_id,
            incident_type=report.incident_type,
            priority=decision.priority,
            description=report.description,
        )
        await self.community_management.create_work_order(work_order)
        await self.work_orders.create(work_order)

        actions = [
            WorkflowAction(
                type=ActionType.WORK_ORDER_CREATED,
                detail=f"{decision.priority.value} work order created",
                reference_id=work_order.id,
            )
        ]

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
        except Exception as exc:
            logger.error("Confirmation SMS failed for work order {}: {}", work_order.id, exc)
            actions.append(
                WorkflowAction(
                    type=ActionType.SMS_FAILED,
                    detail="confirmation SMS failed",
                    reference_id=work_order.id,
                )
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

        spoken_parts = list(decision.instructions)
        if sms_sent:
            spoken_parts.append("I created a work order and sent a confirmation by text.")
        else:
            spoken_parts.append(
                "I created a work order, but I could not send the confirmation text."
            )
        if decision.transfer_to_on_call:
            spoken_parts.append("I am connecting you to the on-call manager now.")

        return ConversationTurnResponse(
            conversation_id=conversation.id,
            intent=Intent.MAINTENANCE,
            say=" ".join(spoken_parts),
            actions=actions,
            decision=decision,
            work_order=work_order,
        )

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
            )
        )

    @staticmethod
    def _extract_caller_details(text: str) -> tuple[str | None, str | None]:
        name_match = re.search(
            r"\b(?:my name is|this is|i am|i'm|name is)\s+"
            r"([a-z][a-z '-]{1,70}?)(?=\s+(?:and\s+)?(?:my\s+)?(?:i am\s+)?"
            r"(?:in\s+)?(?:unit(?:\s+number)?|apt(?:artment)?|flat)\b|[,.]|$)",
            text,
            flags=re.IGNORECASE,
        )
        unit_match = re.search(
            r"\b(?:my\s+)?(?:unit(?:\s+number)?|apt(?:artment)?|flat)\s*"
            r"(?:is|:)?\s*#?\s*([a-z0-9][a-z0-9 -]{0,40})",
            text,
            flags=re.IGNORECASE,
        )
        name = name_match.group(1).strip(" .,-") if name_match else None
        unit = (
            f"Unit {ConversationService._normalize_spoken_unit(unit_match.group(1))}"
            if unit_match
            else None
        )
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
        }
        words = value.strip(" .,-").lower().split()
        if words and all(word in digit_words for word in words):
            return "".join(digit_words[word] for word in words)
        return value.strip(" .,-")

    @staticmethod
    def _voice_system_prompt(community_name: str, faqs: dict[str, str]) -> str:
        approved_facts = "\n".join(f"- {question}: {answer}" for question, answer in faqs.items())
        return (
            f"You are the friendly voice concierge for {community_name}. "
            "Give the direct answer in the first sentence, ending it within 12 words. "
            "Use a second short sentence only when it materially helps. "
            "You may greet the resident and help with general conversation. For HOA facts, "
            "use only this approved information:\n"
            f"{approved_facts or '- No approved facts are available.'}\n"
            "Do not invent policies, hours, fees, phone numbers, or work-order status. "
            "If the information is unavailable, say you can help with a community question "
            "or a maintenance report. Do not give emergency instructions or promise dispatch."
        )

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

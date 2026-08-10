"""Pure booking-state transitions and bounded PMS lookup plans.

The conversation service owns persistence and side effects.  This module owns
the booking meaning of a turn: which slots would change, whether the dates are
valid, whether an existing offer is stale, and which read-only lookup should
be planned.  Keeping this interface free of SQLAlchemy makes booking edge
cases executable as small replay tests.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

_GUEST_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "ek": 1,
    "do": 2,
    "teen": 3,
    "char": 4,
    "chaar": 4,
    "paanch": 5,
    "cheh": 6,
    "chhe": 6,
    "saat": 7,
    "aath": 8,
    "nau": 9,
    "das": 10,
}
_GUEST_COUNT_PATTERN = "|".join(sorted(_GUEST_NUMBER_WORDS, key=len, reverse=True)) + r"|\d+"
_GUEST_NOUN_PATTERN = (
    r"guests?|people|persons?|adults?|children?|kids?|members?|log(?:on)?|"
    r"गेस्ट|मेहमान|लोग"
)


GuestCountEvidenceStatus = Literal["explicit", "semantic", "unknown"]


@dataclass(frozen=True)
class GuestCountEvidence:
    """Application-owned assessment of a model's caller-text evidence."""

    guest_count: int | None
    evidence: str | None
    status: GuestCountEvidenceStatus


def _normalize_evidence_text(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def validate_guest_count_evidence(
    caller_text: str | None,
    *,
    guest_count: int | None,
    evidence: str | None,
) -> GuestCountEvidence:
    """Classify model count evidence without silently committing inference.

    The model may understand any language or phrasing.  The application owns
    the final commit.  Verbatim evidence is explicit.  A model-provided value
    with non-verbatim evidence is semantic and must go through the booking-fact
    confirmation lifecycle before it can become active state.
    """

    if guest_count is None or guest_count < 1 or not evidence:
        return GuestCountEvidence(None, evidence, "unknown")
    normalized_text = _normalize_evidence_text(caller_text)
    normalized_evidence = _normalize_evidence_text(evidence)
    if not normalized_text or not normalized_evidence:
        return GuestCountEvidence(None, evidence, "unknown")
    if normalized_evidence not in normalized_text:
        return GuestCountEvidence(guest_count, evidence, "semantic")
    return GuestCountEvidence(guest_count, evidence, "explicit")


def explicit_guest_count_from_text(text: str | None) -> int | None:
    """Return a party size only when the caller explicitly states one.

    This does not infer a count from a room type, a recommendation, or a
    question asking how many guests there are.
    """

    normalized = " ".join((text or "").casefold().split())
    if not normalized:
        return None
    if re.search(
        r"(?:how many|kitne|कितने)\b.{0,40}(?:guest|people|person|adult|member|"
        r"गेस्ट|मेहमान|लोग)",
        normalized,
    ):
        return None

    patterns = (
        rf"\b(?P<count>{_GUEST_COUNT_PATTERN})\s*(?:{_GUEST_NOUN_PATTERN})\b",
        rf"\b(?:{_GUEST_NOUN_PATTERN})\s*(?:will be|are|is|honge|hain|:)?\s*"
        rf"(?P<count>{_GUEST_COUNT_PATTERN})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        raw_count = match.group("count")
        count = int(raw_count) if raw_count.isdigit() else _GUEST_NUMBER_WORDS[raw_count]
        if count >= 1:
            return count
    return None


@dataclass(frozen=True)
class BookingSlotUpdate:
    """Booking values extracted from one caller turn."""

    check_in: date | None = None
    check_out: date | None = None
    guest_count: int | None = None
    guest_count_evidence: str | None = None
    guest_count_evidence_status: GuestCountEvidenceStatus = "unknown"
    nights: int | None = None

    @property
    def has_value(self) -> bool:
        return any((self.check_in, self.check_out, self.guest_count, self.nights))


@dataclass(frozen=True)
class BookingStateSnapshot:
    """The booking facts needed to calculate a turn transition."""

    check_in: date | None
    check_out: date | None
    guest_count: int | None
    pending_category: str | None
    pending_description: str | None
    has_offer: bool
    has_hold: bool
    nights: int | None = None


@dataclass(frozen=True)
class BookingStateTransition:
    """Pure result of applying one candidate booking turn."""

    active: bool = False
    invalid_dates: bool = False
    changed_slots: bool = False
    invalidate_offer: bool = False
    clear_previous_search: bool = False
    next_check_in: date | None = None
    next_check_out: date | None = None
    next_guest_count: int | None = None
    next_nights: int | None = None
    next_pending_category: str | None = None
    next_pending_description: str | None = None
    reason: str = "not_a_booking_slot_turn"


BookingToolPurpose = Literal["availability", "comparison"]


@dataclass(frozen=True)
class BookingToolPlan:
    """A validated, read-only PMS operation planned from owned state."""

    name: Literal["search_rooms"]
    arguments: dict[str, Any]
    purpose: BookingToolPurpose


BookingLifecycleAction = Literal[
    "none",
    "accept_offer",
    "confirm_hold",
    "hold_missing",
    "cancel_booking",
]


@dataclass(frozen=True)
class BookingLifecycleSnapshot:
    """Persisted booking facts required for lifecycle authorization."""

    pending_category: str | None
    state: str
    has_offer: bool
    has_hold: bool
    has_reservation: bool


@dataclass(frozen=True)
class BookingLifecycleDecision:
    """Pure authorization for one lifecycle action."""

    action: BookingLifecycleAction = "none"
    next_state: str | None = None
    reason: str = "not_applicable"


class HotelBookingFlow:
    """Pure booking transition and tool-planning interface."""

    @staticmethod
    def transition(
        state: BookingStateSnapshot,
        slots: BookingSlotUpdate,
        *,
        booking_intent: bool,
        caller_text: str,
        lookup_completed_this_turn: bool,
    ) -> BookingStateTransition:
        is_active = slots.has_value and (
            state.pending_category == "booking" or booking_intent
        )
        if not is_active:
            return BookingStateTransition()

        next_check_in = slots.check_in or state.check_in
        next_check_out = slots.check_out or state.check_out
        next_nights = slots.nights or state.nights
        if slots.check_out and next_check_in and next_check_out:
            if next_check_out <= next_check_in:
                return BookingStateTransition(
                    active=True,
                    invalid_dates=True,
                    next_check_in=next_check_in,
                    next_check_out=next_check_out,
                    next_guest_count=slots.guest_count or state.guest_count,
                    next_nights=next_nights,
                    reason="checkout_must_follow_checkin",
                )
            if slots.nights is None:
                next_nights = (next_check_out - next_check_in).days
        elif slots.nights and next_check_in:
            next_check_out = next_check_in + timedelta(days=next_nights)
        elif next_nights and next_check_in:
            next_check_out = next_check_in + timedelta(days=next_nights)
        if next_check_in and next_check_out and next_check_out <= next_check_in:
            return BookingStateTransition(
                active=True,
                invalid_dates=True,
                next_check_in=next_check_in,
                next_check_out=next_check_out,
                next_guest_count=slots.guest_count or state.guest_count,
                next_nights=next_nights,
                reason="checkout_must_follow_checkin",
            )

        next_guest_count = slots.guest_count or state.guest_count
        changed_slots = any(
            (
                next_check_in != state.check_in,
                next_check_out != state.check_out,
                next_guest_count != state.guest_count,
                next_nights != state.nights,
            )
        )
        previous = " ".join((state.pending_description or "").split())
        current = " ".join(caller_text.split())
        description = current if not previous else f"{previous} {current}"[-2000:]
        return BookingStateTransition(
            active=True,
            changed_slots=changed_slots,
            invalidate_offer=changed_slots,
            clear_previous_search=changed_slots and not lookup_completed_this_turn,
            next_check_in=next_check_in,
            next_check_out=next_check_out,
            next_guest_count=next_guest_count,
            next_nights=next_nights,
            next_pending_category="booking",
            next_pending_description=description,
            reason=(
                "booking_slots_changed"
                if changed_slots
                else "booking_slots_reconfirmed"
                if slots.has_value
                else "booking_slot_turn"
            ),
        )

    @staticmethod
    def availability_plan(
        *,
        check_in: date | None,
        check_out: date | None,
        guest_count: int | None,
        room_type: str | None = None,
        purpose: BookingToolPurpose = "availability",
    ) -> BookingToolPlan | None:
        """Plan one exact-date read-only lookup only when all inputs exist."""

        if not check_in or not check_out or not guest_count or check_out <= check_in:
            return None
        arguments: dict[str, Any] = {
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guest_count": guest_count,
        }
        if room_type:
            arguments["room_type"] = room_type
        return BookingToolPlan(
            name="search_rooms",
            arguments=arguments,
            purpose=purpose,
        )

    @staticmethod
    def lifecycle_snapshot(conversation: Any) -> BookingLifecycleSnapshot:
        """Adapt the persisted conversation record to the pure lifecycle model."""

        return BookingLifecycleSnapshot(
            pending_category=conversation.pending_category,
            state=conversation.state,
            has_offer=bool(conversation.booking_offer_id),
            has_hold=bool(conversation.booking_hold_reservation_id),
            has_reservation=bool(conversation.reservation_id),
        )

    @staticmethod
    def acceptance_decision(
        snapshot: BookingLifecycleSnapshot,
        *,
        affirmative: bool,
    ) -> BookingLifecycleDecision:
        """Authorize acceptance only for the active offer prompt."""

        if not affirmative or snapshot.pending_category != "booking":
            return BookingLifecycleDecision()
        if snapshot.state != "awaiting_booking_confirmation":
            return BookingLifecycleDecision(reason="no_active_offer_prompt")
        return BookingLifecycleDecision(
            action="accept_offer",
            next_state="awaiting_booking_details",
            reason=("active_offer_acceptance" if snapshot.has_offer else "legacy_hold_acceptance"),
        )

    @staticmethod
    def confirmation_decision(
        snapshot: BookingLifecycleSnapshot,
        *,
        caller_name_provided: bool,
    ) -> BookingLifecycleDecision:
        """Authorize confirmation only after an accepted hold and guest name."""

        if (
            snapshot.pending_category != "booking"
            or snapshot.state != "awaiting_booking_details"
            or not caller_name_provided
        ):
            return BookingLifecycleDecision()
        if snapshot.has_hold:
            return BookingLifecycleDecision(
                action="confirm_hold",
                next_state="awaiting_post_action_followup",
                reason="active_hold_and_guest_name",
            )
        return BookingLifecycleDecision(
            action="hold_missing",
            next_state="collecting_booking_allocation",
            reason="guest_name_without_active_hold",
        )

    @staticmethod
    def cancellation_decision(
        snapshot: BookingLifecycleSnapshot,
        *,
        requested: bool,
    ) -> BookingLifecycleDecision:
        """Authorize cancellation for any unfinished booking lifecycle state."""

        if not requested or snapshot.pending_category != "booking":
            return BookingLifecycleDecision()
        if snapshot.has_reservation:
            return BookingLifecycleDecision(reason="confirmed_reservation_requires_separate_flow")
        return BookingLifecycleDecision(
            action="cancel_booking",
            next_state="ready",
            reason=("cancel_active_hold" if snapshot.has_hold else "cancel_offer_or_draft"),
        )

    @staticmethod
    def offer_state(*, presented: bool) -> str:
        """Return the only valid conversation state after offer preparation."""

        return "awaiting_booking_confirmation" if presented else "collecting_booking_allocation"


__all__ = [
    "BookingSlotUpdate",
    "BookingStateSnapshot",
    "BookingStateTransition",
    "BookingToolPlan",
    "BookingLifecycleAction",
    "BookingLifecycleDecision",
    "BookingLifecycleSnapshot",
    "GuestCountEvidence",
    "GuestCountEvidenceStatus",
    "explicit_guest_count_from_text",
    "HotelBookingFlow",
    "validate_guest_count_evidence",
]

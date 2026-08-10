"""Conversation-scoped lifecycle for caller-provided booking facts.

The voice layer receives noisy, incremental proposals.  A proposal is not the
same thing as an active booking fact: a correction must be confirmed before it
can replace the value that downstream PMS operations use.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import uuid4

FactEvidenceStatus = Literal["explicit", "semantic", "legacy", "unknown"]
FactStatus = Literal[
    "active",
    "pending_confirmation",
    "superseded",
    "rejected",
    "ambiguous",
]
FactProposalOutcome = Literal[
    "accepted",
    "unchanged",
    "pending_confirmation",
    "ambiguous",
    "rejected",
    "ignored",
]


@dataclass
class BookingFact:
    """One caller-provided value and its provenance."""

    fact_id: str
    field: str
    value: Any
    evidence: str | None
    source_turn_id: str | None
    evidence_status: FactEvidenceStatus
    status: FactStatus
    revision: int
    supersedes_fact_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> BookingFact | None:
        if not isinstance(raw, dict):
            return None
        field = raw.get("field")
        value = raw.get("value")
        status = raw.get("status")
        evidence_status = raw.get("evidence_status", "unknown")
        if not isinstance(field, str) or not field or status not in {
            "active",
            "pending_confirmation",
            "superseded",
            "rejected",
            "ambiguous",
        }:
            return None
        if evidence_status not in {"explicit", "semantic", "legacy", "unknown"}:
            evidence_status = "unknown"
        try:
            revision = int(raw.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        return cls(
            fact_id=str(raw.get("fact_id") or uuid4()),
            field=field,
            value=value,
            evidence=raw.get("evidence") if isinstance(raw.get("evidence"), str) else None,
            source_turn_id=(
                raw.get("source_turn_id")
                if isinstance(raw.get("source_turn_id"), str)
                else None
            ),
            evidence_status=evidence_status,
            status=status,
            revision=revision,
            supersedes_fact_id=(
                raw.get("supersedes_fact_id")
                if isinstance(raw.get("supersedes_fact_id"), str)
                else None
            ),
        )


@dataclass(frozen=True)
class FactProposal:
    """Result of proposing or confirming one booking fact."""

    field: str
    outcome: FactProposalOutcome
    current_value: Any = None
    proposed_value: Any = None
    pending_value: Any = None


class BookingFactLedger:
    """Append-only fact history with an active projection and pending changes."""

    def __init__(self, facts: list[BookingFact] | None = None, *, revision: int = 0) -> None:
        self._facts = facts or []
        self.revision = max(0, revision)

    @classmethod
    def from_json(cls, raw: str | None) -> BookingFactLedger:
        if not raw:
            return cls()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls()
        if not isinstance(payload, dict):
            return cls()
        facts = [
            fact
            for item in payload.get("facts", [])
            if (fact := BookingFact.from_dict(item)) is not None
        ]
        try:
            revision = int(payload.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        return cls(facts, revision=revision)

    def to_json(self) -> str:
        return json.dumps(
            {
                "revision": self.revision,
                "facts": [fact.as_dict() for fact in self._facts],
            },
            sort_keys=True,
            default=str,
        )

    def active(self, field: str) -> BookingFact | None:
        return next(
            (
                fact
                for fact in reversed(self._facts)
                if fact.field == field and fact.status == "active"
            ),
            None,
        )

    def active_value(self, field: str) -> Any:
        fact = self.active(field)
        return fact.value if fact else None

    def pending(self, field: str) -> BookingFact | None:
        return next(
            (
                fact
                for fact in reversed(self._facts)
                if fact.field == field and fact.status == "pending_confirmation"
            ),
            None,
        )

    def pending_fields(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                fact.field for fact in self._facts if fact.status == "pending_confirmation"
            )
        )

    def has_pending(self) -> bool:
        return bool(self.pending_fields())

    def active_values(self) -> dict[str, Any]:
        fields = {fact.field for fact in self._facts}
        return {
            field: fact.value
            for field in fields
            if (fact := self.active(field)) is not None
        }

    def history(self, field: str | None = None) -> list[BookingFact]:
        return [
            fact
            for fact in self._facts
            if (field is None or fact.field == field)
            and fact.status != "pending_confirmation"
        ]

    def propose(
        self,
        field: str,
        value: Any,
        *,
        evidence: str | None,
        source_turn_id: str | None,
        evidence_status: FactEvidenceStatus,
    ) -> FactProposal:
        """Propose a value without silently replacing an active correction."""

        if not field or value is None:
            return FactProposal(field, "ignored")

        current = self.active(field)
        pending = self.pending(field)
        current_value = current.value if current else None

        if pending:
            if _same_value(pending.value, value):
                return FactProposal(
                    field,
                    "pending_confirmation",
                    current_value=current_value,
                    proposed_value=value,
                    pending_value=pending.value,
                )
            # A second value arrived before the first correction was confirmed.
            # Keep both the active value and the first pending correction intact;
            # choosing the latest STT fragment would be unsafe.
            return FactProposal(
                field,
                "ambiguous",
                current_value=current_value,
                proposed_value=value,
                pending_value=pending.value,
            )

        if current and _same_value(current.value, value):
            return FactProposal(field, "unchanged", current_value=current.value)

        self.revision += 1
        status: FactStatus = (
            "active"
            if current is None and evidence_status in {"explicit", "legacy"}
            else "pending_confirmation"
        )
        fact = BookingFact(
            fact_id=str(uuid4()),
            field=field,
            value=value,
            evidence=evidence,
            source_turn_id=source_turn_id,
            evidence_status=evidence_status,
            status=status,
            revision=self.revision,
            supersedes_fact_id=current.fact_id if current else None,
        )
        self._facts.append(fact)
        return FactProposal(
            field,
            "accepted" if status == "active" else "pending_confirmation",
            current_value=current_value,
            proposed_value=value,
            pending_value=value if status == "pending_confirmation" else None,
        )

    def confirm(self, field: str, *, accepted: bool) -> FactProposal:
        """Commit or reject the pending correction for one field."""

        pending = self.pending(field)
        current = self.active(field)
        if pending is None:
            return FactProposal(field, "ignored", current_value=current.value if current else None)

        self.revision += 1
        if not accepted:
            pending.status = "rejected"
            pending.revision = self.revision
            return FactProposal(
                field,
                "rejected",
                current_value=current.value if current else None,
                proposed_value=pending.value,
            )

        if current:
            current.status = "superseded"
            current.revision = self.revision
        pending.status = "active"
        pending.revision = self.revision
        return FactProposal(
            field,
            "accepted",
            current_value=current.value if current else None,
            proposed_value=pending.value,
        )


def _same_value(left: Any, right: Any) -> bool:
    return left == right


__all__ = [
    "BookingFact",
    "BookingFactLedger",
    "FactEvidenceStatus",
    "FactProposal",
]

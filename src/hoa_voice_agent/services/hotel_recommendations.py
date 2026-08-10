"""Requirement-aware room recommendation logic.

This module is deliberately independent of SQLAlchemy and the LLM. It ranks
verified catalogue entries plus dated availability and returns explainable
candidate plans that the booking coordinator can safely offer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from hoa_voice_agent.services.hotel_catalog import HotelCatalogSnapshot


@dataclass(frozen=True)
class BookingCriteria:
    required_features: tuple[str, ...] = ()
    preferred_features: tuple[str, ...] = ()
    excluded_features: tuple[str, ...] = ()
    max_nightly_rate_cents: int | None = None
    bed_type: str | None = None
    accessibility_required: bool | None = None
    evidence: dict[str, str] = field(default_factory=dict)
    revision: int = 0

    @classmethod
    def from_json(cls, raw: str | None) -> BookingCriteria:
        if not raw:
            return cls()
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls()
        if not isinstance(value, dict):
            return cls()
        return cls(
            required_features=cls._strings(value.get("required_features")),
            preferred_features=cls._strings(value.get("preferred_features")),
            excluded_features=cls._strings(value.get("excluded_features")),
            max_nightly_rate_cents=cls._optional_nonnegative_int(
                value.get("max_nightly_rate_cents")
            ),
            bed_type=cls._optional_string(value.get("bed_type")),
            accessibility_required=(
                value.get("accessibility_required")
                if isinstance(value.get("accessibility_required"), bool)
                else None
            ),
            evidence={
                str(key): str(item)
                for key, item in (value.get("evidence") or {}).items()
                if isinstance(key, str) and isinstance(item, str)
            },
            revision=cls._optional_nonnegative_int(value.get("revision")) or 0,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "required_features": list(self.required_features),
                "preferred_features": list(self.preferred_features),
                "excluded_features": list(self.excluded_features),
                "max_nightly_rate_cents": self.max_nightly_rate_cents,
                "bed_type": self.bed_type,
                "accessibility_required": self.accessibility_required,
                "evidence": self.evidence,
                "revision": self.revision,
            },
            sort_keys=True,
        )

    def is_empty(self) -> bool:
        return not any(
            (
                self.required_features,
                self.preferred_features,
                self.excluded_features,
                self.max_nightly_rate_cents is not None,
                self.bed_type,
                self.accessibility_required,
            )
        )

    def apply_patch(
        self,
        *,
        required_features: list[str] | tuple[str, ...] = (),
        preferred_features: list[str] | tuple[str, ...] = (),
        excluded_features: list[str] | tuple[str, ...] = (),
        removed_features: list[str] | tuple[str, ...] = (),
        max_nightly_rate_cents: int | None = None,
        clear_max_nightly_rate: bool = False,
        bed_type: str | None = None,
        accessibility_required: bool | None = None,
        evidence: dict[str, str] | None = None,
        known_features: set[str] | frozenset[str] | None = None,
    ) -> BookingCriteria:
        """Merge one model-produced patch after catalog-key validation."""

        allowed = {feature.casefold() for feature in known_features or ()}

        def clean(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
            output: list[str] = []
            for value in values:
                normalized = " ".join(str(value).casefold().split())
                if not normalized or (known_features is not None and normalized not in allowed):
                    continue
                if normalized not in output:
                    output.append(normalized)
            return tuple(output)

        required = set(self.required_features)
        preferred = set(self.preferred_features)
        excluded = set(self.excluded_features)
        required.update(clean(required_features))
        preferred.update(clean(preferred_features))
        excluded.update(clean(excluded_features))
        for feature in clean(removed_features):
            required.discard(feature)
            preferred.discard(feature)
            excluded.discard(feature)

        next_budget = None if clear_max_nightly_rate else self.max_nightly_rate_cents
        if max_nightly_rate_cents is not None and max_nightly_rate_cents >= 0:
            next_budget = max_nightly_rate_cents
        next_bed = (
            " ".join(bed_type.casefold().split())
            if isinstance(bed_type, str) and bed_type.strip()
            else self.bed_type
        )
        next_accessibility = (
            accessibility_required
            if isinstance(accessibility_required, bool)
            else self.accessibility_required
        )
        next_evidence = dict(self.evidence)
        if evidence:
            next_evidence.update(
                {key: value for key, value in evidence.items() if isinstance(value, str)}
            )
        candidate = BookingCriteria(
            required_features=tuple(sorted(required)),
            preferred_features=tuple(sorted(preferred)),
            excluded_features=tuple(sorted(excluded)),
            max_nightly_rate_cents=next_budget,
            bed_type=next_bed,
            accessibility_required=next_accessibility,
            evidence=next_evidence,
            revision=self.revision,
        )
        if candidate == self:
            return self
        return BookingCriteria(
            **{**candidate.__dict__, "revision": self.revision + 1}
        )

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(
            normalized
            for item in value
            if isinstance(item, str)
            for normalized in [" ".join(item.casefold().split())]
            if normalized
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return " ".join(value.casefold().split())
        return None

    @staticmethod
    def _optional_nonnegative_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None


@dataclass(frozen=True)
class RankedRecommendation:
    room_type: str
    room_count: int
    total_capacity: int
    nightly_total_cents: int
    room_lines: tuple[dict[str, Any], ...]
    matched_features: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allocation": [{"room_type": self.room_type, "quantity": self.room_count}],
            "room_lines": [dict(line) for line in self.room_lines],
            "room_count": self.room_count,
            "total_capacity": self.total_capacity,
            "nightly_total_cents": self.nightly_total_cents,
            "matched_features": list(self.matched_features),
            "reasons": list(self.reasons),
            "explanation": "; ".join(self.reasons),
        }


class RecommendationEngine:
    """Rank at most three single-room plans from verified dated inventory."""

    def rank(
        self,
        snapshot: HotelCatalogSnapshot,
        available_rooms: list[dict[str, Any]],
        *,
        guest_count: int,
        criteria: BookingCriteria,
        max_results: int = 3,
        telemetry: Any | None = None,
    ) -> list[dict[str, Any]]:
        started = time.perf_counter()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for room in available_rooms:
            room_type = room.get("room_type")
            if isinstance(room_type, str):
                grouped.setdefault(room_type.casefold(), []).append(room)

        ranked: list[tuple[tuple[Any, ...], RankedRecommendation]] = []
        for entry in snapshot.room_types:
            rooms = grouped.get(entry.key.casefold(), [])
            if not rooms:
                continue
            if entry.max_occupancy < guest_count:
                continue
            amenity_keys = entry.amenity_keys
            if any(feature not in amenity_keys for feature in criteria.required_features):
                continue
            if any(feature in amenity_keys for feature in criteria.excluded_features):
                continue
            if criteria.bed_type and entry.bed_type:
                if criteria.bed_type not in entry.bed_type.casefold():
                    continue
            elif criteria.bed_type:
                continue
            if criteria.accessibility_required and not entry.accessibility_notes:
                continue

            selected = min(
                rooms,
                key=lambda room: int(room.get("nightly_rate_cents") or 0),
            )
            nightly_rate = int(selected.get("nightly_rate_cents") or 0)
            if (
                criteria.max_nightly_rate_cents is not None
                and nightly_rate > criteria.max_nightly_rate_cents
            ):
                continue
            matched = tuple(
                feature for feature in criteria.preferred_features if feature in amenity_keys
            )
            names = {
                amenity.key: amenity.name
                for amenity in entry.amenities
            }
            reasons = [
                f"includes {names[feature]}"
                for feature in (*criteria.required_features, *matched)
                if feature in names
            ]
            if criteria.max_nightly_rate_cents is not None:
                reasons.append(
                    f"within your ${criteria.max_nightly_rate_cents / 100:.2f} nightly budget"
                )
            if not reasons:
                reasons.append("fits the requested party size")
            recommendation = RankedRecommendation(
                room_type=entry.key,
                room_count=1,
                total_capacity=entry.max_occupancy,
                nightly_total_cents=nightly_rate,
                room_lines=(
                    {
                        "room_id": selected.get("room_id"),
                        "room_number": selected.get("room_number"),
                        "room_type": entry.key,
                        "max_occupancy": entry.max_occupancy,
                        "guests": guest_count,
                        "nightly_rate_cents": nightly_rate,
                    },
                ),
                matched_features=matched,
                reasons=tuple(reasons),
            )
            score = (
                -len(matched),
                nightly_rate,
                entry.max_occupancy - guest_count,
                entry.sort_order,
                entry.key,
            )
            ranked.append((score, recommendation))

        ranked.sort(key=lambda item: item[0])
        if telemetry:
            telemetry.record(
                "recommendation_ms",
                (time.perf_counter() - started) * 1000,
                metadata={"candidate_count": len(ranked)},
            )
        return [recommendation.as_dict() for _, recommendation in ranked[:max_results]]


__all__ = ["BookingCriteria", "RankedRecommendation", "RecommendationEngine"]

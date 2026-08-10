"""Cached, verified room-catalogue facts for the hotel conversation.

The catalogue is a deep module: callers ask for one snapshot, while this
module hides the fixed set of database reads, legacy fallback, versioning,
normalisation, and process-local caching behind that seam.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.persistence.hotel_tables import (
    HotelAmenityRecord,
    HotelCatalogVersionRecord,
    HotelRatePlanRecord,
    HotelRoomRecord,
    HotelRoomTypeAmenityRecord,
    HotelRoomTypeRecord,
)


@dataclass(frozen=True)
class RoomAmenity:
    key: str
    name: str
    category: str
    description: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "description": self.description,
        }


@dataclass(frozen=True)
class RoomRatePlan:
    code: str
    name: str
    nightly_rate_cents: int
    included_meals: tuple[str, ...] = ()
    cancellation_policy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "nightly_rate_cents": self.nightly_rate_cents,
            "included_meals": list(self.included_meals),
            "cancellation_policy": self.cancellation_policy,
        }


@dataclass(frozen=True)
class RoomCatalogEntry:
    key: str
    display_name: str
    description: str | None
    bed_type: str | None
    max_occupancy: int
    accessibility_notes: str | None
    policies: str | None
    sort_order: int
    amenities: tuple[RoomAmenity, ...]
    rate_plans: tuple[RoomRatePlan, ...]

    @property
    def primary_rate_plan(self) -> RoomRatePlan | None:
        return min(
            self.rate_plans,
            key=lambda plan: plan.nightly_rate_cents,
            default=None,
        )

    @property
    def amenity_keys(self) -> frozenset[str]:
        return frozenset(amenity.key for amenity in self.amenities)

    def as_dict(self) -> dict[str, Any]:
        primary = self.primary_rate_plan
        return {
            "room_type": self.key,
            "display_name": self.display_name,
            "description": self.description,
            "bed_type": self.bed_type,
            "max_occupancy": self.max_occupancy,
            "accessibility_notes": self.accessibility_notes,
            "policies": self.policies,
            "sort_order": self.sort_order,
            "amenities": [amenity.as_dict() for amenity in self.amenities],
            "amenity_keys": sorted(self.amenity_keys),
            "rate_plans": [plan.as_dict() for plan in self.rate_plans],
            "nightly_rate_cents": primary.nightly_rate_cents if primary else None,
        }


@dataclass(frozen=True)
class HotelCatalogSnapshot:
    hotel_id: UUID
    version: int
    loaded_at: datetime
    room_types: tuple[RoomCatalogEntry, ...]

    @property
    def room_type_keys(self) -> tuple[str, ...]:
        return tuple(entry.key for entry in self.room_types)

    @property
    def feature_keys(self) -> frozenset[str]:
        return frozenset(
            amenity.key
            for entry in self.room_types
            for amenity in entry.amenities
        )

    def find_room_type(self, value: str | None) -> RoomCatalogEntry | None:
        if not value:
            return None
        normalized = " ".join(value.casefold().split())
        for entry in self.room_types:
            candidates = {
                entry.key.casefold(),
                entry.display_name.casefold(),
                f"{entry.key.casefold()} room",
                f"{entry.display_name.casefold()} room",
            }
            if normalized in candidates:
                return entry
        return None

    def resolve_feature_key(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = " ".join(value.casefold().split())
        for entry in self.room_types:
            for amenity in entry.amenities:
                if normalized in {amenity.key.casefold(), amenity.name.casefold()}:
                    return amenity.key
        return None

    def relevant(self, room_types: set[str] | None = None) -> list[dict[str, Any]]:
        selected = self.room_types
        if room_types is not None:
            selected = tuple(
                entry for entry in self.room_types if entry.key.casefold() in room_types
            )
        return [entry.as_dict() for entry in selected]


class CatalogSnapshotService:
    """Load one immutable hotel catalogue snapshot with a bounded local cache."""

    DEFAULT_TTL_SECONDS: ClassVar[float] = 600.0
    _cache: ClassVar[dict[UUID, tuple[float, HotelCatalogSnapshot]]] = {}

    def __init__(
        self,
        session: AsyncSession,
        hotel_id: UUID,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        telemetry: Any | None = None,
    ) -> None:
        self.session = session
        self.hotel_id = hotel_id
        self.ttl_seconds = ttl_seconds
        self.telemetry = telemetry

    @classmethod
    def invalidate(cls, hotel_id: UUID) -> None:
        cls._cache.pop(hotel_id, None)

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

    async def mark_changed(self) -> int:
        """Advance the durable catalogue version after a metadata write."""

        record = await self.session.scalar(
            select(HotelCatalogVersionRecord).where(
                HotelCatalogVersionRecord.hotel_id == self.hotel_id
            )
        )
        if record is None:
            record = HotelCatalogVersionRecord(hotel_id=self.hotel_id, version=1)
            self.session.add(record)
        else:
            record.version += 1
        await self.session.flush()
        self.invalidate(self.hotel_id)
        return record.version

    async def get(self, *, force_refresh: bool = False) -> HotelCatalogSnapshot:
        now = time.monotonic()
        cached = self._cache.get(self.hotel_id)
        if not force_refresh and cached and now - cached[0] < self.ttl_seconds:
            self._record("catalog_cache_hit")
            return cached[1]

        started = time.perf_counter()
        self._record("catalog_cache_miss")
        snapshot = await self._load_snapshot()
        self._cache[self.hotel_id] = (time.monotonic(), snapshot)
        self._record("catalog_snapshot_ms", (time.perf_counter() - started) * 1000)
        return snapshot

    async def _load_snapshot(self) -> HotelCatalogSnapshot:
        version_record = await self.session.scalar(
            select(HotelCatalogVersionRecord).where(
                HotelCatalogVersionRecord.hotel_id == self.hotel_id
            )
        )
        version = version_record.version if version_record else 1

        type_rows = list(
            await self.session.scalars(
                select(HotelRoomTypeRecord)
                .where(
                    HotelRoomTypeRecord.hotel_id == self.hotel_id,
                    HotelRoomTypeRecord.is_active.is_(True),
                )
                .order_by(HotelRoomTypeRecord.sort_order, HotelRoomTypeRecord.key)
            )
        )
        physical_rows = list(
            await self.session.scalars(
                select(HotelRoomRecord)
                .where(HotelRoomRecord.hotel_id == self.hotel_id)
                .order_by(HotelRoomRecord.room_type, HotelRoomRecord.room_number)
            )
        )

        # Existing local databases may predate the catalogue tables' seed
        # data. Their physical room rows remain a safe, metadata-free fallback.
        known_types = {row.key for row in type_rows}
        for room_type in sorted({row.room_type for row in physical_rows} - known_types):
            matching = [row for row in physical_rows if row.room_type == room_type]
            type_rows.append(
                HotelRoomTypeRecord(
                    hotel_id=self.hotel_id,
                    key=room_type,
                    display_name=room_type.title(),
                    description=None,
                    bed_type=None,
                    max_occupancy=max(row.max_occupancy for row in matching),
                    accessibility_notes=None,
                    policies=None,
                    sort_order=999,
                    is_active=True,
                )
            )

        amenity_rows = list(
            await self.session.scalars(
                select(HotelAmenityRecord).where(
                    HotelAmenityRecord.hotel_id == self.hotel_id,
                    HotelAmenityRecord.is_active.is_(True),
                )
            )
        )
        amenity_by_id = {row.id: row for row in amenity_rows}
        links = list(
            (
                await self.session.execute(
                    select(HotelRoomTypeAmenityRecord).where(
                        HotelRoomTypeAmenityRecord.room_type_id.in_(
                            [row.id for row in type_rows if row.id]
                        )
                    )
                )
            ).scalars()
        )
        amenity_ids_by_type: dict[UUID, list[UUID]] = {}
        for link in links:
            amenity_ids_by_type.setdefault(link.room_type_id, []).append(link.amenity_id)

        rate_rows = list(
            await self.session.scalars(
                select(HotelRatePlanRecord).where(
                    HotelRatePlanRecord.hotel_id == self.hotel_id,
                    HotelRatePlanRecord.is_active.is_(True),
                )
            )
        )
        rates_by_type: dict[UUID, list[RoomRatePlan]] = {}
        for row in rate_rows:
            try:
                meals = json.loads(row.included_meals_json or "[]")
            except json.JSONDecodeError:
                meals = []
            rates_by_type.setdefault(row.room_type_id, []).append(
                RoomRatePlan(
                    code=row.code,
                    name=row.name,
                    nightly_rate_cents=row.nightly_rate_cents,
                    included_meals=tuple(
                        str(meal) for meal in meals if isinstance(meal, str)
                    ),
                    cancellation_policy=row.cancellation_policy,
                )
            )

        entries: list[RoomCatalogEntry] = []
        for row in type_rows:
            amenities = tuple(
                RoomAmenity(
                    key=amenity_by_id[amenity_id].key,
                    name=amenity_by_id[amenity_id].name,
                    category=amenity_by_id[amenity_id].category,
                    description=amenity_by_id[amenity_id].description,
                )
                for amenity_id in amenity_ids_by_type.get(row.id, [])
                if amenity_id in amenity_by_id
            )
            rates = tuple(rates_by_type.get(row.id, ()))
            if not rates:
                fallback_rates = [
                    room.nightly_rate_cents
                    for room in physical_rows
                    if room.room_type.casefold() == row.key.casefold()
                ]
                if fallback_rates:
                    rates = (
                        RoomRatePlan(
                            code="default",
                            name="Standard rate",
                            nightly_rate_cents=min(fallback_rates),
                        ),
                    )
            entries.append(
                RoomCatalogEntry(
                    key=row.key,
                    display_name=row.display_name,
                    description=row.description,
                    bed_type=row.bed_type,
                    max_occupancy=row.max_occupancy,
                    accessibility_notes=row.accessibility_notes,
                    policies=row.policies,
                    sort_order=row.sort_order,
                    amenities=amenities,
                    rate_plans=rates,
                )
            )

        return HotelCatalogSnapshot(
            hotel_id=self.hotel_id,
            version=version,
            loaded_at=datetime.now(UTC),
            room_types=tuple(entries),
        )

    def _record(self, metric_name: str, value_ms: float | None = None) -> None:
        if self.telemetry:
            self.telemetry.record(metric_name, value_ms)


__all__ = [
    "CatalogSnapshotService",
    "HotelCatalogSnapshot",
    "RoomAmenity",
    "RoomCatalogEntry",
    "RoomRatePlan",
]

"""Fake PMS boundary used by the hotel MVP.

The interface mirrors the operations the voice agent needs, while keeping the
demo implementation backed by the hotel tables. A live PMS can replace this
adapter without changing the conversation or console layers.
"""

# ruff: noqa: E501

from datetime import UTC, date, datetime, timedelta
from itertools import product
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.persistence.hotel_tables import (
    HotelGuestRecord,
    HotelReservationRecord,
    HotelReservationRoomRecord,
    HotelRoomRecord,
)
from hoa_voice_agent.services.hotel_booking_plan import BookingPlan, RoomTypeRequest
from hoa_voice_agent.services.hotel_catalog import (
    CatalogSnapshotService,
    HotelCatalogSnapshot,
)
from hoa_voice_agent.services.hotel_recommendations import (
    BookingCriteria,
    RecommendationEngine,
)


class HotelPMSAdapter:
    HOLD_DURATION = timedelta(minutes=5)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """SQLite returns naive datetimes even for timezone-aware columns."""

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    def __init__(self, session: AsyncSession, hotel_id: UUID) -> None:
        self.session = session
        self.hotel_id = hotel_id

    async def lookup_active_stay(self, phone: str) -> dict | None:
        guest = await self.session.scalar(
            select(HotelGuestRecord).where(
                HotelGuestRecord.hotel_id == self.hotel_id,
                HotelGuestRecord.phone == phone,
            )
        )
        if not guest:
            return None
        reservation = await self.session.scalar(
            select(HotelReservationRecord).where(
                HotelReservationRecord.hotel_id == self.hotel_id,
                HotelReservationRecord.guest_id == guest.id,
                HotelReservationRecord.status.in_(["confirmed", "checked_in"]),
            )
        )
        if not reservation:
            return None
        room = (
            await self.session.get(HotelRoomRecord, reservation.room_id)
            if reservation.room_id
            else None
        )
        return {
            "guest_id": str(guest.id),
            "guest_name": guest.full_name,
            "reservation_id": str(reservation.id),
            "confirmation_code": reservation.confirmation_code,
            "room_number": room.room_number if room else None,
            "check_in": reservation.check_in.isoformat(),
            "check_out": reservation.check_out.isoformat(),
            "status": reservation.status,
        }

    async def search_availability(
        self,
        room_type: str | None = None,
        *,
        check_in: date | None = None,
        check_out: date | None = None,
    ) -> list[dict]:
        """List operational rooms, optionally filtered by a requested stay."""

        if (check_in is None) != (check_out is None):
            raise ValueError("Provide both check-in and check-out dates.")
        if check_in and check_out:
            if check_out <= check_in:
                raise ValueError("Check-out must be after check-in.")
            rows = await self._available_rooms_for_dates(
                check_in=check_in, check_out=check_out, room_type=room_type
            )
        else:
            query = select(HotelRoomRecord).where(
                HotelRoomRecord.hotel_id == self.hotel_id,
                HotelRoomRecord.status == "available",
            )
            if room_type:
                query = query.where(HotelRoomRecord.room_type == room_type)
            rows = list(await self.session.scalars(query.order_by(HotelRoomRecord.room_number)))
        return [
            {
                "room_id": str(row.id),
                "room_number": row.room_number,
                "room_type": row.room_type,
                "max_occupancy": row.max_occupancy,
                "nightly_rate_cents": row.nightly_rate_cents,
            }
            for row in rows
        ]

    async def search_booking_availability(
        self,
        *,
        check_in: date,
        check_out: date,
        guest_count: int,
        requested_room_type: str | None = None,
        catalog: HotelCatalogSnapshot | None = None,
        criteria: BookingCriteria | None = None,
        booking_plan: BookingPlan | None = None,
        telemetry=None,
    ) -> dict:
        """Return a dated, guest-fitting request result and explicit alternatives.

        A requested room type remains the caller's choice. Alternatives appear
        only when that exact type is sold out, so a caller never receives a
        silent room-type substitution.
        """

        catalog = catalog or await CatalogSnapshotService(self.session, self.hotel_id).get()
        catalog_room_types = list(catalog.room_type_keys)
        requested = self._canonical_room_type(
            requested_room_type,
            catalog_room_types,
        )
        available_rooms = await self.search_availability(
            check_in=check_in,
            check_out=check_out,
        )
        available_room_types = self._room_type_inventory(available_rooms)
        party_group_occupancies, allow_room_sharing = self._recommendation_constraints(
            booking_plan
        )
        fitting_rooms = [
            room
            for room in available_rooms
            if self._assign_guests_to_recommended_rooms(
                [room],
                guest_count=guest_count,
                party_group_occupancies=party_group_occupancies,
                allow_room_sharing=allow_room_sharing,
            )
            is not None
        ]
        date_available_requested_rooms = (
            [
                room
                for room in available_rooms
                if room["room_type"].casefold() == requested.casefold()
            ]
            if requested
            else []
        )
        requested_rooms = (
            [
                room
                for room in fitting_rooms
                if room["room_type"].casefold() == requested.casefold()
            ]
            if requested
            else fitting_rooms
        )
        requested_status = "not_requested"
        if requested:
            if not any(
                room_type.casefold() == requested.casefold()
                for room_type in catalog_room_types
            ):
                requested_status = "unknown_room_type"
            elif not date_available_requested_rooms:
                requested_status = "sold_out"
            elif not requested_rooms:
                requested_status = "does_not_fit_party"
            else:
                requested_status = "available"
        alternatives: list[dict] = []
        if requested_status in {"sold_out", "does_not_fit_party"}:
            by_room_type: dict[str, list[dict]] = {}
            for room in fitting_rooms:
                if room["room_type"].casefold() == requested.casefold():
                    continue
                by_room_type.setdefault(room["room_type"], []).append(room)
            alternatives = [
                {
                    "room_type": room_type,
                    "available_room_count": len(rooms),
                    "from_nightly_rate_cents": min(
                        room["nightly_rate_cents"] for room in rooms
                    ),
                    "max_occupancy": max(room["max_occupancy"] for room in rooms),
                }
                for room_type, rooms in sorted(by_room_type.items())
            ]
        multi_room_recommendations = (
            self._multi_room_recommendations(
                available_rooms,
                guest_count=guest_count,
                requested_room_type=requested,
                party_group_occupancies=party_group_occupancies,
                allow_room_sharing=allow_room_sharing,
            )
            if not fitting_rooms
            else []
        )
        result = {
            "requested_room_type": requested,
            "requested_room_type_status": requested_status,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guest_count": guest_count,
            "rooms": requested_rooms,
            "alternative_room_types": alternatives,
            "available_room_types": available_room_types,
            "multi_room_recommendations": multi_room_recommendations,
        }
        if criteria and not requested_room_type:
            result["recommendations"] = RecommendationEngine().rank(
                catalog,
                available_rooms,
                guest_count=guest_count,
                criteria=criteria,
                telemetry=telemetry,
            )
            if not result["recommendations"]:
                result["criteria_failure"] = (
                    "No available room matched all of the stated room requirements."
                )
        return result

    async def recommended_booking_layouts(
        self,
        *,
        check_in: date,
        check_out: date,
        guest_count: int,
        preferred_room_type: str | None = None,
        booking_plan: BookingPlan | None = None,
    ) -> dict:
        """Return non-reserving layouts that can actually fit a party.

        This is deliberately separate from a caller's selected plan.  It is
        used only after that plan cannot work, so the conversation can offer a
        checked alternative without treating it as a caller-selected change.
        """

        available_rooms = await self.search_availability(
            check_in=check_in,
            check_out=check_out,
        )
        by_room_type: dict[str, list[dict]] = {}
        for room in available_rooms:
            room_type = room["room_type"]
            by_room_type.setdefault(room_type, []).append(room)

        party_group_occupancies, allow_room_sharing = self._recommendation_constraints(
            booking_plan
        )

        candidates: list[dict] = []
        for room_type, rooms in by_room_type.items():
            room = sorted(rooms, key=lambda item: item["room_number"])[0]
            if room["max_occupancy"] < guest_count:
                continue
            room_lines = self._assign_guests_to_recommended_rooms(
                [room],
                guest_count=guest_count,
                party_group_occupancies=party_group_occupancies,
                allow_room_sharing=allow_room_sharing,
            )
            if room_lines is None:
                continue
            candidates.append(
                {
                    "allocation": [{"room_type": room_type, "quantity": 1}],
                    "room_lines": room_lines,
                    "room_count": 1,
                    "total_capacity": room["max_occupancy"],
                    "nightly_total_cents": room["nightly_rate_cents"],
                }
            )
        candidates.extend(
            self._multi_room_recommendations(
                available_rooms,
                guest_count=guest_count,
                requested_room_type=preferred_room_type,
                party_group_occupancies=party_group_occupancies,
                allow_room_sharing=allow_room_sharing,
            )
        )

        def rank(candidate: dict) -> tuple:
            allocation = candidate["allocation"]
            exact_type_match = bool(
                preferred_room_type
                and all(
                    item["room_type"].casefold() == preferred_room_type.casefold()
                    for item in allocation
                )
            )
            preference_rank = 0 if not preferred_room_type or exact_type_match else 1
            return (
                preference_rank,
                candidate["room_count"],
                candidate["nightly_total_cents"],
                candidate["total_capacity"] - guest_count,
                tuple((item["room_type"], item["quantity"]) for item in allocation),
            )

        return {
            "available_room_types": self._room_type_inventory(available_rooms),
            "recommendations": sorted(candidates, key=rank)[:3],
        }

    @staticmethod
    def _room_type_inventory(rooms: list[dict]) -> list[dict]:
        """Summarize date-valid inventory without hiding room types from the caller."""

        by_room_type: dict[str, list[dict]] = {}
        for room in rooms:
            room_type = room.get("room_type")
            if isinstance(room_type, str):
                by_room_type.setdefault(room_type, []).append(room)
        return [
            {
                "room_type": room_type,
                "available_room_count": len(group),
                "from_nightly_rate_cents": min(
                    room["nightly_rate_cents"] for room in group
                ),
                "max_occupancy": max(room["max_occupancy"] for room in group),
            }
            for room_type, group in sorted(by_room_type.items())
        ]

    @staticmethod
    def _multi_room_recommendations(
        rooms: list[dict],
        *,
        guest_count: int,
        requested_room_type: str | None,
        party_group_occupancies: list[int] | None = None,
        allow_room_sharing: bool | None = None,
    ) -> list[dict]:
        """Rank real multi-room layouts when no one room fits the whole party.

        The result is a proposal, not a booking.  It prefers a layout with the
        fewest rooms, then the lowest nightly total, then the least unused
        capacity.  A caller's stated room type is preferred when it can serve
        the party without mixing room types.
        """

        by_room_type: dict[str, list[dict]] = {}
        for room in rooms:
            room_type = room.get("room_type")
            if isinstance(room_type, str):
                by_room_type.setdefault(room_type, []).append(room)
        types = sorted(by_room_type)
        if not types:
            return []

        candidates: list[tuple[tuple, dict]] = []
        for quantities in product(*(range(len(by_room_type[room_type]) + 1) for room_type in types)):
            room_count = sum(quantities)
            if room_count < 2 or room_count > guest_count:
                continue
            selected_rooms = [
                room
                for room_type, quantity in zip(types, quantities, strict=True)
                for room in sorted(
                    by_room_type[room_type],
                    # Match search_booking_plan's dated room selection so
                    # ranking uses the same total that the shown offer will.
                    key=lambda item: item["room_number"],
                )[:quantity]
            ]
            total_capacity = sum(room["max_occupancy"] for room in selected_rooms)
            if total_capacity < guest_count:
                continue
            room_lines = HotelPMSAdapter._assign_guests_to_recommended_rooms(
                selected_rooms,
                guest_count=guest_count,
                party_group_occupancies=party_group_occupancies,
                allow_room_sharing=allow_room_sharing,
            )
            if room_lines is None:
                continue
            allocation = [
                {"room_type": room_type, "quantity": quantity}
                for room_type, quantity in zip(types, quantities, strict=True)
                if quantity
            ]
            nightly_total = sum(room["nightly_rate_cents"] for room in selected_rooms)
            exact_type_match = bool(
                requested_room_type
                and all(
                    item["room_type"].casefold() == requested_room_type.casefold()
                    for item in allocation
                )
            )
            preference_rank = 0 if not requested_room_type or exact_type_match else 1
            candidate = {
                "allocation": allocation,
                "room_lines": room_lines,
                "room_count": room_count,
                "total_capacity": total_capacity,
                "nightly_total_cents": nightly_total,
            }
            candidates.append(
                (
                    (
                        preference_rank,
                        room_count,
                        nightly_total,
                        total_capacity - guest_count,
                        tuple((item["room_type"], item["quantity"]) for item in allocation),
                    ),
                    candidate,
                )
            )
        return [candidate for _, candidate in sorted(candidates, key=lambda item: item[0])[:3]]

    @staticmethod
    def _assign_guests_to_recommended_rooms(
        rooms: list[dict],
        *,
        guest_count: int,
        party_group_occupancies: list[int] | None = None,
        allow_room_sharing: bool | None = None,
    ) -> list[dict] | None:
        """Create an explicit, human-reviewable guest split for a proposal."""

        ordered = sorted(
            rooms,
            key=lambda room: (
                -room["max_occupancy"],
                room["nightly_rate_cents"],
                room["room_type"],
                room["room_number"],
            ),
        )
        if party_group_occupancies:
            if any(group < 1 for group in party_group_occupancies):
                return None
            fixed_guest_count = sum(party_group_occupancies)
            flexible_guest_count = guest_count - fixed_guest_count
            if flexible_guest_count < 0:
                return None

            # A caller-described group (such as a couple) stays together. In
            # the default recommendation mode each described group gets its
            # own room; only an explicit ``shared`` choice permits groups to
            # be packed together.
            if allow_room_sharing is not True:
                remaining_rooms = list(ordered)
                fixed_lines: list[dict] = []
                for group_size in sorted(party_group_occupancies, reverse=True):
                    fitting = [
                        room
                        for room in remaining_rooms
                        if room["max_occupancy"] >= group_size
                    ]
                    if not fitting:
                        return None
                    room = min(
                        fitting,
                        key=lambda item: (
                            item["max_occupancy"] - group_size,
                            item["nightly_rate_cents"],
                            item["room_type"],
                            item["room_number"],
                        ),
                    )
                    remaining_rooms.remove(room)
                    fixed_lines.append(
                        {"room_type": room["room_type"], "guests": group_size}
                    )

                remaining = flexible_guest_count
                flexible_lines: list[dict] = []
                for index, room in enumerate(remaining_rooms):
                    remaining_rooms_after = len(remaining_rooms) - index - 1
                    guests = min(room["max_occupancy"], remaining - remaining_rooms_after)
                    if guests < 1:
                        return None
                    flexible_lines.append(
                        {"room_type": room["room_type"], "guests": guests}
                    )
                    remaining -= guests
                return fixed_lines + flexible_lines if remaining == 0 else None

        remaining = guest_count
        lines: list[dict] = []
        for index, room in enumerate(ordered):
            remaining_rooms = len(ordered) - index - 1
            guests = min(room["max_occupancy"], remaining - remaining_rooms)
            if guests < 1:
                return None
            lines.append({"room_type": room["room_type"], "guests": guests})
            remaining -= guests
        return lines if remaining == 0 else None

    @staticmethod
    def _recommendation_constraints(
        booking_plan: BookingPlan | None,
    ) -> tuple[list[int] | None, bool | None]:
        if not booking_plan or not booking_plan.party_groups:
            return None, None
        return booking_plan.occupancies, booking_plan.allow_room_sharing

    @staticmethod
    def _canonical_room_type(
        requested_room_type: str | None,
        catalog_room_types: list[str],
    ) -> str | None:
        """Resolve a caller phrase such as ``standard room`` to a PMS type."""

        if not requested_room_type:
            return None
        requested = " ".join(requested_room_type.split()).casefold()
        for room_type in catalog_room_types:
            normalized = room_type.casefold()
            if requested in {
                normalized,
                f"{normalized} room",
                f"{normalized} rooms",
                f"{normalized}s",
            }:
                return room_type
        return requested_room_type.strip()

    async def room_types(self) -> list[str]:
        """Return configured types, including a type with no current inventory.

        This is deliberately separate from availability: a caller asking for
        three standard rooms deserves a precise "not available" answer, not
        an invented claim that standard rooms do not exist.
        """

        snapshot = await CatalogSnapshotService(self.session, self.hotel_id).get()
        return list(snapshot.room_type_keys)

    async def _expire_stale_holds(self) -> None:
        """Release only timed-out holds; confirmed stays are never touched."""

        now = datetime.now(UTC)
        expired = await self.session.scalars(
            select(HotelReservationRecord).where(
                HotelReservationRecord.hotel_id == self.hotel_id,
                HotelReservationRecord.status == "held",
                or_(
                    HotelReservationRecord.hold_expires_at.is_(None),
                    HotelReservationRecord.hold_expires_at <= now,
                ),
            )
        )
        for reservation in expired:
            reservation.status = "expired"

    async def _reserved_room_ids(
        self,
        *,
        check_in: date,
        check_out: date,
        exclude_reservation_id: UUID | None = None,
    ) -> set[UUID]:
        """Find active dated allocations that overlap ``[check_in, check_out)``.

        The legacy ``room_id`` is included so records made before reservation
        lines were introduced still protect inventory correctly.
        """

        await self._expire_stale_holds()
        now = datetime.now(UTC)
        active = and_(
            HotelReservationRecord.hotel_id == self.hotel_id,
            HotelReservationRecord.status.in_(("held", "confirmed", "checked_in")),
            HotelReservationRecord.check_in < check_out,
            HotelReservationRecord.check_out > check_in,
            or_(
                HotelReservationRecord.status != "held",
                HotelReservationRecord.hold_expires_at > now,
            ),
        )
        if exclude_reservation_id:
            active = and_(active, HotelReservationRecord.id != exclude_reservation_id)
        legacy_ids = set(
            await self.session.scalars(
                select(HotelReservationRecord.room_id).where(
                    active, HotelReservationRecord.room_id.is_not(None)
                )
            )
        )
        line_ids = set(
            await self.session.scalars(
                select(HotelReservationRoomRecord.room_id)
                .join(
                    HotelReservationRecord,
                    HotelReservationRoomRecord.reservation_id == HotelReservationRecord.id,
                )
                .where(active)
            )
        )
        return {room_id for room_id in legacy_ids | line_ids if room_id is not None}

    async def _available_rooms_for_dates(
        self,
        *,
        check_in: date,
        check_out: date,
        room_type: str | None = None,
        exclude_reservation_id: UUID | None = None,
    ) -> list[HotelRoomRecord]:
        reserved_ids = await self._reserved_room_ids(
            check_in=check_in,
            check_out=check_out,
            exclude_reservation_id=exclude_reservation_id,
        )
        query = select(HotelRoomRecord).where(
            HotelRoomRecord.hotel_id == self.hotel_id,
            HotelRoomRecord.status == "available",
        )
        if room_type:
            query = query.where(HotelRoomRecord.room_type.ilike(room_type))
        if reserved_ids:
            query = query.where(HotelRoomRecord.id.not_in(reserved_ids))
        return list(await self.session.scalars(query.order_by(HotelRoomRecord.room_number)))

    @staticmethod
    def _assign_party_groups(
        rooms: list[HotelRoomRecord], plan: BookingPlan, guest_count: int
    ) -> tuple[list[tuple[HotelRoomRecord, int]] | None, str | None]:
        """Assign declared groups without silently splitting or moving them."""

        if not rooms:
            return None, "No rooms were selected."
        groups = sorted(plan.occupancies, reverse=True)
        if not groups:
            if len(rooms) == 1 and guest_count <= rooms[0].max_occupancy:
                return [(rooms[0], guest_count)], None
            return None, "Tell me how the party should be split across the requested rooms."
        if sum(groups) != guest_count:
            return None, "The party split does not match the total number of guests."

        ordered_rooms = sorted(rooms, key=lambda room: room.max_occupancy, reverse=True)
        assigned: dict[UUID, int] = {room.id: 0 for room in ordered_rooms}
        if plan.allow_room_sharing is False:
            if len(groups) != len(ordered_rooms):
                return None, "The separate-room request needs one stated party group for each room."
            for group, room in zip(groups, ordered_rooms, strict=True):
                if group > room.max_occupancy:
                    return None, "One of the stated couples or families does not fit in the requested room layout."
                assigned[room.id] = group
        else:
            # First-fit decreasing keeps each stated group together while
            # allowing groups that agreed to share to occupy one room.
            for group in groups:
                fitting = [
                    room
                    for room in ordered_rooms
                    if room.max_occupancy - assigned[room.id] >= group
                ]
                if not fitting:
                    return None, "One of the stated couples or families does not fit in the requested room layout."
                room = min(
                    fitting,
                    key=lambda candidate: candidate.max_occupancy - assigned[candidate.id] - group,
                )
                assigned[room.id] += group
            if any(assigned[room.id] == 0 for room in ordered_rooms):
                return None, "The requested room count leaves a room without an assigned guest group."
        return [(room, assigned[room.id]) for room in rooms], None

    async def search_booking_plan(
        self,
        plan: BookingPlan,
        guest_count: int,
        *,
        check_in: date | None = None,
        check_out: date | None = None,
        exclude_reservation_id: UUID | None = None,
    ) -> dict:
        """Price an exact layout against dated, active inventory records."""

        if not plan.has_exact_allocation:
            return {
                "status": "needs_allocation",
                "reason": "No exact room allocation was selected.",
            }
        if check_in is None or check_out is None:
            return {"status": "needs_dates", "reason": "A check-in and check-out date are required."}
        if check_out <= check_in:
            return {"status": "invalid_dates", "reason": "Check-out must be after check-in."}
        requested_count = plan.requested_room_count or 0
        if plan.occupancies and plan.allow_room_sharing is False and len(plan.occupancies) > requested_count:
            return {
                "status": "unavailable",
                "failure_code": "too_many_party_groups",
                "reason": "The requested room count cannot keep each stated party group together.",
            }

        requested_quantities: dict[str, tuple[str, int]] = {}
        for requested in plan.allocation:
            key = requested.room_type.casefold()
            canonical, quantity = requested_quantities.get(key, (requested.room_type, 0))
            requested_quantities[key] = (canonical, quantity + requested.quantity)

        chosen_rooms: list[HotelRoomRecord] = []
        for room_type, quantity in requested_quantities.values():
            rows = await self._available_rooms_for_dates(
                check_in=check_in,
                check_out=check_out,
                room_type=room_type,
                exclude_reservation_id=exclude_reservation_id,
            )
            if len(rows) < quantity:
                return {
                    "status": "unavailable",
                    "failure_code": "inventory_shortage",
                    "reason": (
                        f"Only {len(rows)} {room_type} "
                        f"room{' is' if len(rows) == 1 else 's are'} available in the demo inventory."
                    ),
                    "requested": {
                        "room_type": room_type,
                        "quantity": quantity,
                    },
                }
            chosen_rooms.extend(rows[:quantity])

        requested_total_capacity = sum(room.max_occupancy for room in chosen_rooms)
        if requested_total_capacity < guest_count:
            requested_types = {room.room_type.casefold() for room in chosen_rooms}
            preferred_room_type = (
                chosen_rooms[0].room_type if len(requested_types) == 1 else None
            )
            alternatives = await self.recommended_booking_layouts(
                check_in=check_in,
                check_out=check_out,
                guest_count=guest_count,
                preferred_room_type=preferred_room_type,
                booking_plan=plan,
            )
            return {
                "status": "unavailable",
                "failure_code": "insufficient_capacity",
                "reason": (
                    f"The requested layout can accommodate up to {requested_total_capacity} guests, "
                    f"but the stay has {guest_count}."
                ),
                "guest_count": guest_count,
                "requested_total_capacity": requested_total_capacity,
                "requested_allocation": [
                    {"room_type": room_type, "quantity": quantity}
                    for room_type, quantity in requested_quantities.values()
                ],
                "available_room_types": alternatives["available_room_types"],
                "alternative_recommendations": alternatives["recommendations"],
            }

        if plan.party_groups and plan.allow_room_sharing is None:
            return {
                "status": "unavailable",
                "failure_code": "party_split_required",
                "reason": "The caller has not said whether the stated groups may share rooms.",
                "guest_count": guest_count,
                "requested_total_capacity": requested_total_capacity,
            }

        if not plan.occupancies and len(chosen_rooms) > 1:
            return {
                "status": "unavailable",
                "failure_code": "party_split_required",
                "reason": "Tell me how the party should be split across the requested rooms.",
                "guest_count": guest_count,
                "requested_total_capacity": requested_total_capacity,
            }

        assigned, reason = self._assign_party_groups(chosen_rooms, plan, guest_count)
        if assigned is None:
            return {
                "status": "unavailable",
                "failure_code": "party_split_invalid",
                "reason": reason,
                "guest_count": guest_count,
                "requested_total_capacity": requested_total_capacity,
            }
        room_lines = []
        for room, assigned_guests in assigned:
            room_lines.append(
                {
                    "room_id": str(room.id),
                    "room_number": room.room_number,
                    "room_type": room.room_type,
                    "max_occupancy": room.max_occupancy,
                    "guests": assigned_guests,
                    "nightly_rate_cents": room.nightly_rate_cents,
                }
            )
        return {
            "status": "available",
            "rooms": room_lines,
            "nightly_total_cents": sum(room.nightly_rate_cents for room in chosen_rooms),
            "guest_count": guest_count,
            "plan_version": plan.version,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
        }

    async def create_booking_hold(
        self,
        plan: BookingPlan,
        guest_count: int,
        *,
        check_in: date,
        check_out: date,
        existing_hold_id: UUID | None = None,
    ) -> dict:
        """Create a short-lived dated allocation after a fresh availability check."""

        if existing_hold_id:
            await self.cancel_booking_hold(existing_hold_id, commit=False)
        offer = await self.search_booking_plan(
            plan, guest_count, check_in=check_in, check_out=check_out
        )
        if offer["status"] != "available":
            raise ValueError(offer["reason"])
        expires_at = datetime.now(UTC) + self.HOLD_DURATION
        reservation = HotelReservationRecord(
            hotel_id=self.hotel_id,
            room_id=UUID(offer["rooms"][0]["room_id"]),  # legacy console compatibility
            confirmation_code=f"EH-HOLD-{str(uuid4()).split('-')[0].upper()}",
            check_in=check_in,
            check_out=check_out,
            guest_count=guest_count,
            status="held",
            payment_status="pending_confirmation",
            hold_expires_at=expires_at,
            booking_plan_version=plan.version,
            nightly_total_cents=offer["nightly_total_cents"],
        )
        self.session.add(reservation)
        await self.session.flush()
        for line in offer["rooms"]:
            self.session.add(
                HotelReservationRoomRecord(
                    hotel_id=self.hotel_id,
                    reservation_id=reservation.id,
                    room_id=UUID(line["room_id"]),
                    room_type=line["room_type"],
                    guest_count=line["guests"],
                    nightly_rate_cents=line["nightly_rate_cents"],
                )
            )
        await self.session.commit()
        offer["hold_reservation_id"] = str(reservation.id)
        offer["hold_expires_at"] = expires_at.isoformat()
        return offer

    async def cancel_booking_hold(self, reservation_id: UUID, *, commit: bool = True) -> None:
        reservation = await self.session.get(HotelReservationRecord, reservation_id)
        if not reservation or reservation.hotel_id != self.hotel_id:
            return
        if reservation.status == "held":
            reservation.status = "cancelled"
        if commit:
            await self.session.commit()

    async def confirm_booking_hold(
        self, reservation_id: UUID, *, guest_name: str, phone: str
    ) -> dict:
        """Atomically turn a live hold into a confirmed multi-room stay."""

        await self._expire_stale_holds()
        reservation = await self.session.get(HotelReservationRecord, reservation_id)
        if not reservation or reservation.hotel_id != self.hotel_id:
            raise ValueError("That booking hold is no longer available.")
        if reservation.status == "expired":
            await self.session.commit()
            raise ValueError("That booking hold has expired.")
        if reservation.status != "held" or not reservation.hold_expires_at:
            raise ValueError("That booking hold cannot be confirmed.")
        if self._as_utc(reservation.hold_expires_at) <= datetime.now(UTC):
            reservation.status = "expired"
            await self.session.commit()
            raise ValueError("That booking hold has expired.")
        guest = await self.session.scalar(
            select(HotelGuestRecord).where(
                HotelGuestRecord.hotel_id == self.hotel_id,
                HotelGuestRecord.phone == phone,
            )
        )
        if not guest:
            guest = HotelGuestRecord(hotel_id=self.hotel_id, full_name=guest_name, phone=phone)
            self.session.add(guest)
            await self.session.flush()
        else:
            guest.full_name = guest_name
        reservation.guest_id = guest.id
        reservation.status = "confirmed"
        reservation.payment_status = "demo_confirmed"
        reservation.hold_expires_at = None
        lines = list(
            await self.session.scalars(
                select(HotelReservationRoomRecord)
                .where(HotelReservationRoomRecord.reservation_id == reservation.id)
                .order_by(HotelReservationRoomRecord.id)
            )
        )
        if not lines:
            raise ValueError("That booking hold has no room allocation.")
        await self.session.commit()
        return {
            "reservation_id": str(reservation.id),
            "confirmation_code": reservation.confirmation_code,
            "rooms": [
                {
                    "room_id": str(line.room_id),
                    "room_type": line.room_type,
                    "guest_count": line.guest_count,
                    "nightly_rate_cents": line.nightly_rate_cents,
                }
                for line in lines
            ],
            "nightly_total_cents": reservation.nightly_total_cents,
            "payment_status": reservation.payment_status,
        }

    async def get_room_details(
        self, *, room_id: str | None = None, room_type: str | None = None
    ) -> list[dict]:
        """Read room capacity, amenities metadata, and configured pricing."""

        snapshot = await CatalogSnapshotService(self.session, self.hotel_id).get()
        query = select(HotelRoomRecord).where(HotelRoomRecord.hotel_id == self.hotel_id)
        if room_id:
            query = query.where(HotelRoomRecord.id == UUID(room_id))
        elif room_type:
            query = query.where(HotelRoomRecord.room_type.ilike(room_type))
        else:
            return []
        rows = list(await self.session.scalars(query.order_by(HotelRoomRecord.room_number)))
        details: list[dict] = []
        for row in rows:
            entry = snapshot.find_room_type(row.room_type)
            primary = entry.primary_rate_plan if entry else None
            details.append(
                {
                    "room_id": str(row.id),
                    "room_number": row.room_number,
                    "room_type": row.room_type,
                    "display_name": entry.display_name if entry else row.room_type.title(),
                    "description": entry.description if entry else None,
                    "bed_type": entry.bed_type if entry else None,
                    "max_occupancy": entry.max_occupancy if entry else row.max_occupancy,
                    "nightly_rate_cents": row.nightly_rate_cents
                    if row.nightly_rate_cents is not None
                    else (primary.nightly_rate_cents if primary else 0),
                    "status": row.status,
                    "accessibility_notes": entry.accessibility_notes if entry else None,
                    "policies": entry.policies if entry else None,
                    "amenities": [amenity.as_dict() for amenity in entry.amenities]
                    if entry
                    else [],
                    "rate_plans": [plan.as_dict() for plan in entry.rate_plans]
                    if entry
                    else [],
                }
            )
        if not details and room_type:
            entry = snapshot.find_room_type(room_type)
            if entry:
                primary = entry.primary_rate_plan
                details.append(
                    {
                        "room_id": None,
                        "room_number": None,
                        "room_type": entry.key,
                        "display_name": entry.display_name,
                        "description": entry.description,
                        "bed_type": entry.bed_type,
                        "max_occupancy": entry.max_occupancy,
                        "nightly_rate_cents": primary.nightly_rate_cents if primary else 0,
                        "status": "configured",
                        "accessibility_notes": entry.accessibility_notes,
                        "policies": entry.policies,
                        "amenities": [amenity.as_dict() for amenity in entry.amenities],
                        "rate_plans": [plan.as_dict() for plan in entry.rate_plans],
                    }
                )
        return details

    async def create_demo_reservation(
        self,
        *,
        guest_name: str,
        phone: str,
        check_in: date,
        check_out: date,
        room_type: str = "standard",
        guest_count: int = 1,
    ) -> dict:
        if check_out <= check_in:
            raise ValueError("Check-out must be after check-in")
        plan = BookingPlan(allocation=[RoomTypeRequest(room_type=room_type, quantity=1)])
        hold = await self.create_booking_hold(
            plan, guest_count, check_in=check_in, check_out=check_out
        )
        confirmed = await self.confirm_booking_hold(
            UUID(hold["hold_reservation_id"]), guest_name=guest_name, phone=phone
        )
        room = await self.session.get(HotelRoomRecord, UUID(hold["rooms"][0]["room_id"]))
        return {
            **confirmed,
            "room_number": room.room_number if room else None,
        }

    async def cancel_demo_reservation(self, reservation_id: UUID) -> None:
        reservation = await self.session.get(HotelReservationRecord, reservation_id)
        if not reservation or reservation.hotel_id != self.hotel_id:
            raise ValueError("Reservation not found")
        reservation.status = "cancelled"
        await self.session.commit()

    async def modify_demo_reservation(
        self,
        reservation_id: UUID,
        *,
        check_in: date | None = None,
        check_out: date | None = None,
        room_type: str | None = None,
    ) -> dict:
        reservation = await self.session.get(HotelReservationRecord, reservation_id)
        if not reservation or reservation.hotel_id != self.hotel_id:
            raise ValueError("Reservation not found")
        if reservation.status not in {"confirmed", "checked_in"}:
            raise ValueError("Cancelled reservations cannot be modified")
        if check_in is not None:
            reservation.check_in = check_in
        if check_out is not None:
            reservation.check_out = check_out
        if reservation.check_out <= reservation.check_in:
            raise ValueError("Check-out must be after check-in")
        lines = list(
            await self.session.scalars(
                select(HotelReservationRoomRecord).where(
                    HotelReservationRoomRecord.reservation_id == reservation.id
                )
            )
        )
        if len(lines) > 1:
            raise ValueError("Multi-room reservations must be changed through a new room allocation.")
        current_room_id = lines[0].room_id if lines else reservation.room_id
        current_room = await self.session.get(HotelRoomRecord, current_room_id) if current_room_id else None
        if room_type and room_type != (current_room.room_type if current_room else None):
            rooms = await self._available_rooms_for_dates(
                check_in=reservation.check_in,
                check_out=reservation.check_out,
                room_type=room_type,
                exclude_reservation_id=reservation.id,
            )
            if not rooms:
                raise ValueError("No demo room is available for that room type")
            room = rooms[0]
            reservation.room_id = room.id
            if lines:
                lines[0].room_id = room.id
                lines[0].room_type = room.room_type
                lines[0].nightly_rate_cents = room.nightly_rate_cents
            else:
                self.session.add(
                    HotelReservationRoomRecord(
                        hotel_id=self.hotel_id,
                        reservation_id=reservation.id,
                        room_id=room.id,
                        room_type=room.room_type,
                        guest_count=reservation.guest_count,
                        nightly_rate_cents=room.nightly_rate_cents,
                    )
                )
        await self.session.commit()
        room = (
            await self.session.get(HotelRoomRecord, reservation.room_id)
            if reservation.room_id
            else None
        )
        return {
            "reservation_id": str(reservation.id),
            "confirmation_code": reservation.confirmation_code,
            "room_number": room.room_number if room else None,
            "check_in": reservation.check_in.isoformat(),
            "check_out": reservation.check_out.isoformat(),
            "status": reservation.status,
        }

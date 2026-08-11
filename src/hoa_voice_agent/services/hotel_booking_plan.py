"""Typed, customer-owned state for a multi-room hotel inquiry.

The voice layer may interpret natural language, but this module is the seam at
which booking intent becomes an inspectable plan.  It deliberately does not
know about database rows or a specific PMS provider.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

_NUMBER_WORDS = {
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
    "tin": 3,
    "char": 4,
    "chaar": 4,
    "paanch": 5,
    "panch": 5,
    "chhe": 6,
    "cheh": 6,
    "saat": 7,
    "aath": 8,
    "ath": 8,
    "nau": 9,
    "das": 10,
    "एक": 1,
    "दो": 2,
    "तीन": 3,
    "चार": 4,
    "पाँच": 5,
    "पांच": 5,
    "छह": 6,
    "छः": 6,
    "सात": 7,
    "आठ": 8,
    "नौ": 9,
    "दस": 10,
}
_NUMBER_PATTERN = "|".join(
    re.escape(value) for value in sorted(_NUMBER_WORDS, key=len, reverse=True)
) + r"|\d+"


def _number(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBER_WORDS[value]


@dataclass(frozen=True)
class PartyGroup:
    """People who have said they intend to stay together."""

    label: str
    people_per_group: int
    count: int = 1

    def room_occupancies(self) -> list[int]:
        return [self.people_per_group] * self.count


@dataclass(frozen=True)
class RoomTypeRequest:
    """A caller-selected room quantity; never a system recommendation."""

    room_type: str
    quantity: int


@dataclass
class BookingPlan:
    """The small interface used by the conversation and PMS modules.

    ``allocation`` is only populated by an explicit caller selection.  When
    ``allow_mixed_types`` is true, a caller has explicitly permitted the PMS
    to offer a mixture instead of a named type.
    """

    version: int = 0
    party_groups: list[PartyGroup] = field(default_factory=list)
    room_count: int | None = None
    allocation: list[RoomTypeRequest] = field(default_factory=list)
    allow_mixed_types: bool = False
    # None means the caller has not yet said whether party groups can share.
    # This is intentionally independent from accepting a mix of room types.
    allow_room_sharing: bool | None = None
    # A known empty list means the caller answered that there are no couples or
    # other fixed groups. It is different from not having asked yet.
    party_composition_known: bool = False

    @classmethod
    def from_json(cls, value: str | None) -> BookingPlan:
        if not value:
            return cls()
        try:
            raw = json.loads(value)
            return cls(
                version=int(raw.get("version", 0)),
                party_groups=[PartyGroup(**group) for group in raw.get("party_groups", [])],
                room_count=raw.get("room_count"),
                allocation=[RoomTypeRequest(**item) for item in raw.get("allocation", [])],
                allow_mixed_types=bool(raw.get("allow_mixed_types", False)),
                allow_room_sharing=raw.get("allow_room_sharing"),
                party_composition_known=bool(raw.get("party_composition_known", False)),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            # A corrupt prototype field must not cause a voice call to fail.
            return cls()

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "party_groups": [asdict(group) for group in self.party_groups],
                "room_count": self.room_count,
                "allocation": [asdict(item) for item in self.allocation],
                "allow_mixed_types": self.allow_mixed_types,
                "allow_room_sharing": self.allow_room_sharing,
                "party_composition_known": self.party_composition_known,
            },
            sort_keys=True,
        )

    @property
    def party_size(self) -> int | None:
        if not self.party_groups:
            return None
        return sum(group.people_per_group * group.count for group in self.party_groups)

    @property
    def occupancies(self) -> list[int]:
        return [size for group in self.party_groups for size in group.room_occupancies()]

    @property
    def requested_room_count(self) -> int | None:
        if self.allocation:
            return sum(item.quantity for item in self.allocation)
        return self.room_count

    @property
    def has_exact_allocation(self) -> bool:
        return bool(self.allocation)

    def apply_caller_turn(self, text: str, room_types: list[str]) -> bool:
        """Apply only explicit, customer-owned allocation information.

        Returns whether the plan changed.  A material update increments the
        version so any previous PMS quote can be treated as stale.
        """

        normalized = " ".join(text.casefold().split())
        changed = False
        party_groups = self._parse_party_groups(normalized)
        if party_groups and party_groups != self.party_groups:
            self.party_groups = party_groups
            changed = True
        if party_groups and not self.party_composition_known:
            self.party_composition_known = True
            changed = True
        if party_groups and self.allow_room_sharing is None:
            self.allow_room_sharing = False
            changed = True

        # "I need a room" starts a booking; it is not the caller choosing a
        # one-room allocation.  Only an explicit numeric/word count becomes
        # a room-count constraint here.
        generic_room_count = re.search(
            rf"\b(?:{_NUMBER_PATTERN})\s+rooms?\b", normalized
        )
        allocation = self._parse_allocation(normalized, room_types)
        if allocation:
            if allocation != self.allocation or self.allow_mixed_types:
                self.allocation = allocation
                self.room_count = sum(item.quantity for item in allocation)
                self.allow_mixed_types = False
                changed = True
        elif generic_room_count and not re.search(r"\b(?:room type|types)\b", normalized):
            count = _number(generic_room_count.group(0).split()[0])
            if count != self.room_count or self.allocation:
                self.room_count = count
                self.allocation = []
                changed = True

        if self.party_groups and re.search(r"\b(?:separate|their own)\s+rooms?\b", normalized):
            count = len(self.occupancies)
            if count != self.room_count:
                self.room_count = count
                changed = True
            if self.allow_room_sharing is not False:
                self.allow_room_sharing = False
                changed = True

        if re.search(r"\b(?:share|shared|together)\b", normalized):
            if self.allow_room_sharing is not True:
                self.allow_room_sharing = True
                changed = True

        allows_mix = bool(
            re.search(
                r"\b(?:any (?:room )?type|any available|whatever works|a mix|mixed)\b",
                normalized,
            )
        )
        if allows_mix and not self.allow_mixed_types:
            self.allow_mixed_types = True
            changed = True

        if changed:
            self.version += 1
        return changed

    def apply_model_update(
        self,
        *,
        party_groups: list[dict] | None = None,
        room_count: int | None = None,
        room_allocation: list[dict] | None = None,
        allocation_mode: str | None = None,
        party_composition_known: bool | None = None,
    ) -> bool:
        """Apply model-extracted meaning after lightweight validation.

        Natural language stays with the interpreter. This method validates
        only the domain contract: positive counts, non-empty labels/types,
        supported allocation modes, and no malformed provider-facing values.
        """

        changed = False
        parsed_groups: list[PartyGroup] = []
        if party_groups is not None:
            for raw in party_groups:
                if not isinstance(raw, dict):
                    continue
                label = str(raw.get("label") or raw.get("kind") or "group").strip()
                try:
                    people = int(raw.get("people_per_group") or raw.get("size"))
                    count = int(raw.get("count", 1))
                except (TypeError, ValueError):
                    continue
                if label and 1 <= people <= 50 and 1 <= count <= 50:
                    parsed_groups.append(PartyGroup(label, people, count))
        if party_groups is not None and parsed_groups != self.party_groups:
            self.party_groups = parsed_groups
            changed = True
        if (
            party_composition_known is not None
            and party_composition_known != self.party_composition_known
        ):
            self.party_composition_known = party_composition_known
            changed = True
        elif party_groups is not None and parsed_groups and not self.party_composition_known:
            self.party_composition_known = True
            changed = True

        # Once the caller has described couples/groups, recommendation mode
        # keeps those groups together by default. Explicit ``shared`` below
        # still overrides this when the caller says groups may share.
        mode = (allocation_mode or "").casefold()
        if (
            party_composition_known is True
            and parsed_groups
            and mode not in {"shared", "mixed"}
            and self.allow_room_sharing is not False
        ):
            self.allow_room_sharing = False
            changed = True

        if room_count is not None:
            try:
                parsed_room_count = int(room_count)
            except (TypeError, ValueError):
                return False
            if not 1 <= parsed_room_count <= 100:
                return False
            if self.room_count != parsed_room_count or self.allocation:
                self.room_count = parsed_room_count
                self.allocation = []
                self.allow_mixed_types = False
                changed = True

        parsed_allocation: list[RoomTypeRequest] = []
        if room_allocation:
            for raw in room_allocation:
                if not isinstance(raw, dict):
                    continue
                room_type = str(raw.get("room_type") or raw.get("type") or "").strip()
                try:
                    quantity = int(raw.get("quantity") or raw.get("count"))
                except (TypeError, ValueError):
                    continue
                if room_type and 1 <= quantity <= 100:
                    parsed_allocation.append(RoomTypeRequest(room_type, quantity))
        if parsed_allocation and parsed_allocation != self.allocation:
            self.allocation = parsed_allocation
            self.room_count = sum(item.quantity for item in parsed_allocation)
            self.allow_mixed_types = False
            changed = True

        if mode == "mixed" and not self.allow_mixed_types:
            self.allow_mixed_types = True
            changed = True
        if mode == "shared" and self.allow_room_sharing is not True:
            self.allow_room_sharing = True
            changed = True
        if mode == "separate" and self.party_groups:
            count = len(self.occupancies)
            if self.room_count != count:
                self.room_count = count
                changed = True
            if self.allow_room_sharing is not False:
                self.allow_room_sharing = False
                changed = True
        if changed:
            self.version += 1
        return changed

    @staticmethod
    def _parse_party_groups(normalized: str) -> list[PartyGroup]:
        groups: list[PartyGroup] = []
        family_pattern = re.compile(rf"\b(?:a\s+)?family\s+of\s+(?P<size>{_NUMBER_PATTERN})\b")
        couple_pattern = re.compile(
            rf"\b(?P<count>{_NUMBER_PATTERN})\s+couples?\b|\b(?:a|an)\s+couple\b"
        )
        single_pattern = re.compile(
            rf"\b(?P<count>{_NUMBER_PATTERN})\s+singles?\b|\b(?:a|an)\s+single\b"
        )
        for match in family_pattern.finditer(normalized):
            groups.append(PartyGroup("family", _number(match.group("size"))))
        for match in couple_pattern.finditer(normalized):
            count = match.groupdict().get("count") or "one"
            groups.append(PartyGroup("couple", 2, _number(count)))
        for match in single_pattern.finditer(normalized):
            count = match.groupdict().get("count") or "one"
            groups.append(PartyGroup("single", 1, _number(count)))
        return groups

    @staticmethod
    def _parse_allocation(normalized: str, room_types: list[str]) -> list[RoomTypeRequest]:
        if not room_types:
            return []
        aliases = sorted({item.casefold() for item in room_types}, key=len, reverse=True)
        type_pattern = "|".join(re.escape(item) for item in aliases)
        matches = re.finditer(
            rf"\b(?P<count>{_NUMBER_PATTERN})\s+(?P<room_type>{type_pattern})\s+rooms?\b",
            normalized,
        )
        requested: list[RoomTypeRequest] = []
        for match in matches:
            room_type = match.group("room_type")
            canonical = next(item for item in room_types if item.casefold() == room_type)
            requested.append(RoomTypeRequest(canonical, _number(match.group("count"))))
        return requested


def composition_prompt(guest_count: int) -> str:
    return (
        f"I have {guest_count} guests noted. Are any of them couples? "
        "If yes, how many?"
    )


def room_split_prompt(plan: BookingPlan) -> str:
    labels = []
    for group in plan.party_groups:
        labels.append(f"{group.count} {group.label}" + ("s" if group.count != 1 else ""))
    party_description = ", ".join(labels)
    return (
        f"Thanks — I have {party_description}. Should those groups have separate rooms, "
        "or are some of them happy to share?"
    )


def room_type_prompt(plan: BookingPlan) -> str:
    room_count = plan.requested_room_count or len(plan.occupancies)
    return (
        f"Got it. I’ll plan for {room_count} rooms. Do you want a particular room type, "
        "or may I check an available mix?"
    )

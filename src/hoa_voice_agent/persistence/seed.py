# ruff: noqa: E501
import json
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.fake import (
    COMMUNITY_ID,
    LEGACY_REMOVED_COMMUNITY_ID,
    MANAGEMENT_COMPANY_ID,
)
from hoa_voice_agent.persistence.hotel_tables import (
    HotelAmenityRecord,
    HotelCatalogVersionRecord,
    HotelDepartmentRecord,
    HotelGuestRecord,
    HotelKnowledgeRecord,
    HotelMenuItemRecord,
    HotelOrganizationRecord,
    HotelRatePlanRecord,
    HotelRecord,
    HotelReservationRecord,
    HotelReservationRoomRecord,
    HotelRoomRecord,
    HotelRoomTypeAmenityRecord,
    HotelRoomTypeRecord,
    HotelStaffMembershipRecord,
    HotelStaffUserRecord,
)
from hoa_voice_agent.persistence.repository import CommunityKnowledgeRepository
from hoa_voice_agent.persistence.tables import (
    CommunityRecord,
    ManagementCompanyRecord,
    RoutingContactRecord,
    StaffMembershipRecord,
    StaffUserRecord,
    VendorRecord,
    VendorRouteRecord,
)
from hoa_voice_agent.services.operations_console import hash_password

HOTEL_ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000001")
HOTEL_ID = UUID("40000000-0000-0000-0000-000000000001")
DEMO_SOLD_OUT_CHECK_IN = date(2026, 9, 17)
DEMO_SOLD_OUT_CHECK_OUT = date(2026, 9, 19)

DEMO_COMMUNITY_FACTS: tuple[tuple[str, str, str], ...] = (
    (
        "office",
        "office hours",
        "The management office is open Monday through Friday, 9 AM to 5 PM.",
    ),
    ("office", "office phone", "The management office phone number is +1 555-010-2000."),
    ("amenity", "pool hours", "The pool is open daily from 8 AM to 9 PM."),
    ("amenity", "gym hours", "The fitness room is open daily from 5 AM to 10 PM."),
    (
        "parking",
        "guest parking",
        "Guest vehicles must use marked guest spaces and display a guest pass.",
    ),
    (
        "parking",
        "towing",
        "Vehicles parked in fire lanes, reserved spaces, or unmarked areas may be towed.",
    ),
    ("operations", "trash pickup", "Trash is collected Tuesday and Friday mornings."),
    (
        "emergency",
        "after hours maintenance",
        "For an active flood, gas smell, fire or smoke, elevator entrapment, or unsafe access, "
        "the agent creates an emergency work order and contacts the on-call manager.",
    ),
    (
        "routing",
        "water leak routing",
        "Active leaks are handled as emergency maintenance; stopped leaks are logged as urgent.",
    ),
    (
        "routing",
        "hvac routing",
        "Routine HVAC issues are logged for the maintenance team during service hours.",
    ),
)


async def seed_demo_community_knowledge(session: AsyncSession) -> None:
    await CommunityKnowledgeRepository(session).seed_if_empty(
        management_company_id=MANAGEMENT_COMPANY_ID,
        community_id=COMMUNITY_ID,
        facts=DEMO_COMMUNITY_FACTS,
    )


async def seed_demo_operations_console(session: AsyncSession) -> None:
    """Seed one credible management-company pilot tenant without overwriting user data."""

    company = await session.get(ManagementCompanyRecord, MANAGEMENT_COMPANY_ID)
    if not company:
        session.add(
            ManagementCompanyRecord(
                id=MANAGEMENT_COMPANY_ID,
                name="Evergreen Community Management",
            )
        )
    for community_id, name, office_phone, policy in (
        (
            COMMUNITY_ID,
            "Pine Grove Community",
            "+15550102000",
            "Active leaks, fire, gas, entrapment, and unsafe access require immediate escalation.",
        ),
    ):
        if not await session.get(CommunityRecord, community_id):
            session.add(
                CommunityRecord(
                    id=community_id,
                    management_company_id=MANAGEMENT_COMPANY_ID,
                    name=name,
                    office_phone=office_phone,
                    emergency_policy_summary=policy,
                )
            )
    await session.commit()

    for email, display_name, role, scoped_community in (
        ("ops@evergreen.demo", "Morgan Lee", "operations_admin", None),
        ("manager@evergreen.demo", "Sam Rivera", "community_manager", COMMUNITY_ID),
        ("oncall@evergreen.demo", "Avery Morgan", "on_call_responder", COMMUNITY_ID),
    ):
        user = await session.scalar(select(StaffUserRecord).where(StaffUserRecord.email == email))
        if not user:
            user = StaffUserRecord(
                email=email,
                display_name=display_name,
                password_hash=hash_password("demo-pass"),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        membership = await session.scalar(
            select(StaffMembershipRecord).where(
                StaffMembershipRecord.staff_user_id == user.id,
                StaffMembershipRecord.management_company_id == MANAGEMENT_COMPANY_ID,
            )
        )
        if not membership:
            session.add(
                StaffMembershipRecord(
                    staff_user_id=user.id,
                    management_company_id=MANAGEMENT_COMPANY_ID,
                    role=role,
                    community_id=scoped_community,
                )
            )
    # The MVP intentionally exposes one community only. Keep any legacy second
    # community record inactive so existing databases do not leak it into console.
    legacy_community = await session.get(CommunityRecord, LEGACY_REMOVED_COMMUNITY_ID)
    if legacy_community:
        legacy_community.is_active = False
    contacts = ((COMMUNITY_ID, "Avery Morgan", "+15550102999"),)
    for community_id, name, phone in contacts:
        existing = await session.scalar(
            select(RoutingContactRecord.id).where(
                RoutingContactRecord.community_id == community_id,
                RoutingContactRecord.contact_type == "on_call",
            )
        )
        if not existing:
            session.add(
                RoutingContactRecord(
                    management_company_id=MANAGEMENT_COMPANY_ID,
                    community_id=community_id,
                    contact_type="on_call",
                    name=name,
                    phone=phone,
                )
            )
    vendors = (
        ("Northstar Mechanical", "+15550104000"),
        ("Rapid Response Restoration", "+15550104001"),
        ("Pine Grove Plumbing Services", "+15550104002"),
    )
    vendor_records = {}
    for name, phone in vendors:
        vendor = await session.scalar(
            select(VendorRecord).where(
                VendorRecord.management_company_id == MANAGEMENT_COMPANY_ID,
                VendorRecord.name == name,
            )
        )
        if not vendor:
            vendor = VendorRecord(
                management_company_id=MANAGEMENT_COMPANY_ID,
                name=name,
                phone=phone,
            )
            session.add(vendor)
            await session.flush()
        vendor_records[name] = vendor
    routes = (
        (
            "hvac",
            "Northstar Mechanical",
            "inspection_only",
            15000,
            15000,
            "confirm_before_booking",
        ),
        (
            "active_flooding",
            "Rapid Response Restoration",
            "auto_up_to_limit",
            None,
            None,
            "association",
        ),
        (
            "plumbing",
            "Pine Grove Plumbing Services",
            "inspection_only",
            None,
            None,
            "confirm_before_booking",
        ),
    )
    for incident_type, vendor_name, mode, fee, limit, payer in routes:
        existing = await session.scalar(
            select(VendorRouteRecord).where(
                VendorRouteRecord.community_id == COMMUNITY_ID,
                VendorRouteRecord.incident_type == incident_type,
            )
        )
        if not existing:
            vendor = vendor_records[vendor_name]
            session.add(
                VendorRouteRecord(
                    management_company_id=MANAGEMENT_COMPANY_ID,
                    community_id=COMMUNITY_ID,
                    incident_type=incident_type,
                    vendor_id=vendor.id,
                    vendor_name=vendor.name,
                    contact_phone=vendor.phone,
                    dispatch_mode=mode,
                    diagnostic_fee_cents=fee,
                    automatic_approval_limit_cents=limit,
                    payer_responsibility=payer,
                ),
            )
        else:
            vendor = vendor_records[vendor_name]
            existing.vendor_id = existing.vendor_id or vendor.id
            existing.vendor_name = vendor.name
            existing.contact_phone = existing.contact_phone or vendor.phone
            # These are the seeded pilot routes. Keep their policy deterministic when
            # upgrading a database that already had the old name/phone-only records.
            existing.dispatch_mode = mode
            existing.diagnostic_fee_cents = fee
            existing.automatic_approval_limit_cents = limit
            existing.payer_responsibility = payer
    await session.commit()


async def seed_demo_hotel(session: AsyncSession) -> None:
    """Seed a separate hotel workspace and a small fake PMS dataset.

    This is intentionally deterministic and idempotent. It gives the demo a
    real-looking hotel workflow without pretending to be connected to a live
    PMS or payment processor.
    """

    organization = await session.get(HotelOrganizationRecord, HOTEL_ORGANIZATION_ID)
    if not organization:
        session.add(
            HotelOrganizationRecord(
                id=HOTEL_ORGANIZATION_ID,
                name="Evergreen Hospitality Group",
            )
        )
        await session.flush()

    hotel = await session.get(HotelRecord, HOTEL_ID)
    if not hotel:
        hotel = HotelRecord(
            id=HOTEL_ID,
            organization_id=HOTEL_ORGANIZATION_ID,
            name="Evergreen Harbor Hotel",
            phone="+15550103000",
            timezone="America/New_York",
            emergency_policy_summary="For fire, medical, gas, or immediate safety concerns, contact the front desk or emergency services.",
        )
        session.add(hotel)
        await session.flush()

    departments = (
        ("front_desk", "Front desk", "+15550103001"),
        ("housekeeping", "Housekeeping", "+15550103002"),
        ("engineering", "Engineering", "+15550103003"),
        ("concierge", "Concierge", "+15550103004"),
        ("security", "Security", "+15550103005"),
        ("food_beverage", "Food and Beverage", "+15550103006"),
    )
    for key, name, phone in departments:
        existing = await session.scalar(
            select(HotelDepartmentRecord).where(
                HotelDepartmentRecord.hotel_id == HOTEL_ID,
                HotelDepartmentRecord.key == key,
            )
        )
        if not existing:
            session.add(
                HotelDepartmentRecord(
                    hotel_id=HOTEL_ID,
                    key=key,
                    name=name,
                    escalation_phone=phone,
                )
            )

    rooms = (
        ("101", "standard", 15900, "available"),
        ("102", "standard", 15900, "available"),
        ("103", "standard", 15900, "available"),
        ("204", "king", 21900, "occupied"),
        ("305", "suite", 31900, "available"),
        ("306", "suite", 31900, "available"),
    )
    for number, room_type, rate, room_status in rooms:
        existing = await session.scalar(
            select(HotelRoomRecord).where(
                HotelRoomRecord.hotel_id == HOTEL_ID,
                HotelRoomRecord.room_number == number,
            )
        )
        if not existing:
            session.add(
                HotelRoomRecord(
                    hotel_id=HOTEL_ID,
                    room_number=number,
                    room_type=room_type,
                    max_occupancy=4 if room_type == "suite" else 2,
                    nightly_rate_cents=rate,
                    status=room_status,
                )
            )
        else:
            existing.max_occupancy = 4 if room_type == "suite" else 2

    catalog_types = {
        "standard": {
            "display_name": "Standard",
            "description": "A practical room for a comfortable short stay.",
            "bed_type": "queen",
            "max_occupancy": 2,
            "amenities": ("wifi", "breakfast"),
            "rate_name": "Standard flexible rate",
            "meals": ["breakfast"],
        },
        "king": {
            "display_name": "King",
            "description": "A room with a king bed for a comfortable stay.",
            "bed_type": "king",
            "max_occupancy": 2,
            "amenities": ("wifi", "breakfast"),
            "rate_name": "King flexible rate",
            "meals": ["breakfast"],
        },
        "suite": {
            "display_name": "Suite",
            "description": "A larger room option for guests who want more space.",
            "bed_type": "king",
            "max_occupancy": 4,
            "amenities": ("wifi", "pool", "breakfast"),
            "rate_name": "Suite flexible rate",
            "meals": ["breakfast"],
        },
    }
    catalog_amenities = {
        "wifi": ("Wi-Fi", "connectivity", "Complimentary Wi-Fi access."),
        "pool": ("swimming pool access", "recreation", "Access to the hotel swimming pool."),
        "breakfast": ("breakfast", "meal", "Breakfast included with the configured rate."),
    }
    amenity_records: dict[str, HotelAmenityRecord] = {}
    for key, (name, category, description) in catalog_amenities.items():
        amenity = await session.scalar(
            select(HotelAmenityRecord).where(
                HotelAmenityRecord.hotel_id == HOTEL_ID,
                HotelAmenityRecord.key == key,
            )
        )
        if not amenity:
            amenity = HotelAmenityRecord(
                hotel_id=HOTEL_ID,
                key=key,
                name=name,
                category=category,
                description=description,
            )
            session.add(amenity)
            await session.flush()
        amenity_records[key] = amenity

    room_type_records: dict[str, HotelRoomTypeRecord] = {}
    for sort_order, (key, metadata) in enumerate(catalog_types.items()):
        room_type_record = await session.scalar(
            select(HotelRoomTypeRecord).where(
                HotelRoomTypeRecord.hotel_id == HOTEL_ID,
                HotelRoomTypeRecord.key == key,
            )
        )
        if not room_type_record:
            room_type_record = HotelRoomTypeRecord(
                hotel_id=HOTEL_ID,
                key=key,
                display_name=metadata["display_name"],
                description=metadata["description"],
                bed_type=metadata["bed_type"],
                max_occupancy=metadata["max_occupancy"],
                accessibility_notes=None,
                policies="Subject to availability and the hotel's standard cancellation policy.",
                sort_order=sort_order,
            )
            session.add(room_type_record)
            await session.flush()
        room_type_records[key] = room_type_record
        for amenity_key in metadata["amenities"]:
            association = await session.scalar(
                select(HotelRoomTypeAmenityRecord).where(
                    HotelRoomTypeAmenityRecord.room_type_id == room_type_record.id,
                    HotelRoomTypeAmenityRecord.amenity_id == amenity_records[amenity_key].id,
                )
            )
            if not association:
                session.add(
                    HotelRoomTypeAmenityRecord(
                        room_type_id=room_type_record.id,
                        amenity_id=amenity_records[amenity_key].id,
                    )
                )
        rate_plan = await session.scalar(
            select(HotelRatePlanRecord).where(
                HotelRatePlanRecord.room_type_id == room_type_record.id,
                HotelRatePlanRecord.code == "default",
            )
        )
        if not rate_plan:
            configured_rate = next(
                rate for number, room, rate, _ in rooms if room == key
            )
            session.add(
                HotelRatePlanRecord(
                    hotel_id=HOTEL_ID,
                    room_type_id=room_type_record.id,
                    code="default",
                    name=metadata["rate_name"],
                    nightly_rate_cents=configured_rate,
                    included_meals_json=json.dumps(metadata["meals"]),
                    cancellation_policy="Standard hotel cancellation policy applies.",
                )
            )
    catalog_version = await session.scalar(
        select(HotelCatalogVersionRecord).where(
            HotelCatalogVersionRecord.hotel_id == HOTEL_ID
        )
    )
    if not catalog_version:
        session.add(HotelCatalogVersionRecord(hotel_id=HOTEL_ID, version=1))

    # A dated availability scenario for the hotel demo: every standard room
    # is occupied from 17 to 19 September 2026, while the two suites remain
    # available. The records are confirmed stays, not an artificial room-status
    # toggle, so availability uses the same overlap logic as real bookings.
    for room_number in ("101", "102", "103"):
        confirmation_code = f"EH-SEP26-STD-{room_number}"
        existing = await session.scalar(
            select(HotelReservationRecord).where(
                HotelReservationRecord.hotel_id == HOTEL_ID,
                HotelReservationRecord.confirmation_code == confirmation_code,
            )
        )
        if existing:
            continue
        sold_out_room = await session.scalar(
            select(HotelRoomRecord).where(
                HotelRoomRecord.hotel_id == HOTEL_ID,
                HotelRoomRecord.room_number == room_number,
            )
        )
        assert sold_out_room is not None
        reservation = HotelReservationRecord(
            hotel_id=HOTEL_ID,
            room_id=sold_out_room.id,
            confirmation_code=confirmation_code,
            check_in=DEMO_SOLD_OUT_CHECK_IN,
            check_out=DEMO_SOLD_OUT_CHECK_OUT,
            guest_count=2,
            status="confirmed",
            payment_status="demo_confirmed",
            nightly_total_cents=sold_out_room.nightly_rate_cents,
        )
        session.add(reservation)
        await session.flush()
        session.add(
            HotelReservationRoomRecord(
                hotel_id=HOTEL_ID,
                reservation_id=reservation.id,
                room_id=sold_out_room.id,
                room_type=sold_out_room.room_type,
                guest_count=2,
                nightly_rate_cents=sold_out_room.nightly_rate_cents,
            )
        )

    guest = await session.scalar(
        select(HotelGuestRecord).where(
            HotelGuestRecord.hotel_id == HOTEL_ID,
            HotelGuestRecord.phone == "+15550101111",
        )
    )
    if not guest:
        guest = HotelGuestRecord(
            hotel_id=HOTEL_ID,
            full_name="Demo Guest",
            phone="+15550101111",
        )
        session.add(guest)
        await session.flush()
    room = await session.scalar(
        select(HotelRoomRecord).where(
            HotelRoomRecord.hotel_id == HOTEL_ID,
            HotelRoomRecord.room_number == "204",
        )
    )
    reservation = await session.scalar(
        select(HotelReservationRecord).where(
            HotelReservationRecord.hotel_id == HOTEL_ID,
            HotelReservationRecord.confirmation_code == "EH-DEMO204",
        )
    )
    if not reservation:
        reservation = HotelReservationRecord(
            hotel_id=HOTEL_ID,
            guest_id=guest.id,
            room_id=room.id if room else None,
            confirmation_code="EH-DEMO204",
            check_in=date.today(),
            check_out=date.today() + timedelta(days=2),
            guest_count=2,
            status="checked_in",
            payment_status="demo_confirmed",
        )
        session.add(reservation)

    facts = (
        (
            "front_desk",
            "check-in time",
            "Check-in begins at 3 PM; early check-in depends on room availability.",
        ),
        ("front_desk", "check-out time", "Check-out is at 11 AM."),
        ("amenity", "pool hours", "The pool is open daily from 7 AM to 10 PM."),
        (
            "amenity",
            "breakfast hours",
            "Breakfast is served from 6:30 AM to 10 AM in the Harbor dining room.",
        ),
        (
            "policy",
            "wifi",
            "Complimentary Wi-Fi is available throughout the hotel. The front desk can provide the access details.",
        ),
        (
            "routing",
            "maintenance",
            "Room maintenance requests are routed to Engineering; urgent safety issues are escalated to the front desk.",
        ),
        (
            "rooms",
            "standard room",
            "Standard rooms accommodate up to 2 guests and are the lower-rate option.",
        ),
        (
            "rooms",
            "suite room",
            "Suite rooms accommodate up to 4 guests and provide the larger-room option.",
        ),
    )
    for category, topic, answer in facts:
        existing = await session.scalar(
            select(HotelKnowledgeRecord).where(
                HotelKnowledgeRecord.hotel_id == HOTEL_ID,
                HotelKnowledgeRecord.topic == topic,
            )
        )
        if not existing:
            session.add(
                HotelKnowledgeRecord(
                    hotel_id=HOTEL_ID,
                    category=category,
                    topic=topic,
                    answer=answer,
                    source_reference="Evergreen Harbor demo operating guide",
                    approval_status="approved",
                )
            )

    menu_items = (
        ("Mains", "Maggi Noodles", ("maggie", "maggi", "instant noodles"), 2500),
        ("Mains", "Vegetable Noodles", ("noodles", "noodle"), 1800),
        ("Mains", "Chicken Manchurian", ("manchurian", "chicken manchurian"), 3200),
        ("Beverages", "Tea", ("chai",), 800),
        ("Beverages", "Coffee", ("coffee",), 1000),
    )
    for category, name, aliases, price_cents in menu_items:
        existing = await session.scalar(
            select(HotelMenuItemRecord).where(
                HotelMenuItemRecord.hotel_id == HOTEL_ID,
                HotelMenuItemRecord.name == name,
            )
        )
        if not existing:
            session.add(
                HotelMenuItemRecord(
                    hotel_id=HOTEL_ID,
                    category=category,
                    name=name,
                    aliases=json.dumps(list(aliases)),
                    price_cents=price_cents,
                    is_available=True,
                )
            )

    # Hotel identity and membership are fully separate from HOA staff tables.
    email = "hotel.manager@evergreen.demo"
    user = await session.scalar(
        select(HotelStaffUserRecord).where(HotelStaffUserRecord.email == email)
    )
    if not user:
        user = HotelStaffUserRecord(
            email=email,
            display_name="Jordan Ellis",
            password_hash=hash_password("demo-pass"),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    membership = await session.scalar(
        select(HotelStaffMembershipRecord).where(
            HotelStaffMembershipRecord.staff_user_id == user.id,
            HotelStaffMembershipRecord.organization_id == HOTEL_ORGANIZATION_ID,
        )
    )
    if not membership:
        session.add(
            HotelStaffMembershipRecord(
                staff_user_id=user.id,
                organization_id=HOTEL_ORGANIZATION_ID,
                hotel_id=HOTEL_ID,
                role="hotel_manager",
            )
        )
    await session.commit()

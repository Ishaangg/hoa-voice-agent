from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.fake import (
    COMMUNITY_ID,
    LEGACY_REMOVED_COMMUNITY_ID,
    MANAGEMENT_COMPANY_ID,
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

from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.fake import COMMUNITY_ID, MANAGEMENT_COMPANY_ID
from hoa_voice_agent.persistence.repository import CommunityKnowledgeRepository

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

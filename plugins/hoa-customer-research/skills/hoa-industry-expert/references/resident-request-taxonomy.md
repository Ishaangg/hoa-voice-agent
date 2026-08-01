---
topic: "HOA resident request taxonomy"
domain: "US HOA and community-association management"
geography: "United States"
last_verified: "2026-07-30"
refresh_after_days: 90
knowledge_type: "working-model"
status: "curated-from-source"
source_basis: "Split and curated from the uploaded master HOA report"
evidence_status: "derived from operational responsibilities; validate with interviews"
---

# Resident Request Taxonomy

This is a **working operational model**, not a measured frequency distribution. It should be validated through manager interviews and call-log data.

| Request category | Likely handler | Automation suitability | Principal risk |
|---|---|---:|---:|
| General policy or document question | Manager, portal, support desk | High when answer is approved and community-specific | Medium |
| Amenity hours, access, or booking | Manager, concierge, portal | High for informational requests | Low–Medium |
| Maintenance report intake | Manager, support desk, after-hours line | High for structured intake | Medium |
| Emergency or urgent incident report | On-call manager, vendor, emergency services | Partial; classification and escalation only | High |
| Work-order status | Manager, maintenance team, portal | High if system integration is reliable | Medium |
| Assessment balance or payment status | Accounting team or portal | Medium; requires authentication and live account data | High |
| Payment dispute or collections issue | Accounting, manager, counsel | Low | High |
| Architectural request | Manager plus ARC/ACC or board | Intake and status only | High |
| Violation notice or status | Manager, compliance team | Partial | High |
| Violation dispute or appeal | Manager, board, counsel | Low | High |
| Parking, towing, or access-control issue | Manager, security, towing vendor | Partial | High |
| Document or resale request | Manager, document service, portal | Medium–High | Medium |
| Board complaint or governance issue | Manager or board | Intake only | Medium–High |
| Vendor access or scheduling | Manager or site team | Partial | Medium |

## Required fields for voice-agent design

For each request type capture:

- Caller identity and relationship to property
- Community, address, unit, and account identifiers
- Request category and urgency
- Approved knowledge source
- Account-specific data required
- Authorisation boundary
- Escalation destination
- Service-level expectation
- Audit and recording requirements
- Failure consequences

## Safety boundary

Accepting a report is not the same as authorising repairs, interpreting governing documents, waiving charges, deciding violations, approving architectural changes, or making emergency-response decisions.

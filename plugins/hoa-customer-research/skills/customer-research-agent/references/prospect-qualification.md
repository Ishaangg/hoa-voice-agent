# Prospect Qualification

This file defines how to score companies and prioritise people for customer research.

## Company ICP score

Score each company from `0` to `100`.

### Industry fit — 0 to 25

- 25: HOA or community-association management is a core business
- 18: mixed property management with a substantial HOA practice
- 10: adjacent vendor or uncertain focus
- 0: not relevant

### Operational pain likelihood — 0 to 20

- 20: explicit after-hours, emergency, maintenance, or resident-support workflow
- 14: strong resident-service and maintenance responsibilities
- 7: some operational-complexity evidence
- 0: no relevant signal

### Company-size fit — 0 to 15

- 15: directly within the configured target range
- 10: slightly outside but commercially useful
- 5: size uncertain
- 0: clearly unsuitable

### Geographic fit — 0 to 10

- 10: operates in the configured target geography
- 5: mixed or uncertain fit
- 0: outside scope

### Buyer accessibility — 0 to 15

- 15: owner or operations leader is publicly identifiable and reachable
- 10: relevant leadership exists but access is uncertain
- 5: only frontline personnel are identifiable
- 0: no relevant person can be verified

### Evidence quality — 0 to 10

- 10: multiple current primary sources
- 7: one strong primary source
- 4: mostly secondary evidence
- 0: weak or unverifiable

### Strategic learning value — 0 to 5

- 5: can test a major startup assumption
- 3: useful but not central
- 0: low learning value

## Qualification thresholds

```text
80-100 = QUALIFIED, high priority
65-79  = QUALIFIED, normal priority
50-64  = NEEDS_REVIEW
0-49   = REJECTED
```

A high score does not override missing identity or source evidence.

## Person prioritisation

Prioritise:

1. Owner or founder
2. COO or operations executive
3. VP or director of operations
4. Regional leader
5. Portfolio-management leader
6. Senior community manager
7. Resident-support or customer-support leader

## Role categories

Use only values defined in `google-sheets-schema.md`.

## Problem proximity

- `HIGH`: directly owns or performs the target workflow
- `MEDIUM`: manages teams close to it or influences the process
- `LOW`: relevant but distant
- `UNKNOWN`: insufficient evidence

## Decision relevance

Use the strongest supported category from `google-sheets-schema.md`. Do not assume every executive is the budget owner.

## Person inclusion requirements

A person may be added only when current name, company, and role are verifiable; a company record already exists; at least one source supports the record; and the person is relevant to the research question.

## Exclusions

Exclude former employees presented as current, unrelated salespeople or recruiters, single-HOA board members when researching management-company buyers, ambiguous identities, previously contacted or opted-out people, and duplicates.

# Assumption Ledger Method

This file defines how to track startup assumptions. It is a methodology and template, not the live source of truth.

Until a dedicated Sheet tab exists, store the live ledger as a project Markdown file.

## Recommended fields

```text
assumption_id
statement
segment
type
status
supporting_evidence
contradictory_evidence
confidence
independent_sources
next_test
owner
last_updated
```

## Types

```text
PROBLEM
FREQUENCY
SEVERITY
WORKFLOW
BUYER
BUDGET
ADOPTION
PRICING
INTEGRATION
RISK
CHANNEL
```

## Statuses

```text
UNTESTED
PARTIALLY_SUPPORTED
SUPPORTED
CONTRADICTED
REJECTED
NEEDS_MORE_EVIDENCE
```

## Confidence

```text
LOW
MEDIUM
HIGH
```

## Evidence rules

- One interview is anecdotal.
- Independent repeated reports strengthen confidence.
- Behaviour is stronger than opinion.
- Existing spending is stronger than hypothetical willingness to pay.
- A pilot commitment is stronger than praise.
- Preserve contradictory evidence.

## Update rule

Update only when new evidence materially changes status or confidence. Record what changed, the source, date, and whether it supports or contradicts the assumption.

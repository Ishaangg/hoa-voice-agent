# Response Classification

Use only these `response_status` values:

```text
NO_RESPONSE
POSITIVE
QUESTIONS_BY_EMAIL
MEETING_OFFERED
REFERRAL_OFFERED
NEEDS_MORE_INFO
POLITE_DECLINE
NOT_RELEVANT
NEGATIVE
OPT_OUT
```

## `NO_RESPONSE`

No reply received. Keep the sequence active and follow up only when due.

## `POSITIVE`

Willing to engage, but no exact next step. Stop automated follow-ups and prepare a specific reply.

## `QUESTIONS_BY_EMAIL`

Willing to answer asynchronously. Stop the sequence and send no more than three focused questions.

## `MEETING_OFFERED`

Agrees to or proposes a meeting. Set `meeting_booked = TRUE` only after a time is confirmed, prepare an interview guide, and stop outreach.

## `REFERRAL_OFFERED`

Suggests another contact. Thank them, request permission to mention the referral, and verify the new person before adding them.

## `NEEDS_MORE_INFO`

Asks what the research is about. Answer briefly, restate the research objective, and avoid a product pitch.

## `POLITE_DECLINE`

Declines participation. Set `status = DECLINED` and stop follow-ups. Do not set `opted_out = TRUE` unless explicitly requested.

## `NOT_RELEVANT`

Topic is outside their role. Stop outreach and review whether qualification failed.

## `NEGATIVE`

Adverse response or complaint. Stop outreach and review targeting and message quality.

## `OPT_OUT`

Explicit no-contact request. Set `opted_out = TRUE`, `status = OPTED_OUT`, clear `followup_due_at`, and do not contact through another channel.

## Rules

- Use explicit meaning, not optimistic interpretation.
- A polite acknowledgement is not positive.
- Connection acceptance alone is not positive.
- Automated replies are not customer responses.
- A referral does not validate the problem.

# LinkedIn Message Templates

Use this file for all LinkedIn outreach performed through the configured browser workflow.

Preserve the exact action and message that were actually delivered in the `Outreach` tab.

---

## Connection Requests

Always send LinkedIn connection requests without a note.

Do not:

- click `Add a note`
- attach a personalised message
- use A/B variants during the connection-request step
- record an intended note as delivered

Record a successful connection request as:

```text
channel = LINKEDIN_CONNECTION
variant = NO_NOTE
subject = blank
message = SENT_WITHOUT_NOTE
status = SENT
response_status = NO_RESPONSE
meeting_booked = FALSE
opted_out = FALSE
```

Set `status = SENT` only after LinkedIn visibly confirms that the request was sent.

---

## Post-Acceptance DM Workflow

Send a DM only after LinkedIn visibly confirms that the person accepted the connection request.

Before sending:

1. Verify the person and current employer.
2. Confirm the connection request exists in `Outreach`.
3. Check `Responses` for any reply.
4. Check `Suppression` for any no-contact record.
5. Confirm the person has not declined or opted out.
6. Assign either `LINKEDIN_A` or `LINKEDIN_B`.
7. Replace every placeholder with verified information.
8. Check that no identical DM has already been sent.

Create a new `Outreach` row for the DM. Do not overwrite the connection-request row.

Record the DM as:

```text
channel = LINKEDIN_DM
variant = LINKEDIN_A or LINKEDIN_B
subject = blank
message = exact delivered message
status = SENT
response_status = NO_RESPONSE
meeting_booked = FALSE
opted_out = FALSE
```

---

## Post-Acceptance DM — `LINKEDIN_A`

```text
Thanks for connecting, [First name].

I’m researching how HOA management teams handle resident calls, urgent maintenance intake, and after-hours escalation.

Would you be open to answering three short questions here or having a brief 15-minute conversation?

This is customer research, not a product demo.
```

Purpose: test whether offering both written questions and a short call increases qualified replies.

---

## Post-Acceptance DM — `LINKEDIN_B`

```text
Thanks for connecting, [First name].

I’m an AI engineer researching resident-support workflows at HOA management companies.

Your work at [Company] appeared closely related to this problem. Would you be open to a short 15-minute conversation?

I’m trying to understand the current workflow before building anything.
```

Purpose: test whether a more specific, call-focused request produces more meetings.

---

## Follow-Up DM — `LINKEDIN_FOLLOWUP_1`

Send only when:

- the first DM was delivered
- no reply has been received
- `followup_due_at` has passed
- the person is not suppressed
- no previous LinkedIn follow-up has been sent

```text
Hi [First name] — following up once in case this is relevant.

I’m speaking with people close to HOA operations about resident support and after-hours maintenance. Three short answers here would be useful, and there is no sales presentation involved.
```

Create a separate `Outreach` row for the follow-up.

---

## Final Follow-Up DM — `LINKEDIN_FOLLOWUP_2`

Use only when the configured limit permits a second follow-up.

```text
Hi [First name] — this will be my final follow-up.

I’m trying to learn from people close to HOA operations before deciding whether this problem is worth building for. If someone else at [Company] is better placed to discuss resident support or after-hours maintenance, I would appreciate a pointer.

Thank you,
Ishaan
```

Do not send another follow-up after this message.

---

## InMail — `LINKEDIN_INMAIL_A`

**Subject:** Researching HOA resident-support workflows

```text
Hi [First name],

I’m Ishaan, an AI engineer researching how HOA management companies handle resident calls, maintenance intake, and after-hours escalation.

I contacted you because [verified relevant reason].

Would you be open to answering three short questions by message or having a brief 15-minute conversation?

I’m trying to understand the existing workflow before building anything.

Best,
Ishaan
```

---

## Personalisation Rules

Every personalised claim must be:

- truthful
- professionally relevant
- publicly verifiable
- supported by `People.personalisation_source`

Acceptable facts include:

- the person leads operations
- the company provides 24/7 emergency support
- the company operates across several states
- the person publicly discussed resident service or maintenance operations
- the company has a clearly relevant operational model

Do not use:

- generic praise
- invented referrals
- sensitive personal information
- unsupported claims
- fake urgency
- language implying a previous relationship

When no person-level fact exists, use a verified company-level fact.

When no reliable fact exists, use baseline wording instead of fabricating personalisation.

---

## A/B Testing Rules

Use `LINKEDIN_A` and `LINKEDIN_B` only for post-acceptance DMs.

Do not use A/B variants for connection requests because all requests are sent without a note.

For a valid test:

- use the same channel
- target similar role categories
- target similar ICP-score ranges
- use a similar sending period
- test one meaningful variable
- preserve the exact delivered wording
- record the variant before sending

Primary metrics:

- qualified reply rate
- meeting-booked rate

Secondary metrics:

- overall reply rate
- referral rate
- decline rate
- opt-out rate

Do not declare a winner from a very small sample.

---

## Stop Rules

Stop outreach when:

- any reply is received
- the person declines
- the person opts out
- the topic is declared irrelevant
- the identity or employer becomes uncertain
- LinkedIn displays a warning, restriction, CAPTCHA, identity check, or unusual activity notice
- the browser state is uncertain
- the configured daily or per-run limit is reached

Do not automatically retry an uncertain action.

---

## Recordkeeping Rules

For every LinkedIn action:

- create a separate `Outreach` row
- use a unique `outreach_id`
- preserve the exact delivered message
- set `sent_at` only after visible confirmation
- leave `subject` blank unless using InMail
- keep connection requests and DMs as separate records
- never record a draft as sent
- never record an intended note as delivered
- update `response_status` only after observing a real reply
- set `opted_out = TRUE` after an explicit opt-out
- check `Responses` and `Suppression` before every new action

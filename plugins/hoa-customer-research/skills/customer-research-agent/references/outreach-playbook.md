# Outreach Playbook

## Objective

Create qualified customer-research conversations while preserving truthful personalisation, accurate records, and clean experiment data.

## Sequence

```text
Company discovered
→ Company verified
→ Company scored
→ Person identified
→ Person verified
→ Contact channel verified
→ Duplicate check
→ Message generated
→ Preflight check
→ Outreach sent
→ Ledger updated
→ Follow-up or reply handling
→ Sequence closed
```

## Before outreach

Read `config/config.yaml`, `google-sheets-schema.md`, `prospect-qualification.md`, `personalisation-rules.md`, and `deduplication-rules.md`.

Confirm that the company is `QUALIFIED`, the person exists and has a verified role, the channel is valid, the personalisation source is saved, no duplicate exists, and the person has not opted out.

## Channel selection

Use email when a permitted business email exists and asynchronous answers are suitable.

Use LinkedIn when identity is clearly verified there, no valid email is available, the person is active in the industry, or a connection-first approach is appropriate.

Use a company form when no person-level channel exists but the company is a strong fit.

## LinkedIn connection request rule

Always send LinkedIn connection requests without a note.

Do not click `Add a note`.
Do not attempt to attach a personalised message to the connection request.

For a connection request:

- use `channel = LINKEDIN_CONNECTION`
- leave `subject` blank
- leave `message` blank, or store `SENT_WITHOUT_NOTE`
- set `status = SENT` only after LinkedIn visibly confirms the request
- set `response_status = NO_RESPONSE`

After the person accepts the connection, send the approved personalised message as a separate LinkedIn DM using `channel = LINKEDIN_DM`.

Do not record an intended connection note as sent when no note was attached.

## Default cadence

```text
Day 0: Initial email or LinkedIn connection
Day 4-6: First follow-up on the initial channel
Day 8-12: Secondary-channel touch when appropriate
Day 14+: Final follow-up or close
```

Follow configured limits when they differ.

## Follow-up rules

- Maximum two follow-ups by default.
- Never follow up before `followup_due_at`.
- Stop immediately after any reply.
- Do not follow up after a bounce, decline, or opt-out.
- Do not resend because an automation is uncertain.

## Company-level coordination

Avoid contacting several people at the same company simultaneously unless they serve clearly different research purposes and the reason is recorded.

## After sending

Immediately update `status`, `sent_at`, `followup_due_at`, exact `message`, `variant`, and `channel`.

## Failure handling

Set `NEEDS_REVIEW` for ambiguous identity, uncertain company fit, invalid email status, unsourced personalisation, inconclusive duplicate checks, unresolved placeholders, or uncertain browser/Sheet state.

# Google Sheets Schema

The active spreadsheet is defined in `config/config.yaml`.

The Google Sheet is the authoritative source of truth for:

- companies
- people
- outreach activity

Before reading or writing:

1. Read `config/config.yaml`.
2. Resolve the actual visible tab names.
3. Confirm the required headers exist.
4. Read existing rows before inserting new records.
5. Never guess a sheet name, column position, or identifier.
6. Stop outreach if the spreadsheet cannot be read.

---

## Required tabs

- `Companies`
- `People`
- `Outreach`

The column names and order below must match the Google Sheet exactly.

---

# Companies

One row represents one company.

## Required columns in exact order

```text
company_id
company_name
website
domain
location
states_served
estimated_size
company_type
after_hours_signal
icp_score
qualification_status
qualification_reason
source_urls
last_verified_at
```

## Column definitions

### `company_id`
Stable unique company identifier. Format: `CMP-0001`. Never reuse an ID.

### `company_name`
Official publicly verified company name.

### `website`
Canonical official company website URL.

### `domain`
Normalised company domain without protocol or path, for example `examplehoa.com`.

### `location`
Headquarters or main operating location.

### `states_served`
States or regions publicly confirmed as served by the company. Separate multiple values with commas.

### `estimated_size`
A publicly supported size estimate, such as `11-50 employees`, `50-100 communities`, `Regional operator`, or `Unknown`. Do not invent precise figures.

### `company_type`
Allowed values:

```text
HOA_MANAGEMENT_COMPANY
COMMUNITY_ASSOCIATION_MANAGEMENT
FULL_SERVICE_MANAGEMENT
SELF_MANAGED_ASSOCIATION
ADJACENT_VENDOR
OTHER
```

### `after_hours_signal`
Allowed values:

```text
CONFIRMED
POSSIBLE
NOT_FOUND
NOT_APPLICABLE
```

### `icp_score`
Integer from `0` to `100`. The scoring method should follow `prospect-qualification.md`.

### `qualification_status`
Allowed values:

```text
DISCOVERED
RESEARCHING
QUALIFIED
REJECTED
NEEDS_REVIEW
```

### `qualification_reason`
Concise explanation of why the company qualifies, does not qualify, or needs review.

### `source_urls`
Public sources supporting the company record. Separate multiple URLs with ` | `.

### `last_verified_at`
ISO-8601 timestamp, for example `2026-07-30T12:00:00+05:30`.

---

# People

One row represents one person.

## Required columns in exact order

```text
person_id
company_id
full_name
title
role_category
linkedin_url
email
email_status
problem_proximity
decision_relevance
personalisation_fact
personalisation_source
last_verified_at
```

## Column definitions

### `person_id`
Stable unique person identifier. Format: `PER-0001`. Never reuse an ID.

### `company_id`
Must match an existing `Companies.company_id`. Do not add a person before creating the related company record.

### `full_name`
Current publicly verified professional name.

### `title`
Current publicly verified job title.

### `role_category`
Allowed values:

```text
OWNER
EXECUTIVE
OPERATIONS_LEADER
REGIONAL_LEADER
PORTFOLIO_MANAGER
COMMUNITY_MANAGER
CUSTOMER_SUPPORT
OTHER
```

### `linkedin_url`
Canonical LinkedIn profile URL. Remove tracking parameters and trailing slashes before duplicate checks.

### `email`
Verified business email address.

### `email_status`
Allowed values:

```text
PUBLICLY_LISTED
PROVIDER_VERIFIED
INFERRED_VERIFIED
INFERRED_UNVERIFIED
GENERIC_COMPANY_EMAIL
NOT_FOUND
BOUNCED
```

Do not send email to records marked `INFERRED_UNVERIFIED`, `NOT_FOUND`, or `BOUNCED`.

### `problem_proximity`
Allowed values:

```text
HIGH
MEDIUM
LOW
UNKNOWN
```

### `decision_relevance`
Allowed values:

```text
BUDGET_OWNER
DECISION_MAKER
CHAMPION
END_USER
INDUSTRY_EXPERT
UNKNOWN
```

### `personalisation_fact`
One concise and truthful fact explaining why the person was selected.

### `personalisation_source`
Public source URL supporting the personalisation fact.

### `last_verified_at`
ISO-8601 timestamp.

---

# Outreach

One row represents one outreach action. A person may have multiple outreach rows, such as an initial email, LinkedIn connection request, follow-up email, or LinkedIn DM.

## Required columns in exact order

```text
outreach_id
person_id
channel
variant
subject
message
status
sent_at
followup_due_at
response_status
meeting_booked
opted_out
notes
```

## Column definitions

### `outreach_id`
Stable unique outreach-event identifier. Format: `OUT-0001`. Never reuse an ID.

### `person_id`
Must match an existing `People.person_id`.

### `channel`
Allowed values:

```text
EMAIL
LINKEDIN_CONNECTION
LINKEDIN_DM
LINKEDIN_INMAIL
CONTACT_FORM
WARM_INTRO
```

### `variant`
The approved outreach template or experiment variant, such as `BASELINE`, `A`, `B`, `EMAIL_A`, or `LINKEDIN_A`.

### `subject`
Email subject line. Leave blank for channels that do not use a subject.

### `message`
Exact message drafted or sent. Preserve the final sent wording and do not overwrite it later.

### `status`
Allowed values:

```text
DRAFT
READY
SENT
FOLLOWUP_DUE
REPLIED
COMPLETED
FAILED
BOUNCED
DECLINED
OPTED_OUT
NEEDS_REVIEW
```

### `sent_at`
ISO-8601 timestamp for when the outreach was sent. Leave blank until the message is actually sent.

### `followup_due_at`
ISO-8601 timestamp for the next permitted follow-up. Leave blank when no follow-up is planned, the person replied, declined, opted out, or the message bounced.

### `response_status`
Allowed values:

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

### `meeting_booked`
Use only `TRUE` or `FALSE`.

### `opted_out`
Use only `TRUE` or `FALSE`. When `TRUE`, no further outreach may be sent to that person.

### `notes`
Concise operational notes, exceptions, or next actions. Do not use this field as a substitute for structured statuses.

---

# ID rules

Use stable sequential identifiers:

```text
CMP-0001
PER-0001
OUT-0001
```

Before assigning a new ID:

1. Read the existing IDs in the relevant tab.
2. Find the highest numeric suffix.
3. Increment it by one.
4. Never fill gaps by reusing an old ID.

---

# Deduplication rules

Before adding a company, check:

1. Exact normalised domain.
2. Canonical website URL.
3. Normalised company name.

Before adding a person, check:

1. Exact email address.
2. Exact normalised LinkedIn URL.
3. Normalised full name plus `company_id`.

Before preparing or sending outreach, check:

1. Existing outreach rows for the same `person_id`.
2. Whether `response_status` shows a reply, decline, or opt-out.
3. Whether `opted_out` is `TRUE`.
4. Whether a follow-up is already scheduled.
5. Whether the same message variant has already been sent through the same channel.

---

# Normalisation rules

Before duplicate comparisons:

- lowercase email addresses
- lowercase domains
- remove `http://` and `https://` from domains
- remove `www.` from domains
- trim whitespace
- collapse repeated spaces in names
- remove trailing slashes from LinkedIn URLs
- remove URL tracking parameters
- compare company names without punctuation where useful

---

# Write rules

- Read the existing tab before writing.
- Append new records instead of overwriting history.
- Preserve exact source URLs.
- Preserve the exact sent message.
- Use ISO-8601 timestamps.
- Never invent contact details.
- Never send when required fields are missing.
- Never send when unresolved placeholders remain.
- Never contact a person who opted out.
- Never send to an unverified or bounced email.
- Stop all writes and outreach if the Sheet cannot be accessed reliably.

---

# Message preflight checks

Before marking an outreach row as `READY` or `SENT`, confirm:

- the company exists in `Companies`
- the person exists in `People`
- the company is qualified
- the person's current role has been verified
- the selected channel is valid
- the personalisation fact has a source
- the message contains no unresolved placeholders
- no duplicate outreach exists
- the person has not opted out
- the email status permits sending, when using email
- the current outreach limit has not been exceeded

If any check fails, set `status = NEEDS_REVIEW` and do not send the message.

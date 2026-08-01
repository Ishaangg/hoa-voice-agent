# Deduplication Rules

The purpose is to prevent repeated records, duplicate outreach, conflicting follow-ups, and contaminated experiments.

## Company checks

Compare normalised domain, canonical website, normalised company name, and geography when names are similar. Treat branches as one company unless explicitly configured otherwise.

## Person checks

Compare exact lowercased email, canonical LinkedIn URL, normalised full name plus `company_id`, name variations, and current versus former employment.

Do not create a second person record merely because the title changed.

## Outreach checks

Before outreach, check existing rows for the same `person_id`, channel, variant, status, `sent_at`, `followup_due_at`, response, and opt-out.

## Stop conditions

Do not contact when:

- `opted_out = TRUE`
- `response_status = OPT_OUT`
- `response_status = POLITE_DECLINE`
- `response_status = NEGATIVE`
- `status = BOUNCED`
- `status = DECLINED`
- `status = OPTED_OUT`
- an unresolved reply exists

## Normalisation

- lowercase domains and emails
- remove protocol, `www.`, paths, and parameters from domains
- trim and collapse spaces in names
- remove tracking parameters and trailing slash from LinkedIn URLs

## Company-level collision

When one person at a company is in an active sequence, avoid another executive unless the campaign permits it or the roles serve distinct research purposes. Record the reason in `notes`.

## IDs

Never solve duplication by reusing an ID for another entity. Never assign a new ID before checking existing records.

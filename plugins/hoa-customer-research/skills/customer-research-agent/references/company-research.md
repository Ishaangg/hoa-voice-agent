# Company Research Playbook

Use this playbook when identifying and qualifying HOA or community-association management companies.

## Objective

Find companies that are relevant to the current customer-research hypothesis and capture only verifiable information needed for qualification and outreach.

The active ICP, geography, ledger, and operating limits come from `config/config.yaml`.

## Required workflow

1. Read `config/config.yaml`.
2. Read `prospect-qualification.md`.
3. Read the existing `Companies` tab.
4. Search for candidate companies.
5. Verify each candidate through its official website.
6. Gather additional public evidence where useful.
7. Check for duplicates.
8. Calculate an ICP score.
9. Save only sufficiently supported records.
10. Mark uncertain records `NEEDS_REVIEW`.

## Rotating discovery strategy

Do not treat a fixed LinkedIn results page as the prospect universe. The same highly ranked companies will recur, so discovery must maintain a rotating frontier:

1. Read all existing company domains, canonical URLs, names, qualification status, and `last_verified_at` values.
2. Exclude existing companies from new-company discovery. A company may be re-verified after the configured freshness window, but it must not be counted as a new prospect again.
3. Rotate at least one discovery dimension per run: state or metro, company type, portfolio-size signal, after-hours workflow, buyer role, or industry directory.
4. Rotate the primary discovery source across Google Search, official websites, state records, CAI directories or chapters, official events and speakers, press releases, reputable industry reporting, and public professional profiles.
5. Use Google Search to discover candidates by rotating queries across states, metros, company types, portfolio-size language, after-hours terms, and workflow terms. Search results are leads only; verify each candidate on the official website and supporting sources before saving it.
6. Use LinkedIn as one discovery and person-verification source, not the only source. Paginate when searching it, but do not repeatedly start from the same unfiltered query.
7. Maintain a frontier of unseen candidate domains and names. Prefer candidates not previously seen, not merely candidates that appear first in a ranking.
8. Re-verify known companies only after the configured freshness interval or when a new public signal changes their qualification.
9. Report coverage in each run: discovery dimensions used, sources used, new candidates found, known candidates skipped, and stale records due for re-verification.

This makes the research process a coverage-and-freshness system rather than a repeated first-page scrape.

## CAI filtered-directory workflow

When `primary_discovery_source` is `cai_filtered_directory`, use the CAI Directory of Credentialed Professionals as the first structured discovery layer:

1. Rotate state, credential, chapter or metro, sort order, and pagination filters across runs.
2. Start with AAMC companies, then rotate through PCAM, AMS, and LSM-related records so discovery is not limited to one credential or company tier.
3. Treat CAI entries as leads, not complete qualification. Verify the company independently through its official website and supporting public sources.
4. Record the CAI URL and applied filters in the source trail or research-run log.
5. Do not recreate, bulk-export, or treat the directory as a marketing list. Capture only the minimum information needed to qualify a company and identify an appropriate public professional contact.
6. After company verification, search official leadership pages, LinkedIn, Google/web results, event pages, and other public professional sources for relevant people.

## Preferred research sources

1. Official company website
2. Official state business or licensing records where relevant
3. Community Associations Institute chapter or directory pages
4. Official event, speaker, or conference pages
5. Company press releases
6. Reputable industry reporting
7. Public professional profiles
8. Search-result snippets for discovery only

## Company verification requirements

Before adding a company, verify:

- official company name
- canonical website
- primary domain
- headquarters or operating geography
- evidence that it manages HOAs, condominiums, or community associations
- publicly supported size signal when available
- after-hours or resident-support signal when available
- why it matches the current ICP

## Size estimation

Use only publicly supported descriptions, such as `11-50 employees`, `Manages 75 communities`, `Regional operator`, or `Unknown`.

Do not convert vague claims into precise numbers.

## After-hours signal

### `CONFIRMED`

The company explicitly states that it provides 24/7 support, after-hours support, emergency response, emergency maintenance intake, on-call services, or round-the-clock resident support.

### `POSSIBLE`

The company mentions emergency maintenance, resident support, or rapid response, but the exact after-hours process is unclear.

### `NOT_FOUND`

No reliable public evidence was found.

### `NOT_APPLICABLE`

The company is not an operating management firm or the signal does not apply.

## Source recording

Store supporting sources in `source_urls`, separated by ` | `. The first URL should normally be the official company website.

## Rejection criteria

Reject or deprioritise:

- rental property managers with no community-association practice
- individual real-estate agents
- homebuilders without a management operation
- generic software vendors when the ICP requires management companies
- single volunteer-run associations when the ICP requires professional managers
- companies outside the configured geography
- unverifiable candidates
- duplicates

## Required output

Every saved company must include all fields defined for the `Companies` tab in `google-sheets-schema.md`.

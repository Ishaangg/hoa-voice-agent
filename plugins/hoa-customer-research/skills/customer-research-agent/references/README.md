# Customer Research Agent Reference Pack

Copy the Markdown files in this directory into `customer-research-agent/references/`.

They are designed to work with `SKILL.md`, `config/config.yaml`, and a Google Sheet containing `Companies`, `People`, and `Outreach`.

## Responsibilities

| File | Responsibility |
|---|---|
| `company-research.md` | Research and verify companies |
| `prospect-qualification.md` | Score companies and prioritise people |
| `personalisation-rules.md` | Create truthful, sourced personalisation |
| `email-templates.md` | Approved email messages and follow-ups |
| `linkedin-message-templates.md` | LinkedIn connection and DM templates |
| `outreach-playbook.md` | Control the outreach lifecycle |
| `deduplication-rules.md` | Prevent duplicate records and messages |
| `response-classification.md` | Classify replies and choose next actions |
| `interview-playbook.md` | Conduct non-leading customer interviews |
| `assumption-ledger.md` | Track startup assumptions and evidence |
| `ab-testing-playbook.md` | Run controlled message experiments |
| `google-sheets-schema.md` | Define the exact live Sheet structure |

`google-sheets-schema.md` is canonical for column names, column order, Sheet statuses, and ID formats. Other files must not introduce conflicting values.

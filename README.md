# trade-leads

Find local trades businesses via Google Places and score them as prospects for a missed-call-text-back service.

## Setup

```bash
pip install -e .
cp .env.example .env
# Fill in your API keys in .env
```

You need:
- **Google Places API key** — enable the Places API in Google Cloud Console
- **Anthropic API key** — for subject line generation during `draft` (optional but recommended)
- **ZeroBounce API key** — for email validation during `search` (optional; omit key to skip)

### Gmail integration (optional)

Enables `draft` to automatically create a Gmail draft with the recipient, subject, and body pre-loaded.

**One-time setup (5 minutes):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and select your existing project (or create a new one)
2. In the left menu go to **APIs & Services > Library**, search for **Gmail API**, and click **Enable**
3. Go to **APIs & Services > Credentials**, click **Create Credentials > OAuth client ID**
4. Choose **Desktop app** as the application type, give it any name, click **Create**
5. Click **Download JSON** on the credential you just created
6. Rename the downloaded file to `credentials.json` and place it in the `lead-research` folder (same folder as this README)
7. The first time you run `trade-leads draft`, a browser window will open asking you to sign in to your Google account and grant access. Do this once and it won't ask again — the token is saved to `gmail_token.json`

> **Note:** Keep `credentials.json` and `gmail_token.json` out of any git repository. They give access to compose emails from your Gmail account.

## Usage

### Search

```bash
trade-leads search --trade "plumber" --location "Guildford, Surrey" --radius 10 --limit 50
```

Options:
- `--trade` — trade type to search (plumber, electrician, roofer, etc.)
- `--location` — location string passed to Google Places
- `--radius` — search radius in km (default: 10)
- `--limit` — max businesses to fetch (default: 50)
- `--dry-run` — skip ZeroBounce email validation

Results are cached in `trade_leads.db` — re-running the same search doesn't hit the API again.

### Draft

```bash
trade-leads draft [--id LEAD_ID] [--template N] [--dry-run]
```

Picks the next uncontacted lead with a valid email (or a specific lead with `--id`), generates 3 hook options using Claude Haiku, and lets you choose one. Recommends the best template and fills it with lead data. Saves the complete draft to the database.

- `--id` — numeric lead ID from `trade-leads status`
- `--template` — override the recommended template (1–5)
- `--dry-run` — skip Claude hook generation, show draft but don't save

### Send

```bash
trade-leads send --id LEAD_ID [--dry-run]
```

Displays the saved draft for a lead, asks for confirmation, then marks it as sent and calculates follow-up due dates (Day 4 and Day 9).

- Does **not** actually send email — it records that you sent it
- `--dry-run` — show what would happen without writing to the database

### Follow-up

```bash
trade-leads followup [--id LEAD_ID] [--dry-run]
```

Without `--id`: lists all leads where a follow-up is due today or overdue.

With `--id`: generates a follow-up draft from the appropriate template file (`templates/followup_1.txt` or `templates/followup_2.txt`), referencing the original send date. Marks the follow-up as sent on confirmation.

### Status

```bash
trade-leads status
```

Prints a pipeline summary:
- Total leads, breakdown by trade and area
- Counts by stage: not contacted / drafted / emailed / responded / closed
- Follow-up 1 and 2 overdue counts
- Next 5 upcoming follow-ups with dates

### Export

```bash
trade-leads export --output leads.csv
```

Exports all leads including all pipeline fields to CSV.

## Email templates

Templates live in the `templates/` folder. Edit them to write your copy.

| File | Purpose |
|---|---|
| `template_1.txt` | Initial outreach — template 1 |
| `template_2.txt` | Initial outreach — template 2 |
| `template_3.txt` | Initial outreach — template 3 |
| `template_4.txt` | Initial outreach — template 4 |
| `template_5.txt` | Initial outreach — template 5 |
| `followup_1.txt` | Day 4 follow-up |
| `followup_2.txt` | Day 9 follow-up |

### Placeholders

All templates support these placeholders:

| Placeholder | Value |
|---|---|
| `{{name}}` | Recipient's first name (left unfilled — replace manually before sending) |
| `{{company}}` | Business name from Google Places |
| `{{trade}}` | Trade type, e.g. `plumber` |
| `{{trade_plural}}` | Plural trade, e.g. `plumbers` |
| `{{area}}` | Town/city extracted from address |
| `{{hook}}` | The hook line you selected during `draft` |

Follow-up templates additionally receive:

| Placeholder | Value |
|---|---|
| `{{sent_date}}` | Date the original email was sent (YYYY-MM-DD) |

## Output columns (CSV export)

| Column | Description |
|---|---|
| `name` | Business name |
| `phone` | Phone number |
| `email` | Contact email found on website |
| `email_validation_status` | ZeroBounce result: valid / invalid / catch-all / spamtrap / abuse / do_not_mail / unknown |
| `website` | Website URL |
| `address` | Full address |
| `trade` | Trade type searched |
| `rating` | Google rating (1–5) |
| `review_count` | Total reviews |
| `missed_call_score` | 0–3: how many reviews mention contact/response failures |
| `top_review_excerpts` | Up to 3 matching excerpts, pipe-separated |
| `has_existing_automation` | yes / no / unknown |
| `google_maps_url` | Direct Maps link |
| `status` | Pipeline stage: not_contacted / drafted / emailed / responded / closed |
| `draft_generated_at` | When the draft was created |
| `template_number` | Which template was used |
| `selected_hook` | The hook line chosen during draft |
| `sent_at` | When the email was marked as sent |
| `followup1_due` | Day 4 follow-up due date |
| `followup2_due` | Day 9 follow-up due date |
| `followup1_sent_at` | When follow-up 1 was marked as sent |
| `followup2_sent_at` | When follow-up 2 was marked as sent |
| `response_received` | 1 if a response was received |
| `outcome` | Closed outcome notes |
| `notes` | Free-text notes |

## Missed-call scoring

Scans up to 5 Google reviews per business for phrases like "didn't call back", "no answer", "had to chase", "left a message", etc. Each matching review adds 1 point, capped at 3. Score ≥ 2 is a strong prospect.

## Caching

All Google Places API responses are cached in `trade_leads.db` keyed by place_id and query. Re-running is fast and free. Delete `trade_leads.db` to force a fresh fetch.

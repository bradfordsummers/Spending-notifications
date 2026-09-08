# Spending Notifications

Texts one daily summary of spending on the Bank of America Alaska Airlines card
vs a **$9,000/month** budget. Live since 2026-07-22.

```
Spending
62% of $9,000 budget
$3,420 left, 12 days to go
This month: $5,580 of $9,000
Yesterday: $247.83
```

**Stack:** Plaid (transactions) → GitHub Actions cron → carrier email-to-SMS
gateway (Gmail SMTP). No server, no database, no app.

## Layout

| File | Purpose |
|------|---------|
| `daily_report.py` | The job: fetch → budget math → send. Entry point. |
| `notify.py` | Delivery. Only file that knows about channels. |
| `config.py` | Env vars (`.env` locally, repo Secrets in CI). |
| `plaid_client.py` | Builds the Plaid API client. |
| `link_card.py` | One-time card linking; prints `PLAID_ACCESS_TOKEN`. |
| `.github/workflows/daily.yml` | Cloud scheduler. |
| `docs/` | GitHub Pages (privacy/terms/opt-in). Vestigial — built for Twilio A2P vetting; keeps the repo public-safe. |

## Design decisions worth knowing

**Delivery is email-to-SMS.** Twilio was tried first and abandoned — A2P 10DLC
registration kept getting rejected (errors 30034 → 30909 → 30886). The Twilio
code path and its `NOTIFY_CHANNEL` switch were **removed** in favor of one
straightforward path; resurrect from git history if A2P ever clears. Email-to-text
via Gmail SMTP is free and works. T-Mobile (`@tmomail.net`) confirmed delivering;
Xfinity/Verizon (`@vtext.com`) unconfirmed.

**Send window, not exact hour.** GitHub cron fires 30–90 min late and sometimes
drops runs. So: five cron slots across the morning, each run sends only if the
*local* hour is in `[DELIVERY_HOUR, +DELIVERY_WINDOW_HOURS)` (08:00–11:59), and
the first successful send writes `.sent-marker`, cached per-day, so backstop runs
skip. Fixes an earlier bug where exact-hour matching silently sent nothing.

**Report is summary-only.** Previously itemized every purchase; simplified
2026-08-18 to the four-line summary above.

**"Yesterday" is often incomplete.** Plaid refreshes BoA only ~once/day on its
own schedule, so at 8 AM yesterday's charges may not have landed. Accepted
tradeoff — the fix needs the paid `transactions_refresh` add-on (~$3-4/mo), which
was considered and declined. MTD stays correct as data arrives.

**Spend = positive amounts only.** Plaid reports refunds and card payments as
negative; ignoring them keeps the total conservative.

**ASCII only in the message.** Keeps SMS in GSM-7 (160 chars/segment vs 70 for
Unicode).

## Setup

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env    # then fill it in
python link_card.py       # once — prints PLAID_ACCESS_TOKEN into .env
FORCE_SEND=1 python daily_report.py    # test
```

Push secrets to the cloud with `gh secret set -f .env`.

Plaid needs the product literally named **Transactions** (not Enrich) enabled in
production, plus Data Transparency Messaging configured under
`dashboard.plaid.com/link/data-transparency-v5`.

## Gotchas

- **Multiple sessions touch this repo.** Always `git fetch` and check
  `origin/main` before assuming local is current.
- Folder lives in OneDrive. If Git misbehaves on sync, the GitHub repo is the
  source of truth.
- `.env` is gitignored and has never been committed — repo is public.

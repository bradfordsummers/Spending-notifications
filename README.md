# Spending Notifications

A tiny automated system that texts you and your spouse one daily report:
what was spent on the Bank of America Alaska Airlines card yesterday, and how
the month is tracking against a **$9,000** budget.

- **Data:** Plaid (pulls the card's transactions)
- **Delivery:** Twilio SMS to both phones
- **Runs:** free GitHub Actions cron, ~8 AM Pacific, no server, no database

```
Spending — Sat Jul 19
Yesterday: $247.83
  • Costco $138.20
  • Shell $52.10
  • DoorDash $57.53

This month: $3,140 of $9,000 (35%)
$5,860 left · 12 days to go
```

---

## How it works

Every run is stateless: it just asks Plaid "what did this card spend from the
1st of the month through today?", totals it, and texts the result. No stored
balances to drift out of sync.

| File | Purpose |
|------|---------|
| `link_card.py` | One-time: connect the card via Plaid, get an access token |
| `daily_report.py` | The daily job: fetch → math → send |
| `notify.py` | Sends the SMS (swap this one file to change channels) |
| `config.py` | Reads settings from env / GitHub Secrets |
| `.github/workflows/daily.yml` | The cloud scheduler |

---

## Setup

### 0. Get the code running locally

```powershell
cd "C:\Users\bsummers\OneDrive - VOAWW\Desktop\VS Code Projects\Spending Notifications"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

### 1. Plaid  (get transaction access)

1. Sign up at <https://dashboard.plaid.com/signup> (free).
2. In the dashboard, go to **Developers → Keys**. Copy your **client_id** and
   the **Sandbox** secret. Put them in `.env` as `PLAID_CLIENT_ID` and
   `PLAID_SECRET`, and leave `PLAID_ENV=sandbox` for now.
3. **Test with fake data first:** run `python link_card.py`, click through, and
   log in with Plaid's sandbox credentials **user_good / pass_good**. Copy the
   printed `PLAID_ACCESS_TOKEN` into `.env`, then run `python daily_report.py`
   with `FORCE_SEND=1` to confirm a report prints.
4. **Go live:** in the dashboard request **Production access** (Developers →
   Keys → Production). Approval for a personal account is usually quick. Once
   approved, copy your **Production secret** into `.env`, set
   `PLAID_ENV=production`, and re-run `python link_card.py` — this time log into
   the **real** Bank of America and pick the Alaska Airlines card. Save the new
   production `PLAID_ACCESS_TOKEN`.

> Note: BoA uses Plaid's OAuth login, so you'll briefly bounce to Bank of
> America's own login page during linking. That's expected.

### 2. Twilio  (send the texts)

1. Sign up at <https://www.twilio.com/try-twilio>.
2. **Buy a phone number:** Console → Phone Numbers → Buy a number (pick one with
   SMS capability, ~$1.15/mo). Put it in `.env` as `TWILIO_FROM_NUMBER` in
   `+1XXXXXXXXXX` format.
3. From the Console dashboard copy **Account SID** and **Auth Token** into `.env`.
4. Put both cell numbers in `SMS_RECIPIENTS`, comma-separated, e.g.
   `+12065550001,+12065550002`.
5. **A2P 10DLC registration (required for US texting).** Console → Messaging →
   Regulatory Compliance → **A2P 10DLC**. Register a **Sole Proprietor** brand
   and a campaign (use case: *Account Notifications*). This is a one-time,
   few-dollar step and can take a day or two to approve. Until it's approved,
   messages to US numbers may be blocked.
   - *While you wait:* on a Twilio trial you can text **verified** numbers only.
     Verify both your phones under Phone Numbers → Verified Caller IDs to test
     end-to-end before A2P clears.

### 3. Deploy to GitHub Actions  (make it automatic)

1. Push this project to your repo:
   ```powershell
   git init
   git add .
   git commit -m "Spending notifications"
   git branch -M main
   git remote add origin https://github.com/bradfordsummers/Spending-notifications.git
   git push -u origin main
   ```
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**. Add one secret per row:

   | Secret | Value |
   |--------|-------|
   | `PLAID_CLIENT_ID` | from Plaid |
   | `PLAID_SECRET` | your **production** secret |
   | `PLAID_ENV` | `production` |
   | `PLAID_ACCESS_TOKEN` | from `link_card.py` (production) |
   | `TWILIO_ACCOUNT_SID` | from Twilio |
   | `TWILIO_AUTH_TOKEN` | from Twilio |
   | `TWILIO_FROM_NUMBER` | your Twilio number |
   | `SMS_RECIPIENTS` | both phones, comma-separated |
   | `MONTHLY_BUDGET` | `9000` |
   | `TIMEZONE` | `America/Los_Angeles` |
   | `DELIVERY_HOUR` | `8` |

3. **Test it:** repo → **Actions** tab → *Daily spending report* → **Run
   workflow**. It sends immediately (ignoring the 8 AM check). After that it
   runs itself every morning.

---

## Tuning

- **Change the budget/time:** edit the `MONTHLY_BUDGET` / `DELIVERY_HOUR`
  secrets — no code change.
- **Switch off SMS to Pushover/ntfy/email later:** rewrite `send_sms()` in
  `notify.py`; nothing else changes.

## Notes / limitations (v1)

- "Spend" = purchases only. Refunds and card payments (which Plaid reports as
  negative) are ignored, so the month total is slightly conservative.
- GitHub's scheduled runs can be delayed a few minutes under load; the report
  still goes out that morning.
- This project folder lives in OneDrive. If Git ever acts up on sync, move the
  working copy outside the OneDrive folder — the GitHub repo is the source of
  truth regardless.

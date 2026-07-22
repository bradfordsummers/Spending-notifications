"""The daily job: pull the card's transactions, do the budget math, send the SMS.

Run manually to test:   python daily_report.py
In the cloud it's run on a schedule by .github/workflows/daily.yml.
"""
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import (
    TransactionsGetRequestOptions,
)

import config
from plaid_client import make_client
from notify import send_sms


def _today_local() -> date:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def fetch_transactions(client, start: date, end: date):
    """Return all transactions for the card between start and end (inclusive)."""
    if not config.PLAID_ACCESS_TOKEN:
        raise SystemExit(
            "Missing PLAID_ACCESS_TOKEN. Run 'python link_card.py' first to "
            "connect the card, then save the token to .env / GitHub Secrets."
        )

    results = []
    offset = 0
    while True:
        req = TransactionsGetRequest(
            access_token=config.PLAID_ACCESS_TOKEN,
            start_date=start,
            end_date=end,
            options=TransactionsGetRequestOptions(count=500, offset=offset),
        )
        resp = client.transactions_get(req)
        results.extend(resp.transactions)
        if len(results) >= resp.total_transactions:
            break
        offset = len(results)
    return results


def _spend(transactions) -> float:
    """Sum of purchases. Plaid marks money-out as positive; card payments and
    refunds come through negative, so summing positives gives gross spend."""
    return sum(float(t.amount) for t in transactions if float(t.amount) > 0)


def _txn_date(t):
    """The real purchase date we bucket a transaction under. BoA leaves
    `authorized_date` null but populates `authorized_datetime` with the actual
    purchase moment, which stays fixed as the transaction moves pending ->
    posted (so a charge never lands on two different days). Fall back to
    `authorized_date`, then the posted `date`, if the datetime is missing."""
    adt = getattr(t, "authorized_datetime", None)
    if adt is not None:
        if adt.tzinfo is not None:
            adt = adt.astimezone(ZoneInfo(config.TIMEZONE))
        return adt.date()
    return getattr(t, "authorized_date", None) or t.date


def build_report(transactions, today: date) -> str:
    first_of_month = today.replace(day=1)
    yesterday = today - timedelta(days=1)

    # Bucket by purchase date, including pending (their purchase date is fixed,
    # so no charge is counted twice).
    yday_txns = [t for t in transactions if _txn_date(t) == yesterday]
    mtd_txns = [t for t in transactions if first_of_month <= _txn_date(t) <= today]

    yday_spend = _spend(yday_txns)
    mtd_spend = _spend(mtd_txns)

    budget = config.MONTHLY_BUDGET
    remaining = budget - mtd_spend
    pct = (mtd_spend / budget * 100) if budget else 0
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = days_in_month - today.day + 1

    # Plain ASCII only: keeps SMS in the cheap GSM-7 encoding (160 chars/segment
    # vs 70 for Unicode) and displays cleanly in the Windows console.
    # This becomes the email SUBJECT, which carrier gateways show as a "/ ... /"
    # title bar at the top of the text. Sent every morning, so no date needed.
    subject = "Spending"

    lines = [f"Yesterday: ${yday_spend:,.2f}"]

    # Every purchase from yesterday, largest first.
    purchases = sorted(
        (t for t in yday_txns if float(t.amount) > 0),
        key=lambda t: float(t.amount),
        reverse=True,
    )
    if purchases:
        for t in purchases:
            name = (t.merchant_name or t.name or "Unknown")[:24]
            lines.append(f"  - {name} ${float(t.amount):,.2f}")
    else:
        lines.append("  - No purchases")

    lines.append("")
    lines.append(
        f"This month: ${mtd_spend:,.0f} of ${budget:,.0f} ({pct:.0f}%)"
    )
    if remaining >= 0:
        lines.append(f"${remaining:,.0f} left, {days_left} days to go")
    else:
        lines.append(f"OVER by ${-remaining:,.0f}, {days_left} days to go")

    return subject, "\n".join(lines)


def main():
    today = _today_local()
    now_local = datetime.now(ZoneInfo(config.TIMEZONE))

    if not config.FORCE_SEND and now_local.hour != config.DELIVERY_HOUR:
        print(
            f"Local time is {now_local:%H:%M} {config.TIMEZONE}; delivery hour is "
            f"{config.DELIVERY_HOUR:02d}:00. Skipping (set FORCE_SEND=1 to override)."
        )
        return

    client = make_client()
    # Fetch a window covering both 'yesterday' and 'this month' in one call.
    first_of_month = today.replace(day=1)
    start = min(first_of_month, today - timedelta(days=1))
    transactions = fetch_transactions(client, start, today)

    subject, body = build_report(transactions, today)
    print(subject)
    print(body)
    print("-" * 40)

    if config.SMS_GATEWAYS:
        send_sms(body, subject)
        print(f"Sent to {len(config.SMS_GATEWAYS)} recipient(s).")
    else:
        print("No SMS_GATEWAYS set - printed only, nothing sent.")


if __name__ == "__main__":
    main()

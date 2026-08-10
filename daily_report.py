"""The daily job: pull the card's transactions, do the budget math, send the SMS.

Run manually to test:   python daily_report.py
In the cloud it's run on a schedule by .github/workflows/daily.yml.
"""
import calendar
import json
import os
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


def _txn_key(t):
    """A stable id for a purchase across its pending -> posted lifecycle. When a
    pending transaction posts it gets a NEW transaction_id, but the posted record
    carries `pending_transaction_id` pointing back at the pending one. Keying off
    that (when present) means the pending and posted records map to the SAME key,
    so a purchase is reported exactly once even though its id changes."""
    return getattr(t, "pending_transaction_id", None) or t.transaction_id


def _load_seen(path):
    """Return the dict of already-reported {key: purchase-date} or None if there
    is no state yet (first run). None vs {} matters: None means 'seed silently'."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_seen(path, seen, today):
    """Persist the reported-keys map, pruning anything older than 45 days so the
    file stays small. No-op when no path is configured."""
    if not path:
        return
    cutoff = (today - timedelta(days=45)).isoformat()
    pruned = {k: d for k, d in seen.items() if d >= cutoff}
    try:
        with open(path, "w") as f:
            json.dump(pruned, f)
    except OSError as e:  # pragma: no cover - best effort
        print(f"  (could not write seen-state {path}: {e})")


def build_report(transactions, new_txns, today: date, first_run: bool):
    first_of_month = today.replace(day=1)

    # Month-to-date total is bucketed by purchase date over everything currently
    # known (pending included) so the budget figure is as current as possible.
    mtd_txns = [t for t in transactions if first_of_month <= _txn_date(t) <= today]
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

    lines = []
    if first_run:
        # No state yet: don't dump weeks of history. Seed silently and start
        # reporting new charges tomorrow.
        lines.append("Now tracking your card.")
        lines.append("New charges will appear here each morning.")
    else:
        # New charges = everything that showed up since the last report,
        # regardless of when it was purchased or whether it's pending/posted.
        new_purchases = sorted(
            (t for t in new_txns if float(t.amount) > 0),
            key=lambda t: float(t.amount),
            reverse=True,
        )
        new_spend = sum(float(t.amount) for t in new_purchases)
        lines.append(f"New charges: ${new_spend:,.2f}")
        if new_purchases:
            for t in new_purchases:
                name = (t.merchant_name or t.name or "Unknown")[:24]
                d = _txn_date(t)
                lines.append(f"  - {name} ${float(t.amount):,.2f} ({d:%b} {d.day})")
        else:
            lines.append("  - None since last report")

    lines.append("")
    lines.append(
        f"This month: ${mtd_spend:,.0f} of ${budget:,.0f} ({pct:.0f}%)"
    )
    if remaining >= 0:
        lines.append(f"${remaining:,.0f} left, {days_left} days to go")
    else:
        lines.append(f"OVER by ${-remaining:,.0f}, {days_left} days to go")

    return subject, "\n".join(lines)


def _mark_sent(today: date) -> None:
    """Record that today's report went out, so the workflow's backstop runs
    skip. No-op unless SENT_MARKER_FILE is set (i.e. only in the cloud)."""
    if not config.SENT_MARKER_FILE:
        return
    try:
        with open(config.SENT_MARKER_FILE, "w") as f:
            f.write(str(today))
    except OSError as e:  # pragma: no cover - best effort
        print(f"  (could not write sent-marker {config.SENT_MARKER_FILE}: {e})")


def main():
    now_local = datetime.now(ZoneInfo(config.TIMEZONE))
    today = now_local.date()

    # Send on ANY run at or after the delivery hour, within a morning window --
    # not only when the hour matches exactly. GitHub Actions fires cron jobs
    # late, so an 8 AM job may not run until 9 or 10; ">=" lets that late run
    # still send. The workflow's once-per-day cache guard stops the backstop
    # runs from sending a duplicate.
    window_end = config.DELIVERY_HOUR + config.DELIVERY_WINDOW_HOURS
    if not config.FORCE_SEND and not (
        config.DELIVERY_HOUR <= now_local.hour < window_end
    ):
        print(
            f"Local time is {now_local:%H:%M} {config.TIMEZONE}; outside the "
            f"{config.DELIVERY_HOUR:02d}:00-{window_end:02d}:00 send window. "
            f"Skipping (set FORCE_SEND=1 to override)."
        )
        return

    client = make_client()
    # Fetch a wide window (35 days) so late-arriving charges are seen no matter
    # how long the bank took to report them; covers this month for the total too.
    start = today - timedelta(days=35)
    transactions = fetch_transactions(client, start, today)

    # "New" = charges whose stable key we haven't reported before. This is what
    # makes the daily list immune to the bank's ingest lag and weekends: a charge
    # is listed the first morning it appears, whenever that is, and never again.
    seen = _load_seen(config.SEEN_STATE_FILE)
    first_run = seen is None
    seen = seen or {}
    new_txns = [t for t in transactions if _txn_key(t) not in seen]

    subject, body = build_report(transactions, new_txns, today, first_run)
    print(subject)
    print(body)
    print("-" * 40)

    recipients = config.SMS_GATEWAYS or config.SMS_RECIPIENTS
    if not recipients:
        print("No recipients configured - printed only, nothing sent.")
        return

    send_sms(body, subject)
    print(f"Sent to {len(recipients)} recipient(s).")

    # Persist state only on real scheduled sends, not manual FORCE_SEND tests --
    # so a test run neither suppresses the morning send nor consumes 'new' charges.
    if not config.FORCE_SEND:
        for t in transactions:
            seen[_txn_key(t)] = _txn_date(t).isoformat()
        _save_seen(config.SEEN_STATE_FILE, seen, today)
        _mark_sent(today)


if __name__ == "__main__":
    main()

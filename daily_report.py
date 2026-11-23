"""The daily job: pull the card's transactions, build today's report, send it.

Which report goes out depends on the date (one text per day either way):
  the 2nd of the month -> monthly summary text + detailed report by email
  Mondays              -> weekly summary
  every other day      -> the short daily

Run manually to test:   python daily_report.py
Preview without sending: DRY_RUN=1 REPORT=weekly python daily_report.py
In the cloud it's run on a schedule by .github/workflows/daily.yml.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import (
    TransactionsGetRequestOptions,
)

import config
from plaid_client import make_client
import reports
from notify import recipients, send_email, send_sms


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


def pick_report(today: date) -> str:
    if config.REPORT in ("daily", "weekly", "monthly"):
        return config.REPORT
    if today.day == 2:
        # The 2nd, not the 1st: BoA data lands about a day late, so on the 1st
        # the last day of the month is often still missing.
        return "monthly"
    if today.weekday() == 0:
        return "weekly"
    return "daily"


def fetch_start(report: str, today: date) -> date:
    first_of_month = today.replace(day=1)
    if report == "monthly":
        # Last month plus the month before it, for the comparison.
        return reports.prev_month(reports.prev_month(today)[0])[0]
    if report == "weekly":
        return min(first_of_month, reports.last_week(today)[0])
    return min(first_of_month, today - timedelta(days=1))


def main():
    now_local = datetime.now(ZoneInfo(config.TIMEZONE))
    today = now_local.date()

    # Send on ANY run inside the local send window, not only when the hour
    # matches exactly: GitHub fires cron jobs late. The workflow's once-per-day
    # cache guard stops the backstop runs from sending a duplicate.
    window_end = config.DELIVERY_HOUR + config.DELIVERY_WINDOW_HOURS
    if not (config.FORCE_SEND or config.DRY_RUN) and not (
        config.DELIVERY_HOUR <= now_local.hour < window_end
    ):
        print(
            f"Local time is {now_local:%H:%M} {config.TIMEZONE}; outside the "
            f"{config.DELIVERY_HOUR:02d}:00-{window_end:02d}:00 send window. "
            f"Skipping (set FORCE_SEND=1 to override)."
        )
        return

    report = pick_report(today)
    client = make_client()
    transactions = fetch_transactions(client, fetch_start(report, today), today)

    email = None
    if report == "monthly":
        email = reports.build_monthly_email(transactions, today)
        subject, body = reports.build_monthly_text(
            transactions, today, emailed=bool(config.REPORT_EMAIL_TO)
        )
    elif report == "weekly":
        subject, body = reports.build_weekly(transactions, today)
    else:
        subject, body = reports.build_daily(transactions, today)

    if config.DRY_RUN:
        print(f"[DRY RUN - nothing sent] {report} report")
        print(f"--- text ({len(body)} chars) ---")
        print(config.SMS_PROGRAM_NAME)
        print(body.lstrip("\n"))
        if email:
            e_subject, e_text, _, csv_bytes, csv_name = email
            rows = csv_bytes.count(b"\n") - 1
            print(f"--- email: {e_subject} (attachment {csv_name}, {rows} transactions) ---")
            print(e_text)
        return

    # The repo is public, so its Actions logs are too: only print the report
    # itself when running locally.
    if config.IN_CI:
        print(f"Built {report} report ({len(body)} chars).")
    else:
        print(subject)
        print(body)
        print("-" * 40)

    if not recipients():
        print(f"No recipients set for channel '{config.DELIVERY_CHANNEL}' - nothing sent.")
        return

    send_sms(body, subject)
    print(f"Sent {report} report to {len(recipients())} recipient(s) via {config.DELIVERY_CHANNEL}.")

    email_failed = False
    if email and config.REPORT_EMAIL_TO:
        e_subject, e_text, e_html, csv_bytes, csv_name = email
        try:
            send_email(config.REPORT_EMAIL_TO, e_subject, e_text, e_html,
                       [(csv_name, csv_bytes, "text", "csv")])
            print(f"Emailed monthly report to {len(config.REPORT_EMAIL_TO)} address(es).")
        except Exception as e:  # noqa: BLE001 - text already went out
            print(f"!! Monthly report email failed: {e}")
            email_failed = True

    # Mark the day done so the backstop runs skip (even if only the email
    # failed: re-running would text everyone again). Skip for manual tests.
    if not config.FORCE_SEND:
        _mark_sent(today)
    if email_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

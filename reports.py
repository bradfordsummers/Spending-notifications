"""Report builders: the daily text, the Monday weekly text, and the monthly
text + emailed report (HTML summary with a CSV of every transaction).

Each text builder returns (subject, body). Bodies are plain ASCII so SMS stays
in GSM-7 (160 chars/segment vs 70 for Unicode).
"""
import calendar
import csv
import html
import io
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import config

# --- what counts as spending ----------------------------------------------

# Plaid categories that are money movement, not spending: account transfers and
# credit-card payments. These can arrive as POSITIVE amounts (e.g. a $6,000 card
# payment once did) and would otherwise inflate the total, so they're excluded.
_NON_SPEND = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"}
# ...except these, which pay for real purchases and DO count as spending:
# Venmo/app payments and buy-now-pay-later installments (Afterpay, Klarna).
_COUNTS_AS_SPEND = {"TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "LOAN_PAYMENTS_BNPL"}


def _pfc(t):
    pfc = getattr(t, "personal_finance_category", None)
    if not pfc:
        return None, None
    return pfc.primary, pfc.detailed


def is_purchase(t) -> bool:
    """True for spending: a positive amount that isn't a transfer or card
    payment. Refunds come through negative and are excluded by the sign."""
    if float(t.amount) <= 0:
        return False
    primary, detailed = _pfc(t)
    if not primary:
        return True
    return primary not in _NON_SPEND or detailed in _COUNTS_AS_SPEND


def is_refund(t) -> bool:
    """A credit back from a merchant (negative amount, not a payment/transfer)."""
    primary, _ = _pfc(t)
    return float(t.amount) < 0 and primary not in _NON_SPEND | {"INCOME"}


def spend(transactions) -> float:
    """Sum of actual purchases (excludes transfers and payments)."""
    return sum(float(t.amount) for t in transactions if is_purchase(t))


def txn_date(t):
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


def vendor(t) -> str:
    return (getattr(t, "merchant_name", None) or t.name or "Unknown").strip()


def category(t) -> str:
    """Short, friendly category name from Plaid's personal_finance_category."""
    primary, detailed = _pfc(t)
    if not primary:
        return "Other"
    if detailed == "FOOD_AND_DRINK_GROCERIES":
        return "Groceries"
    if detailed == "TRANSPORTATION_GAS":
        return "Gas"
    if detailed == "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS":
        return "Venmo / app payments"
    if detailed == "LOAN_PAYMENTS_BNPL":
        return "Buy now, pay later"
    return _CATEGORY_NAMES.get(primary, primary.replace("_", " ").title())


_CATEGORY_NAMES = {
    "FOOD_AND_DRINK": "Dining",
    "GENERAL_MERCHANDISE": "Shopping",
    "GENERAL_SERVICES": "Services",
    "TRANSPORTATION": "Transportation",
    "TRAVEL": "Travel",
    "ENTERTAINMENT": "Entertainment",
    "PERSONAL_CARE": "Personal care",
    "MEDICAL": "Medical",
    "HOME_IMPROVEMENT": "Home",
    "RENT_AND_UTILITIES": "Rent & utilities",
    "GOVERNMENT_AND_NON_PROFIT": "Government & nonprofit",
    "BANK_FEES": "Bank fees",
}


# --- helpers --------------------------------------------------------------

def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _short(s: str, n: int = 20) -> str:
    s = _ascii(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "."


def purchases_between(transactions, start: date, end: date):
    return [t for t in transactions if is_purchase(t) and start <= txn_date(t) <= end]


def top_purchases(purchases, n):
    return sorted(purchases, key=lambda t: -float(t.amount))[:n]


def vendor_stats(purchases):
    """[(vendor, count, total)] across the given purchases."""
    stats = defaultdict(lambda: [0, 0.0])
    for t in purchases:
        s = stats[vendor(t)]
        s[0] += 1
        s[1] += float(t.amount)
    return [(v, c, tot) for v, (c, tot) in stats.items()]


def most_frequent(purchases, n):
    return sorted(vendor_stats(purchases), key=lambda v: (-v[1], -v[2]))[:n]


def top_by_spend(purchases, n):
    return sorted(vendor_stats(purchases), key=lambda v: -v[2])[:n]


def _span(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start:%b} {start.day}-{end.day}"
    return f"{start:%b} {start.day}-{end:%b} {end.day}"


def _month_bounds(d: date):
    first = d.replace(day=1)
    last = d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return first, last


def prev_month(d: date):
    """(first, last) of the month before the one containing d."""
    return _month_bounds(d.replace(day=1) - timedelta(days=1))


def last_week(today: date):
    """(Monday, Sunday) of the most recent full Mon-Sun week before today."""
    start = today - timedelta(days=today.weekday() + 7)
    return start, start + timedelta(days=6)


def _purchase_lines(purchases):
    return [f"{_short(vendor(t))} ${float(t.amount):,.2f}" for t in purchases]


def _vendor_lines(stats):
    return [f"{_short(v)} {c}x ${tot:,.2f}" for v, c, tot in stats]


# --- daily ----------------------------------------------------------------

def build_daily(transactions, today: date):
    first_of_month = today.replace(day=1)
    yesterday = today - timedelta(days=1)

    # Bucket by purchase date. Banks report charges on a delay, so "yesterday"
    # may be incomplete at send time -- an accepted tradeoff.
    yday_spend = spend(t for t in transactions if txn_date(t) == yesterday)
    mtd_spend = spend(t for t in transactions if first_of_month <= txn_date(t) <= today)

    budget = config.MONTHLY_BUDGET
    remaining = budget - mtd_spend
    pct = (mtd_spend / budget * 100) if budget else 0
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = days_in_month - today.day + 1

    # The subject is the "/ ... /" title on email-to-SMS; Twilio texts use the
    # program name instead. Blank first line puts a break under that title.
    lines = [""]
    if remaining >= 0:
        lines.append(f"${remaining:,.0f} left")
    else:
        lines.append(f"${-remaining:,.0f} over budget")
    lines.append(f"{days_left} days to go")
    lines.append(f"{pct:.0f}% of ${budget:,.0f} budget")
    lines.append(f"This month: ${mtd_spend:,.0f}")
    lines.append(f"Yesterday: ${yday_spend:,.2f}")
    return "Spending", "\n".join(lines)


# --- weekly (Mondays) -----------------------------------------------------

def build_weekly(transactions, today: date):
    """Last Mon-Sun: total, where the month stands now, top 5 purchases, and
    top 5 vendors by number of purchases."""
    start, end = last_week(today)
    week = purchases_between(transactions, start, end)
    first_of_month = today.replace(day=1)
    mtd = spend(t for t in transactions if first_of_month <= txn_date(t) <= today)
    budget = config.MONTHLY_BUDGET
    pct = (mtd / budget * 100) if budget else 0

    lines = [
        "",
        "Weekly summary",
        f"Last week ({_span(start, end)}): ${spend(week):,.0f}",
        f"Month so far: {pct:.0f}% of ${budget:,.0f}",
        "",
        "Top purchases:",
        *_purchase_lines(top_purchases(week, 5)),
        "",
        "Top vendors:",
        *_vendor_lines(most_frequent(week, 5)),
    ]
    return "Weekly spending", "\n".join(lines)


# --- monthly (the 2nd) ----------------------------------------------------

def build_monthly_text(transactions, today: date, emailed: bool):
    """Last month vs budget, top 10 purchases, top 10 vendors by frequency."""
    start, end = prev_month(today)
    month = purchases_between(transactions, start, end)
    total = spend(month)
    budget = config.MONTHLY_BUDGET
    pct = (total / budget * 100) if budget else 0
    diff = budget - total
    standing = f"${diff:,.0f} under budget" if diff >= 0 else f"${-diff:,.0f} over budget"

    lines = [
        "",
        f"{start:%B} summary",
        f"Spent ${total:,.0f} of ${budget:,.0f} ({pct:.0f}%)",
        standing,
        "",
        "Top 10 purchases:",
        *_purchase_lines(top_purchases(month, 10)),
        "",
        "Top 10 vendors:",
        *_vendor_lines(most_frequent(month, 10)),
    ]
    if emailed:
        lines += ["", "Full report sent to your email."]
    return f"{start:%B} spending", "\n".join(lines)


def build_monthly_email(transactions, today: date):
    """The detailed monthly report. Returns (subject, text, html, csv_bytes,
    csv_filename). `transactions` must reach back to the month before last so
    the report can compare against it."""
    start, end = prev_month(today)
    pstart, pend = prev_month(start)
    in_month = [t for t in transactions if start <= txn_date(t) <= end]
    month = [t for t in in_month if is_purchase(t)]
    prior_total = spend(purchases_between(transactions, pstart, pend))

    budget = config.MONTHLY_BUDGET
    total = spend(month)
    pct = (total / budget * 100) if budget else 0
    diff = budget - total
    refunds = -sum(float(t.amount) for t in in_month if is_refund(t))
    by_day = defaultdict(float)
    for t in month:
        by_day[txn_date(t)] += float(t.amount)
    big_day = max(by_day.items(), key=lambda kv: kv[1]) if by_day else None

    # Mon-Sun weeks, clipped to the month.
    weeks = []
    ws = start
    while ws <= end:
        we = min(ws + timedelta(days=6 - ws.weekday()), end)
        weeks.append((ws, we, spend(t for t in month if ws <= txn_date(t) <= we)))
        ws = we + timedelta(days=1)

    cats = defaultdict(lambda: [0, 0.0])
    for t in month:
        c = cats[category(t)]
        c[0] += 1
        c[1] += float(t.amount)
    cats = sorted(cats.items(), key=lambda kv: -kv[1][1])

    top20 = top_purchases(month, 20)
    by_spend = top_by_spend(month, 15)
    frequent = most_frequent(month, 15)

    label = f"{start:%B %Y}"
    subject = f"Spending report: {label}"
    standing = f"${diff:,.2f} under budget" if diff >= 0 else f"${-diff:,.2f} over budget"
    change = total - prior_total
    change_txt = (
        f"{'up' if change >= 0 else 'down'} ${abs(change):,.2f} from {pstart:%B} (${prior_total:,.2f})"
    )

    headline = [
        ("Spent", f"${total:,.2f} of ${budget:,.0f} ({pct:.0f}%)"),
        ("Result", standing),
        ("Vs last month", change_txt),
        ("Purchases", f"{len(month)} (average ${total / len(month):,.2f})" if month else "0"),
        ("Refunds/credits", f"${refunds:,.2f}"),
    ]
    if big_day:
        headline.append(("Biggest day", f"{big_day[0]:%a %b} {big_day[0].day}: ${big_day[1]:,.2f}"))

    # Plain-text version (email fallback, and what a dry run prints).
    text = [f"Spending report: {label}", ""]
    text += [f"{k}: {v}" for k, v in headline]
    text += ["", "By week:"] + [f"  {_span(a, b)}: ${s:,.2f}" for a, b, s in weeks]
    text += ["", "By category:"] + [
        f"  {name}: ${tot:,.2f} ({n} purchases, {tot / total * 100 if total else 0:.0f}%)"
        for name, (n, tot) in cats
    ]
    text += ["", "Top 20 purchases:"] + [
        f"  {txn_date(t):%b} {txn_date(t).day}  {vendor(t)}  ${float(t.amount):,.2f}" for t in top20
    ]
    text += ["", "Top vendors by spend:"] + [f"  {v}: ${tot:,.2f} ({c}x)" for v, c, tot in by_spend]
    text += ["", "Most frequent vendors:"] + [f"  {v}: {c}x, ${tot:,.2f}" for v, c, tot in frequent]
    text += ["", "Every transaction is in the attached CSV."]

    csv_bytes = _transactions_csv(in_month)
    page = _html_report(label, headline, weeks, cats, total, top20, by_spend, frequent)
    filename = f"transactions-{start:%Y-%m}.csv"
    return subject, "\n".join(text), page, csv_bytes, filename


def _transactions_csv(transactions) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Purchase date", "Posted date", "Merchant", "Description", "Amount",
        "Category", "Plaid category", "Pending", "Counted as spending",
    ])
    for t in sorted(transactions, key=lambda t: (txn_date(t), -float(t.amount))):
        _, detailed = _pfc(t)
        w.writerow([
            txn_date(t).isoformat(), t.date.isoformat() if t.date else "",
            vendor(t), t.name or "", f"{float(t.amount):.2f}", category(t),
            detailed or "", "yes" if getattr(t, "pending", False) else "no",
            "yes" if is_purchase(t) else "no",
        ])
    # BOM so Excel opens it as UTF-8.
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _html_report(label, headline, weeks, cats, total, top20, by_spend, frequent) -> str:
    e = html.escape
    td = 'style="padding:4px 10px;border-bottom:1px solid #eee"'
    tdr = 'style="padding:4px 10px;border-bottom:1px solid #eee;text-align:right"'

    def table(headers, rows, right_from=1):
        head = "".join(
            f'<th style="text-align:{"right" if i >= right_from else "left"};padding:4px 10px;'
            f'border-bottom:2px solid #ccc">{e(h)}</th>'
            for i, h in enumerate(headers)
        )
        body = "".join(
            "<tr>" + "".join(
                f"<td {tdr if i >= right_from else td}>{c}</td>" for i, c in enumerate(r)
            ) + "</tr>"
            for r in rows
        )
        return f'<table style="border-collapse:collapse;width:100%;font-size:14px">{head}{body}</table>'

    def bar(share):
        w = max(1, round(share * 100))
        return (f'<div style="background:#e8eef7;width:120px;height:10px;display:inline-block">'
                f'<div style="background:#3366cc;width:{w}%;height:10px"></div></div>')

    def h2(s):
        return f'<h2 style="font-size:17px;margin:26px 0 8px">{e(s)}</h2>'

    top_week = max((s for _, _, s in weeks), default=0) or 1
    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px;color:#222">',
        f'<h1 style="font-size:22px;margin:0 0 12px">Spending report: {e(label)}</h1>',
        table(["", ""], [(f"<b>{e(k)}</b>", e(v)) for k, v in headline], right_from=9),
        h2("By week"),
        table(["Week", "Spent", ""], [
            (e(_span(a, b)), f"${s:,.2f}", bar(s / top_week)) for a, b, s in weeks
        ]),
        h2("By category"),
        table(["Category", "Purchases", "Spent", "Share", ""], [
            (e(name), n, f"${tot:,.2f}", f"{tot / total * 100 if total else 0:.0f}%",
             bar(tot / total if total else 0))
            for name, (n, tot) in cats
        ]),
        h2("Top 20 purchases"),
        table(["Date", "Vendor", "Category", "Amount"], [
            (f"{txn_date(t):%b} {txn_date(t).day}", e(vendor(t)), e(category(t)),
             f"${float(t.amount):,.2f}")
            for t in top20
        ], right_from=3),
        h2("Top vendors by spend"),
        table(["Vendor", "Purchases", "Spent"], [(e(v), c, f"${tot:,.2f}") for v, c, tot in by_spend]),
        h2("Most frequent vendors"),
        table(["Vendor", "Purchases", "Spent"], [(e(v), c, f"${tot:,.2f}") for v, c, tot in frequent]),
        '<p style="font-size:13px;color:#666;margin-top:24px">Every transaction for the month, '
        'including payments and transfers that don\'t count as spending, is in the attached CSV. '
        'Purchases are dated by when they were made, so totals can differ slightly from the bank '
        'statement at month edges.</p>',
        "</div>",
    ]
    return "".join(parts)

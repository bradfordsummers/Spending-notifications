"""Central configuration, loaded from environment variables.

Locally, values are read from a .env file (see .env.example).
In GitHub Actions, they come from repository Secrets.
"""
import os

from dotenv import load_dotenv

# Load .env when running locally. In CI there is no .env file and this is a no-op.
load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required environment variable: {name}\n"
            f"Set it in your .env file (local) or in GitHub repo Secrets (cloud)."
        )
    return value


# --- Plaid ---
PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET = os.environ.get("PLAID_SECRET", "")
# "sandbox" for testing with fake data, "production" for your real BoA card.
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox").lower()
# Saved by link_card.py after you connect the card once.
PLAID_ACCESS_TOKEN = os.environ.get("PLAID_ACCESS_TOKEN", "")

# --- Delivery: email-to-SMS gateway ---
# We email each carrier's SMS gateway and it arrives as a normal text.
# EMAIL_FROM is a Gmail address; EMAIL_APP_PASSWORD is a Gmail App Password
# (not your normal login password — see README).
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
# Comma-separated carrier gateway addresses. Examples:
#   T-Mobile:  5551234567@tmomail.net
#   AT&T:      5551234567@txt.att.net
#   Verizon:   5551234567@vtext.com
SMS_GATEWAYS = [
    a.strip() for a in os.environ.get("SMS_GATEWAYS", "").split(",") if a.strip()
]

# --- Budget & schedule ---
MONTHLY_BUDGET = float(os.environ.get("MONTHLY_BUDGET", "9000"))
TIMEZONE = os.environ.get("TIMEZONE", "America/Los_Angeles")
# The local hour the report should go out. The workflow fires a few times
# around this time in UTC; daily_report.py checks the local hour so DST never
# shifts it and a late run still counts.
DELIVERY_HOUR = int(os.environ.get("DELIVERY_HOUR", "8"))
# How many hours past DELIVERY_HOUR we'll still send. GitHub Actions fires cron
# jobs late (often 30-90 min), so we accept any morning run in the window
# [DELIVERY_HOUR, DELIVERY_HOUR + WINDOW). Default 4 => 08:00-11:59 local.
DELIVERY_WINDOW_HOURS = int(os.environ.get("DELIVERY_WINDOW_HOURS", "4"))
# If "1", skip the delivery-window guard (used for manual test runs).
FORCE_SEND = os.environ.get("FORCE_SEND", "0") == "1"
# When set, a successful scheduled send writes this file. The workflow caches
# it per day so the backstop runs know the report already went out and don't
# send a duplicate. Empty for local runs (no dedupe needed).
SENT_MARKER_FILE = os.environ.get("SENT_MARKER_FILE", "")


def require_plaid():
    return _require("PLAID_CLIENT_ID"), _require("PLAID_SECRET")

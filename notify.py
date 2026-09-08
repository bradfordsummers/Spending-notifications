"""Delivers the daily report as a text via carrier email-to-SMS gateways.

Each US carrier accepts email at a special address and delivers it to the phone
as a normal text (e.g. 5551234567@tmomail.net for T-Mobile). We send from a
Gmail account over SMTP and it arrives in the native Messages app -- no Twilio,
no A2P registration, no app to install.
"""
import smtplib
import ssl
import time
from email.message import EmailMessage

import config

# Retry transient failures (e.g. a gateway "temporarily unavailable") a couple
# times with a short backoff before giving up on a recipient.
_MAX_ATTEMPTS = 3
_RETRY_SECONDS = 5


def send_sms(body: str, subject: str = "") -> None:
    if not config.EMAIL_FROM or not config.EMAIL_APP_PASSWORD:
        raise SystemExit(
            "Missing EMAIL_FROM / EMAIL_APP_PASSWORD. Set your Gmail address and "
            "a Gmail App Password in .env (see README)."
        )
    if not config.SMS_GATEWAYS:
        raise SystemExit("No SMS_GATEWAYS configured (e.g. 5551234567@tmomail.net).")

    context = ssl.create_default_context()
    failures = sum(
        0 if _send_one(context, to_addr, subject, body) else 1
        for to_addr in config.SMS_GATEWAYS
    )

    total = len(config.SMS_GATEWAYS)
    # Only fail the run if EVERY recipient failed -- then nothing was delivered,
    # so the cloud job should error and let a backstop run retry. If at least one
    # recipient got it, succeed so the day is marked done and the working
    # recipients aren't re-texted by later runs.
    if failures == total:
        raise SystemExit(f"All {total} send(s) failed.")
    if failures:
        print(f"  ({failures} of {total} failed; {total - failures} delivered)")


def _send_one(context, to_addr, subject, body) -> bool:
    """Send to one gateway, retrying on error. Returns True on success. A fresh
    SMTP connection per attempt so a dropped/deferred session recovers."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with smtplib.SMTP_SSL(config.EMAIL_HOST, config.EMAIL_PORT, context=context) as server:
                server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
                msg = EmailMessage()
                msg["From"] = config.EMAIL_FROM
                msg["To"] = to_addr
                # Carrier gateways require a subject and show it as a "/ ... /"
                # title bar atop the text, so we pass the report's title here.
                msg["Subject"] = subject
                msg.set_content(body)
                server.send_message(msg)
            print(f"  -> {to_addr}: sent")
            return True
        except Exception as e:  # noqa: BLE001 - retry then report
            if attempt < _MAX_ATTEMPTS:
                print(f"  .. {to_addr}: attempt {attempt} failed ({e}); retrying")
                time.sleep(_RETRY_SECONDS)
            else:
                print(f"  !! {to_addr}: FAILED after {attempt} attempts - {e}")
    return False

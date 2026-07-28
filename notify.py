"""Delivers the daily report to phones via a pluggable channel.

Two interchangeable channels, selected by NOTIFY_CHANNEL in .env:
  - "email"  : carrier email-to-SMS gateway via Gmail SMTP. No Twilio, no A2P,
               no app -- but best-effort (carriers may throttle/defer).
  - "twilio" : Twilio SMS API. Requires A2P 10DLC approval to reach US phones,
               but is a real API with delivery guarantees (better long-term).

This is the ONLY file that knows about delivery. To add another channel,
write a _send_* function and route to it in send_sms().
"""
import smtplib
import ssl
import time
from email.message import EmailMessage

import config

# Retry transient failures (e.g. gateway "temporarily unavailable") a couple
# times with a short backoff before giving up on a recipient.
_MAX_ATTEMPTS = 3
_RETRY_SECONDS = 5


def send_sms(body: str, subject: str = "") -> None:
    """Send the report using whichever channel NOTIFY_CHANNEL selects."""
    if config.NOTIFY_CHANNEL == "twilio":
        _send_twilio(body, subject)
    elif config.NOTIFY_CHANNEL == "email":
        _send_email_gateway(body, subject)
    else:
        raise SystemExit(
            f"Unknown NOTIFY_CHANNEL {config.NOTIFY_CHANNEL!r} "
            f"(use 'email' or 'twilio')."
        )


def _with_retry(label: str, attempt_fn) -> bool:
    """Run attempt_fn(), retrying on exceptions. Returns True on success."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            attempt_fn()
            print(f"  -> {label}: sent")
            return True
        except Exception as e:  # noqa: BLE001 - retry then report
            if attempt < _MAX_ATTEMPTS:
                print(f"  .. {label}: attempt {attempt} failed ({e}); retrying")
                time.sleep(_RETRY_SECONDS)
            else:
                print(f"  !! {label}: FAILED after {attempt} attempts - {e}")
    return False


# --- Channel: email-to-SMS gateway ---------------------------------------

def _send_email_gateway(body: str, subject: str) -> None:
    if not config.EMAIL_FROM or not config.EMAIL_APP_PASSWORD:
        raise SystemExit(
            "Missing EMAIL_FROM / EMAIL_APP_PASSWORD. Set your Gmail address and "
            "a Gmail App Password in .env (see README)."
        )
    if not config.SMS_GATEWAYS:
        raise SystemExit("No SMS_GATEWAYS configured (e.g. 5551234567@tmomail.net).")

    context = ssl.create_default_context()
    failures = 0
    for to_addr in config.SMS_GATEWAYS:
        # Reconnect per attempt so a dropped/deferred SMTP session recovers.
        def attempt(to_addr=to_addr):
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

        if not _with_retry(to_addr, attempt):
            failures += 1

    total = len(config.SMS_GATEWAYS)
    # Only fail the run if EVERY recipient failed -- then nothing was delivered,
    # so the cloud job should error and let a backstop run retry. If at least
    # one recipient got it, succeed so the day is marked done and the working
    # recipients aren't re-texted by later runs.
    if failures == total:
        raise SystemExit(f"All {total} send(s) failed.")
    if failures:
        print(f"  ({failures} of {total} failed; {total - failures} delivered)")


# --- Channel: Twilio ------------------------------------------------------

def _send_twilio(body: str, subject: str) -> None:
    missing = [
        name
        for name, val in [
            ("TWILIO_ACCOUNT_SID", config.TWILIO_ACCOUNT_SID),
            ("TWILIO_AUTH_TOKEN", config.TWILIO_AUTH_TOKEN),
            ("TWILIO_FROM_NUMBER", config.TWILIO_FROM_NUMBER),
        ]
        if not val
    ]
    if missing:
        raise SystemExit("Missing Twilio settings: " + ", ".join(missing))
    if not config.SMS_RECIPIENTS:
        raise SystemExit("No SMS_RECIPIENTS configured.")

    # Imported lazily so the 'email' channel doesn't require the twilio package.
    from twilio.rest import Client

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    # Twilio SMS has no subject line; put the title as the first body line.
    text = f"{subject}\n{body}" if subject else body

    failures = 0
    for to_number in config.SMS_RECIPIENTS:
        def attempt(to_number=to_number):
            client.messages.create(
                body=text,
                from_=config.TWILIO_FROM_NUMBER,
                to=to_number,
            )

        if not _with_retry(to_number, attempt):
            failures += 1

    total = len(config.SMS_RECIPIENTS)
    # Only fail the run if EVERY recipient failed (see email channel note).
    if failures == total:
        raise SystemExit(f"All {total} send(s) failed.")
    if failures:
        print(f"  ({failures} of {total} failed; {total - failures} delivered)")

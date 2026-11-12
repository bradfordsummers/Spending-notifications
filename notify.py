"""Delivers the daily report as a text.

Two channels, picked by DELIVERY_CHANNEL:

  email  (default) Carrier email-to-SMS gateways over Gmail SMTP, e.g.
         5551234567@tmomail.net. Free, but carriers are shutting these down
         (Verizon vtext.com is mostly dead as of late 2026).
  twilio Real SMS through a Twilio Messaging Service. Needs an approved A2P
         10DLC campaign; until then US carriers block the messages.
"""
import base64
import json
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

import config

# Retry transient failures a couple times with a short backoff before giving up
# on a recipient.
_MAX_ATTEMPTS = 3
_RETRY_SECONDS = 5


def recipients() -> list[str]:
    """Who the configured channel sends to."""
    if config.DELIVERY_CHANNEL == "twilio":
        return config.SMS_TO
    return config.SMS_GATEWAYS


def send_sms(body: str, subject: str = "") -> None:
    if config.DELIVERY_CHANNEL == "twilio":
        _check_twilio_config()
        send_one = lambda to: _send_twilio(to, subject, body)  # noqa: E731
    else:
        _check_email_config()
        context = ssl.create_default_context()
        send_one = lambda to: _send_email(context, to, subject, body)  # noqa: E731

    targets = recipients()
    failures = sum(0 if send_one(to) else 1 for to in targets)

    total = len(targets)
    # Only fail the run if EVERY recipient failed. Then nothing was delivered,
    # so the cloud job should error and let a backstop run retry. If at least one
    # recipient got it, succeed so the day is marked done and the working
    # recipients aren't re-texted by later runs.
    if failures == total:
        raise SystemExit(f"All {total} send(s) failed.")
    if failures:
        print(f"  ({failures} of {total} failed; {total - failures} delivered)")


# --- email-to-SMS gateway -------------------------------------------------

def _check_email_config() -> None:
    if not config.EMAIL_FROM or not config.EMAIL_APP_PASSWORD:
        raise SystemExit(
            "Missing EMAIL_FROM / EMAIL_APP_PASSWORD. Set your Gmail address and "
            "a Gmail App Password in .env (see README)."
        )
    if not config.SMS_GATEWAYS:
        raise SystemExit("No SMS_GATEWAYS configured (e.g. 5551234567@tmomail.net).")


def _send_email(context, to_addr, subject, body) -> bool:
    """Send to one gateway, retrying on error. A fresh SMTP connection per
    attempt so a dropped/deferred session recovers."""
    def attempt():
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
    return _with_retries(to_addr, attempt)


# --- Twilio ---------------------------------------------------------------

def _check_twilio_config() -> None:
    missing = [
        name for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_MESSAGING_SERVICE_SID")
        if not getattr(config, name)
    ]
    if missing:
        raise SystemExit(f"DELIVERY_CHANNEL=twilio but missing: {', '.join(missing)}")
    if not config.SMS_TO:
        raise SystemExit("No SMS_TO configured (E.164 numbers, e.g. +15551234567).")


def _send_twilio(to_number, subject, body) -> bool:
    """POST to Twilio's Messages API with the stdlib (no extra dependency).
    SMS has no subject, so the first line is the program name (matches the
    sample messages registered with the A2P campaign)."""
    text = f"{config.SMS_PROGRAM_NAME}\n{body.lstrip(chr(10))}"
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{config.TWILIO_ACCOUNT_SID}/Messages.json"
    )
    data = urllib.parse.urlencode({
        "To": to_number,
        "MessagingServiceSid": config.TWILIO_MESSAGING_SERVICE_SID,
        "Body": text,
    }).encode()
    auth = base64.b64encode(
        f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()
    ).decode()

    def attempt():
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Basic {auth}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                sid = json.load(resp).get("sid", "?")
                print(f"  -> {to_number}: queued ({sid})")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            # 4xx means our request is wrong (bad number, unregistered campaign);
            # retrying won't help, so surface it right away.
            if 400 <= e.code < 500:
                raise _NoRetry(f"HTTP {e.code}: {detail}") from e
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    return _with_retries(to_number, attempt)


# --- shared ---------------------------------------------------------------

class _NoRetry(Exception):
    pass


def _with_retries(target, attempt) -> bool:
    for n in range(1, _MAX_ATTEMPTS + 1):
        try:
            attempt()
            print(f"  -> {target}: sent")
            return True
        except _NoRetry as e:
            print(f"  !! {target}: FAILED - {e}")
            return False
        except Exception as e:  # noqa: BLE001 - retry then report
            if n < _MAX_ATTEMPTS:
                print(f"  .. {target}: attempt {n} failed ({e}); retrying")
                time.sleep(_RETRY_SECONDS)
            else:
                print(f"  !! {target}: FAILED after {n} attempts - {e}")
    return False

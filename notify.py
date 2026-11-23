"""Delivers the daily report as a text.

Two channels, picked by DELIVERY_CHANNEL:

  email  (default) Carrier email-to-SMS gateways over Gmail SMTP, e.g.
         5551234567@tmomail.net. Free, but carriers are shutting these down
         (Verizon vtext.com is mostly dead as of late 2026).
  twilio Real SMS through the Twilio Messaging Service tied to the approved
         A2P 10DLC campaign. Waits for the carrier status so a rejected text
         counts as a failure instead of a silent "queued".
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


def send_email(to_addrs, subject, text, html_body, attachments=()) -> None:
    """Send a regular email (the monthly report) from EMAIL_FROM via Gmail
    SMTP. attachments: [(filename, bytes, maintype, subtype)]."""
    if not config.EMAIL_FROM or not config.EMAIL_APP_PASSWORD:
        raise RuntimeError("Missing EMAIL_FROM / EMAIL_APP_PASSWORD")
    msg = EmailMessage()
    msg["From"] = config.EMAIL_FROM
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    def attempt():
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.EMAIL_HOST, config.EMAIL_PORT, context=context) as server:
            server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)
    if not _with_retries(", ".join(to_addrs), attempt):
        raise RuntimeError("email not sent")


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


def _twilio_request(url, data=None):
    """Authenticated call to Twilio's REST API using only the stdlib."""
    auth = base64.b64encode(
        f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# How long to watch a queued message for a final carrier status. "queued" from
# the API only means Twilio accepted it; carrier/A2P rejections (30007, 30034,
# ...) show up a few seconds later as "undelivered"/"failed".
_STATUS_TIMEOUT_SECONDS = 60
_STATUS_POLL_SECONDS = 5


def _send_twilio(to_number, subject, body) -> bool:
    """Send through the Messaging Service tied to the A2P campaign, then wait
    for the carrier result. SMS has no subject, so the first line is the
    program name (matches the sample messages registered with the campaign)."""
    text = f"{config.SMS_PROGRAM_NAME}\n{body.lstrip(chr(10))}"
    base = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}"
    data = urllib.parse.urlencode({
        "To": to_number,
        "MessagingServiceSid": config.TWILIO_MESSAGING_SERVICE_SID,
        "Body": text,
    }).encode()

    def attempt():
        try:
            msg = _twilio_request(f"{base}/Messages.json", data)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            # 4xx means our request is wrong (bad number, recipient replied
            # STOP = 21610, bad credentials); retrying won't help.
            if 400 <= e.code < 500:
                raise _NoRetry(f"HTTP {e.code}: {detail}") from e
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e

        sid = msg.get("sid", "?")
        print(f"  .. {to_number}: queued ({sid})")
        status, error = _wait_for_final_status(f"{base}/Messages/{sid}.json", msg)
        if status in ("undelivered", "failed"):
            # Not delivered, so a retry can't cause a duplicate text.
            raise RuntimeError(
                f"{status} (error {error}; see twilio.com/docs/api/errors/{error})"
            )
        if status not in ("sent", "delivered"):
            # Still in flight at timeout. Count it as sent rather than risk a
            # duplicate; check the Twilio console log if it never arrives.
            print(f"  .. {to_number}: still '{status}' after {_STATUS_TIMEOUT_SECONDS}s")
        else:
            print(f"  .. {to_number}: {status}")
    return _with_retries(to_number, attempt)


def _wait_for_final_status(url, msg):
    deadline = time.monotonic() + _STATUS_TIMEOUT_SECONDS
    while msg.get("status") in ("accepted", "queued", "sending", "scheduled"):
        if time.monotonic() >= deadline:
            break
        time.sleep(_STATUS_POLL_SECONDS)
        try:
            msg = _twilio_request(url)
        except Exception as e:  # noqa: BLE001 - status check is best effort
            print(f"  .. status check failed ({e}); assuming sent")
            break
    return msg.get("status"), msg.get("error_code")


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

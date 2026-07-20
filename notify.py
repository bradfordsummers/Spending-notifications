"""Sends the report as a real text message via carrier email-to-SMS gateways.

Each US carrier accepts email at a special address and delivers it to the phone
as a normal text (e.g. 5551234567@tmomail.net for T-Mobile). We send a short
email from a Gmail account; it arrives in the native Messages app. No Twilio,
no A2P registration, no app to install.

This is the only file that knows about the delivery channel. To switch back to
Twilio, Pushover, etc. later, you only change send_sms() here.
"""
import smtplib
import ssl
from email.message import EmailMessage

import config


def send_sms(body: str) -> None:
    if not config.EMAIL_FROM or not config.EMAIL_APP_PASSWORD:
        raise SystemExit(
            "Missing EMAIL_FROM / EMAIL_APP_PASSWORD. Set your Gmail address and "
            "a Gmail App Password in .env (see README)."
        )
    if not config.SMS_GATEWAYS:
        raise SystemExit("No SMS_GATEWAYS configured (e.g. 5551234567@tmomail.net).")

    context = ssl.create_default_context()
    failures = []
    with smtplib.SMTP_SSL(config.EMAIL_HOST, config.EMAIL_PORT, context=context) as server:
        server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
        for to_addr in config.SMS_GATEWAYS:
            # Send each recipient independently: one bad address must never
            # stop the others from getting their report.
            try:
                msg = EmailMessage()
                msg["From"] = config.EMAIL_FROM
                msg["To"] = to_addr
                # No subject: carriers prepend the subject to the text, so an
                # empty one keeps the message clean.
                msg["Subject"] = ""
                msg.set_content(body)
                server.send_message(msg)
                print(f"  -> {to_addr}: sent")
            except Exception as e:  # noqa: BLE001 - report and keep going
                failures.append((to_addr, str(e)))
                print(f"  !! {to_addr}: FAILED - {e}")

    if failures:
        raise SystemExit(f"{len(failures)} of {len(config.SMS_GATEWAYS)} sends failed.")

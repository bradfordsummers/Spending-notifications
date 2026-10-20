"""Repair the bank connection without any local setup (Plaid "update mode").

When the daily job fails with ITEM_LOGIN_REQUIRED (bank password change, MFA,
or periodic re-consent), the existing access token is still good -- the bank
just wants you to log in again. This creates an update-mode Link session for
the CURRENT PLAID_ACCESS_TOKEN and emails you a link. Open it, log into Bank of
America, and the same token works again, so no GitHub secret needs changing.

Run it from the Actions tab ("Repair bank connection"), or locally:

    python relink.py
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser

import config
from plaid_client import make_client

# A static page (docs/relink.html on GitHub Pages) that opens the same Plaid
# Link widget link_card.py uses. The token rides in the #fragment, which
# browsers never send to the server. (Plaid Hosted Link was tried first and
# failed with "Internal error occurred" on this account.)
PAGES_URL = "https://bradfordsummers.github.io/Spending-notifications/relink.html"


def _create_link(client):
    """Return (link_token, url) for an update-mode Link session."""
    resp = client.link_token_create(
        LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id="household"),
            client_name="Spending Notifications",
            country_codes=[CountryCode("US")],
            language="en",
            access_token=config.PLAID_ACCESS_TOKEN,  # this is what makes it update mode
        )
    )
    return resp.link_token, f"{PAGES_URL}#token={resp.link_token}"


def _email(url: str) -> None:
    msg = EmailMessage()
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.EMAIL_FROM
    msg["Subject"] = "Spending Notifications: reconnect Bank of America"
    msg.set_content(
        "Bank of America needs you to log in again before the daily spending\n"
        "texts can resume. Open this link (best on a computer), click the button,\n"
        "and log into Bank of America:\n\n"
        f"{url}\n\n"
        "Use it soon -- it expires after a short while. If it has expired, run\n"
        "\"Repair bank connection\" again from the repo's Actions tab.\n"
    )
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config.EMAIL_HOST, config.EMAIL_PORT, context=context) as server:
        server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
        server.send_message(msg)


def main():
    if not config.PLAID_ACCESS_TOKEN:
        raise SystemExit("Missing PLAID_ACCESS_TOKEN -- nothing to repair. Run link_card.py.")

    link_token, url = _create_link(make_client())

    if os.environ.get("GITHUB_ACTIONS"):
        # The link opens a login session for this card -- keep it out of the
        # Actions log (the repo is public) and deliver it by email only.
        print(f"::add-mask::{link_token}")
        print(f"::add-mask::{url}")
    else:
        print(f"\nOpen this link to reconnect:\n\n   {url}\n")

    if config.EMAIL_FROM and config.EMAIL_APP_PASSWORD:
        _email(url)
        print("Emailed the reconnect link to the EMAIL_FROM inbox.")
    elif os.environ.get("GITHUB_ACTIONS"):
        raise SystemExit("EMAIL_FROM / EMAIL_APP_PASSWORD not set; can't deliver the link.")


if __name__ == "__main__":
    main()

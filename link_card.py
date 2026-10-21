"""Connect a bank to this app via Plaid and save its token into .env.

Run on your own computer, once per bank:

    python link_card.py              # Bank of America card -> PLAID_ACCESS_TOKEN
    python link_card.py becu         # -> PLAID_TOKEN_BECU
    python link_card.py wellsfargo   # -> PLAID_TOKEN_WELLSFARGO

It opens a local page; log into the bank and pick the accounts. The token is
written straight into .env (no copy-paste). Push it to the cloud afterwards with
`gh secret set -f .env`. Re-run the same command if a connection ever breaks.
"""
import os
import sys
import webbrowser

from dotenv import set_key
from flask import Flask, jsonify, request

from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)

import config
from plaid_client import make_client

NAME = (sys.argv[1] if len(sys.argv) > 1 else "boa").lower()
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_KEY = "PLAID_ACCESS_TOKEN" if NAME == "boa" else f"PLAID_TOKEN_{NAME.upper()}"

app = Flask(__name__)
client = make_client()

PAGE = f"""
<!doctype html>
<html>
<head><title>Connect {NAME}</title></head>
<body style="font-family: system-ui; max-width: 640px; margin: 60px auto; text-align:center;">
  <h2>Spending Notifications &mdash; connect {NAME}</h2>
  <p>Click below, pick your bank, log in, and select the accounts to include.</p>
  <button id="btn" style="font-size:18px; padding:12px 24px; cursor:pointer;">Connect</button>
  <p id="status" style="color:#666; margin-top:24px;"></p>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    async function start() {{
      const {{ link_token }} = await (await fetch('/link-token')).json();
      Plaid.create({{
        token: link_token,
        onSuccess: async (public_token) => {{
          document.getElementById('status').innerText = 'Finishing up...';
          const data = await (await fetch('/exchange', {{
            method: 'POST', headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ public_token }})
          }})).json();
          document.getElementById('status').innerHTML = data.ok
            ? '&#9989; Done! Saved to .env as {ENV_KEY}. You can close this tab.'
            : ('Something went wrong: ' + (data.error || 'unknown'));
        }},
        onExit: (err) => {{
          if (err) document.getElementById('status').innerText = 'Exited: ' + JSON.stringify(err);
        }}
      }}).open();
    }}
    document.getElementById('btn').addEventListener('click', start);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return PAGE


@app.route("/link-token")
def link_token():
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="household"),
        client_name="Spending Notifications",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    return jsonify({"link_token": client.link_token_create(req).link_token})


@app.route("/exchange", methods=["POST"])
def exchange():
    public_token = request.get_json(force=True)["public_token"]
    try:
        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
    except Exception as e:  # noqa: BLE001 - surface any Plaid error to the page
        return jsonify({"ok": False, "error": str(e)}), 500

    set_key(ENV_FILE, ENV_KEY, resp.access_token, quote_mode="never")
    print(f"\nSUCCESS: saved {ENV_KEY} to .env. Press Ctrl+C to stop this script.\n", flush=True)
    return jsonify({"ok": True})


if __name__ == "__main__":
    if config.PLAID_ENV == "sandbox":
        print("\nNOTE: PLAID_ENV=sandbox -- use Plaid's test login (user_good / pass_good).\n")
    url = "http://127.0.0.1:5000/"
    print(f"Connecting '{NAME}' -> {ENV_KEY}. Opening {url} ...", flush=True)
    webbrowser.open(url)
    app.run(port=5000)

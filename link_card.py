"""One-time setup: connect your Bank of America Alaska Airlines card via Plaid.

Run this ONCE on your own computer:

    python link_card.py

It opens a small local web page. Click the button, log into Bank of America,
and pick the card. When it finishes, this script prints your PLAID_ACCESS_TOKEN.
Copy that token into your .env file and into GitHub repo Secrets. You never need
to run this again unless the connection breaks.
"""
import webbrowser

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

app = Flask(__name__)
client = make_client()

PAGE = """
<!doctype html>
<html>
<head><title>Connect your card</title></head>
<body style="font-family: system-ui; max-width: 640px; margin: 60px auto; text-align:center;">
  <h2>Spending Notifications &mdash; connect your card</h2>
  <p>Click below and log into Bank of America to link the Alaska Airlines card.</p>
  <button id="btn" style="font-size:18px; padding:12px 24px; cursor:pointer;">
    Connect Bank of America
  </button>
  <p id="status" style="color:#666; margin-top:24px;"></p>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    async function start() {
      const r = await fetch('/link-token');
      const { link_token } = await r.json();
      const handler = Plaid.create({
        token: link_token,
        onSuccess: async (public_token) => {
          document.getElementById('status').innerText = 'Finishing up...';
          const resp = await fetch('/exchange', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ public_token })
          });
          const data = await resp.json();
          document.getElementById('status').innerHTML =
            data.ok
              ? '&#9989; Done! Return to your terminal to copy the access token, then close this tab.'
              : ('Something went wrong: ' + (data.error || 'unknown'));
        },
        onExit: (err) => {
          if (err) document.getElementById('status').innerText = 'Exited: ' + JSON.stringify(err);
        }
      });
      handler.open();
    }
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
    resp = client.link_token_create(req)
    return jsonify({"link_token": resp.link_token})


@app.route("/exchange", methods=["POST"])
def exchange():
    public_token = request.get_json(force=True)["public_token"]
    try:
        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
    except Exception as e:  # noqa: BLE001 - surface any Plaid error to the page
        return jsonify({"ok": False, "error": str(e)}), 500

    access_token = resp.access_token
    print("\n" + "=" * 70)
    print("SUCCESS. Your PLAID_ACCESS_TOKEN is:\n")
    print("   " + access_token)
    print("\nAdd it to your .env file and to GitHub repo Secrets as")
    print("PLAID_ACCESS_TOKEN, then stop this script (Ctrl+C).")
    print("=" * 70 + "\n")
    return jsonify({"ok": True})


if __name__ == "__main__":
    if config.PLAID_ENV == "sandbox":
        print(
            "\nNOTE: PLAID_ENV=sandbox — you'll log in with Plaid's fake test "
            "credentials (user_good / pass_good), not real Bank of America.\n"
            "Set PLAID_ENV=production in .env to link your real card.\n"
        )
    url = "http://127.0.0.1:5000/"
    print(f"Opening {url} — complete the flow in your browser.")
    webbrowser.open(url)
    app.run(port=5000)

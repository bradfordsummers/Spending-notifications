"""Builds a configured Plaid API client."""
import plaid
from plaid.api import plaid_api

import config

_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def make_client() -> plaid_api.PlaidApi:
    client_id, secret = config.require_plaid()
    if config.PLAID_ENV not in _HOSTS:
        raise SystemExit(
            f"PLAID_ENV must be one of {list(_HOSTS)}, got {config.PLAID_ENV!r}"
        )
    configuration = plaid.Configuration(
        host=_HOSTS[config.PLAID_ENV],
        api_key={"clientId": client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))

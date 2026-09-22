"""Microsoft Graph auth via MSAL device-code flow.

Only scope requested is Mail.Send. First run prints a code for
microsoft.com/devicelogin; after that the refresh token is cached in
.msal_token_cache.json and later runs are silent.
"""

import os
import sys
from pathlib import Path

import msal
from dotenv import load_dotenv

# This module lives in graph/, so the project root is one level up.
ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / ".msal_token_cache.json"

# Mail.Send only. Getting your own address comes from .env, not User.Read,
# so the consent request stays as small as possible.
SCOPES = ["Mail.Send"]

load_dotenv(ROOT / ".env")
CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
TENANT_ID = os.getenv("GRAPH_TENANT_ID")


def _load_cache():
    cache = msal.SerializableTokenCache()
    if CACHE_PATH.exists():
        cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")


def explain(error_code: str, description: str) -> str:
    """Translate the Entra error codes you are actually likely to hit."""
    if "AADSTS65001" in description or error_code == "consent_required":
        return (
            "CONSENT NOT GRANTED -- the app exists but nobody has approved Mail.Send.\n"
            "  Mail.Send is admin-restricted by default, so your admin must click\n"
            "  'Grant admin consent for <tenant>' on the app's API permissions page.\n"
            "  See 'Using Microsoft Graph' in README.md for the exact steps."
        )
    if "AADSTS700016" in description or error_code == "unauthorized_client":
        return (
            "APP NOT FOUND in this tenant.\n"
            "  Check GRAPH_CLIENT_ID and GRAPH_TENANT_ID in .env match the\n"
            "  registration in Entra > App registrations > Overview."
        )
    if "AADSTS7000218" in description or "public client" in description.lower():
        return (
            "PUBLIC CLIENT FLOW DISABLED.\n"
            "  In Entra > App registrations > your app > Authentication,\n"
            "  set 'Allow public client flows' to Yes. Device code needs it."
        )
    if "AADSTS50076" in description or "AADSTS50079" in description:
        return "MFA required -- complete the prompt in the browser, then re-run."
    if "AADSTS50105" in description:
        return (
            "USER NOT ASSIGNED -- the app requires assignment and your account\n"
            "  is not on the list. Ask the admin to assign you, or turn off\n"
            "  'Assignment required' in Enterprise applications > Properties."
        )
    return ""


def get_token(quiet: bool = False) -> str:
    """Return a Graph access token, prompting for device login only if needed."""
    if not CLIENT_ID or not TENANT_ID:
        sys.exit(
            "Missing GRAPH_CLIENT_ID or GRAPH_TENANT_ID in .env\n"
            "Both come from Entra > App registrations > your app > Overview.\n"
            "Add them yourself -- this script only reads them."
        )

    cache = _load_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )

    # Silent path: reuse the cached refresh token.
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and not quiet:
            print(f"  using cached token for {accounts[0].get('username')}")

    # Interactive path: device code.
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            desc = flow.get("error_description", str(flow))
            print(f"\nCould not start device flow: {desc}\n")
            print(explain(flow.get("error", ""), desc))
            sys.exit(1)

        print("\n" + "=" * 68)
        print(flow["message"])
        print("=" * 68 + "\n")

        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        desc = result.get("error_description", str(result))
        print(f"\nToken request failed: {desc}\n")
        hint = explain(result.get("error", ""), desc)
        if hint:
            print(hint)
        sys.exit(1)

    return result["access_token"]


if __name__ == "__main__":
    token = get_token()
    print(f"\nOK -- got a token ({len(token)} chars). Cached in {CACHE_PATH.name}")

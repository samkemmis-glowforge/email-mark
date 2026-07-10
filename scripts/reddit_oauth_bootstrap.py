"""One-time OAuth bootstrap for the Reddit Ads API — mints a refresh token.

Usage (from project root, REDDIT_ADS_CLIENT_ID/SECRET in .env or env):

  1. Print the authorize URL and open it in a browser logged in as the
     Reddit account that administers the ads account:
         python scripts/reddit_oauth_bootstrap.py

  2. Approve. The browser lands on http://localhost:8080/?state=...&code=XYZ
     (the page won't load — that's fine). Copy the `code` from the URL bar.

  3. Exchange it:
         python scripts/reddit_oauth_bootstrap.py CODE_FROM_URL

     Prints REDDIT_ADS_REFRESH_TOKEN=... — add it to .env / env settings.

The redirect URI must exactly match the one registered on the app
(default http://localhost:8080; override with REDDIT_OAUTH_REDIRECT_URI).
"""

import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import requests  # noqa: E402
from dotenv import find_dotenv, load_dotenv  # noqa: E402

load_dotenv(find_dotenv())

AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SCOPES = "adsread adsedit"


def main() -> None:
    client_id = os.environ.get("REDDIT_ADS_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_ADS_CLIENT_SECRET")
    redirect_uri = os.environ.get("REDDIT_OAUTH_REDIRECT_URI", "http://localhost:8080")
    if not client_id or not client_secret:
        print("Set REDDIT_ADS_CLIENT_ID and REDDIT_ADS_CLIENT_SECRET first.")
        sys.exit(1)

    if len(sys.argv) < 2:
        params = {
            "client_id": client_id,
            "response_type": "code",
            "state": "email-mark",
            "redirect_uri": redirect_uri,
            "duration": "permanent",
            "scope": SCOPES,
        }
        print("Open this URL in a browser logged in as the ads-account admin:\n")
        print(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}\n")
        print("Approve, copy `code` from the localhost URL you land on, then run:")
        print("  python scripts/reddit_oauth_bootstrap.py THE_CODE")
        return

    code = sys.argv[1].strip()
    # Reddit appends #_ to the code in some browsers; strip it.
    code = code.split("#")[0]
    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"User-Agent": "email-mark/1.0 (Glowforge marketing bot)"},
        timeout=30,
    )
    payload = resp.json()
    if resp.status_code >= 400 or "refresh_token" not in payload:
        print(f"Exchange failed (HTTP {resp.status_code}): {payload}")
        print("Common causes: code already used (they're single-use — re-run "
              "step 1), or redirect_uri mismatch with the app registration.")
        sys.exit(1)
    print("Success. Add this to .env and the environment settings:\n")
    print(f"REDDIT_ADS_REFRESH_TOKEN={payload['refresh_token']}")
    print(f"\n(scopes granted: {payload.get('scope')})")


if __name__ == "__main__":
    main()

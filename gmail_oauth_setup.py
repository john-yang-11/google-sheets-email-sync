"""One-time: mint the Gmail refresh token scan_mail.py needs. Run locally.

Why this is separate from the service account that writes the sheet: a service
account has no mailbox and cannot be granted one on a personal Google account.
Reading your mail means authenticating as you, which is what OAuth is for. The
scope requested is gmail.readonly and nothing else -- this can never send,
delete, or modify mail.

Setup, once:

  1. console.cloud.google.com -> the same project as the sheet service account
  2. APIs & Services -> Library -> enable "Gmail API"
  3. APIs & Services -> OAuth consent screen -> External, and add
     johnyang1032@gmail.com as a Test user
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app
  5. python gmail_oauth_setup.py <client_id> <client_secret>

A consent screen left in Testing expires its refresh tokens after 7 days, and
the sync then fails quietly -- a red run and no other signal. Hit "Publish app"
once you are happy it works; the token then lasts until you revoke it.
"""

import http.server
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser

import requests

PORT = 8765
REDIRECT = f"http://localhost:{PORT}"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

result: dict = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = ("<h2>Done - back to the terminal.</h2>" if "code" in result else
                f"<h2>Authorization failed</h2><p>{result.get('error', 'no code')}</p>")
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass                     # the default logger prints the auth code to stderr


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python gmail_oauth_setup.py <client_id> <client_secret>")
    client_id, client_secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)

    def serve():
        # A browser can spend the first request on /favicon.ico, so keep
        # answering until the redirect with the code actually lands.
        while "code" not in result and "error" not in result:
            server.handle_request()

    threading.Thread(target=serve, daemon=True).start()

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",     # without this, no refresh token comes back
        "prompt": "consent",          # force one even if already authorized
        "state": state,
    })
    print("Opening your browser. Sign in as johnyang1032@gmail.com and approve.")
    print(f"If nothing opens, paste this in yourself:\n\n{url}\n")
    webbrowser.open(url)

    for _ in range(600):              # ~2 min, checked every 200ms
        if result:
            break
        time.sleep(0.2)
    if "code" not in result:
        raise SystemExit(f"no auth code received ({result.get('error', 'timed out')})")
    if result.get("state") != state:
        raise SystemExit("state mismatch - start over")

    r = requests.post(TOKEN_URL, timeout=30, data={
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    })
    if not r.ok:
        raise SystemExit(f"token exchange failed ({r.status_code}): {r.text[:400]}")
    tok = r.json().get("refresh_token")
    if not tok:
        raise SystemExit("no refresh_token in the response - re-run; the consent "
                         "screen must be shown at least once per client")

    print("\nAdd these as repo secrets (Settings -> Secrets and variables -> "
          "Actions), and to .env for local runs:\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={tok}")


if __name__ == "__main__":
    main()

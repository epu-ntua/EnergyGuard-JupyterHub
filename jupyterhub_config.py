import base64
import json
import logging
import os
import time

import httpx
from dockerspawner import DockerSpawner
from urllib.parse import quote
from dotenv import dotenv_values
from pathlib import Path

c = get_config()

# ---------------- Auth: Keycloak (OIDC) ----------------
c.JupyterHub.authenticator_class = "oauthenticator.generic.GenericOAuthenticator"

# Keycloak URLs (public URL the browser uses)
REALM = os.environ.get("KC_REALM", "EnergyGuard")
KC_BASE = os.environ["KC_BASE_URL"].rstrip("/")  # e.g. https://keycloak.toolbox.epu.ntua.gr
ISSUER = f"{KC_BASE}/realms/{REALM}"

c.GenericOAuthenticator.client_id = os.environ["KC_CLIENT_ID"]
c.GenericOAuthenticator.client_secret = os.environ["KC_CLIENT_SECRET"]

# JupyterHub callback URL (must match Keycloak client's redirect URI)
c.GenericOAuthenticator.oauth_callback_url = "https://jupyterhub.energy-guard.eu/hub/oauth_callback"

# Keycloak endpoints (explicit)
c.GenericOAuthenticator.authorize_url = f"{ISSUER}/protocol/openid-connect/auth"
c.GenericOAuthenticator.token_url     = f"{ISSUER}/protocol/openid-connect/token"
c.GenericOAuthenticator.userdata_url  = f"{ISSUER}/protocol/openid-connect/userinfo"
c.GenericOAuthenticator.userdata_token_method = "GET"


# Basic scopes + username claim (no offline_access — we want sessions tied to SSO)
c.GenericOAuthenticator.scope = ["openid", "profile", "email", "groups"]
c.GenericOAuthenticator.username_claim = "preferred_username"

# Allow all authenticated users
c.GenericOAuthenticator.allow_all = True

# Enable auth state so refresh_user can check tokens
c.Authenticator.enable_auth_state = True
c.Authenticator.refresh_pre_spawn = True

# Check token validity every 30 seconds — the refresh_user hook below will
# also check the revocation file written by the backchannel logout server.
c.OAuthenticator.auth_refresh_age = 30

# Logout: redirect to Keycloak end-session endpoint (triggers backchannel to other apps)
post = "https://jupyterhub.energy-guard.eu/hub/login?next=%2Fhub%2F"
c.OAuthenticator.logout_redirect_url = (
    f"{ISSUER}/protocol/openid-connect/logout"
    f"?client_id={os.environ['KC_CLIENT_ID']}"
    f"&post_logout_redirect_uri={quote(post, safe='')}"
)


# ---------------- Hub basics ----------------
c.JupyterHub.bind_url = "http://0.0.0.0:8009"
c.JupyterHub.cookie_secret_file = "/srv/jupyterhub/jupyterhub_cookie_secret"
c.JupyterHub.db_url = "sqlite:////srv/jupyterhub/jupyterhub.sqlite"

# Tornado-level settings. xheaders=True makes JupyterHub honor
# X-Forwarded-Proto/Host from Nginx Proxy Manager so OAuth redirects use the
# correct external scheme/host. cookie_options carries the Secure flag.
cookie_secure = os.environ.get("JH_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"}
c.JupyterHub.tornado_settings = {
    "cookie_options": {"secure": cookie_secure},
    "xheaders": True,
}

# Silence the per-request "Setting new xsrf cookie" INFO log — it fires on
# nearly every Hub request and drowns more useful lines. WARNING+ still shows.
logging.getLogger("jupyterhub._xsrf_utils").setLevel(logging.WARNING)


# =========================================================================
# Backchannel Logout (SSO)
# =========================================================================
# Architecture:
#   1. A background stdlib HTTP server on port 8002 receives Keycloak's
#      backchannel logout POST (logout_token JWT).
#   2. It resolves the user and writes the username to a revocation file.
#   3. JupyterHub's refresh_user hook (runs every auth_refresh_age seconds)
#      checks the revocation file. If the current user is revoked, it returns
#      False — which makes JupyterHub clear the session cookie and force
#      re-authentication via Keycloak.
#
# This approach works within JupyterHub's own process for session invalidation,
# avoiding the ORM cache issues that come with direct SQLite manipulation.
# =========================================================================

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

_BCL_PORT = int(os.environ.get("BCL_PORT", "8002"))
_REVOCATION_FILE = "/srv/jupyterhub/revoked_users.json"
_bcl_logger = logging.getLogger("backchannel-logout")
_bcl_logger.setLevel(logging.DEBUG)
_bcl_handler = logging.StreamHandler()
_bcl_handler.setLevel(logging.DEBUG)
_bcl_handler.setFormatter(logging.Formatter("[BCL %(asctime)s] %(levelname)s: %(message)s"))
_bcl_logger.addHandler(_bcl_handler)


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _resolve_keycloak_sub(sub: str) -> str | None:
    """Resolve a Keycloak user UUID to preferred_username via the admin API."""
    client_id = os.environ.get("KC_CLIENT_ID", "")
    client_secret = os.environ.get("KC_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    token_url = f"{ISSUER}/protocol/openid-connect/token"
    admin_base = ISSUER.replace("/realms/", "/admin/realms/")
    user_url = f"{admin_base}/users/{sub}"

    try:
        with httpx.Client(timeout=10) as client:
            token_resp = client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            })
            if token_resp.status_code != 200:
                _bcl_logger.warning("Keycloak token request failed: %s", token_resp.status_code)
                return None
            access_token = token_resp.json().get("access_token")

            user_resp = client.get(user_url, headers={"Authorization": f"Bearer {access_token}"})
            if user_resp.status_code != 200:
                _bcl_logger.warning("Keycloak user lookup failed for sub=%s: %s", sub, user_resp.status_code)
                return None
            user_data = user_resp.json()
            username = user_data.get("username", "").strip()
            _bcl_logger.info("Resolved sub=%s -> username=%s", sub, username)
            return username or None
    except Exception as e:
        _bcl_logger.error("Keycloak API error for sub=%s: %s", sub, e)
        return None


# ---------------------------------------------------------------------------
# Revocation file helpers (used by both the BCL server thread and the
# refresh_user hook running in JupyterHub's main event loop)
# ---------------------------------------------------------------------------
_revocation_lock = threading.Lock()


def _read_revocations() -> dict:
    try:
        with open(_REVOCATION_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_revocations(data: dict) -> None:
    with open(_REVOCATION_FILE, "w") as f:
        json.dump(data, f)


def _add_revocation(username: str) -> None:
    with _revocation_lock:
        revocations = _read_revocations()
        revocations[username.lower()] = time.time()
        _write_revocations(revocations)
        _bcl_logger.info("Added %s to revocation file. Current revocations: %s", username, list(revocations.keys()))


def _is_user_revoked(username: str) -> bool:
    """Return True if *username* is revoked (within the last 5 minutes)."""
    with _revocation_lock:
        revocations = _read_revocations()
        ts = revocations.get(username.lower())
        if ts is None:
            return False
        # Keep the revocation active for 5 minutes so ALL concurrent requests
        # see it.  Clean up expired entries while we're here.
        now = time.time()
        expired = [k for k, v in revocations.items() if now - v > 300]
        if expired:
            for k in expired:
                del revocations[k]
            _write_revocations(revocations)
        return now - ts < 300


# ---------------------------------------------------------------------------
# refresh_user hook — runs inside JupyterHub's process every auth_refresh_age
# seconds when the user makes a Hub request.  Returning False clears the
# session cookie and forces re-authentication.
#
# Just returning False is not enough in practice: clear_login_cookie only
# clears cookies that exactly match name+path+domain, and we've seen
# browsers end up holding a leftover hub-login cookie at the wrong path
# that doesn't get cleared. That causes the next request to *re-identify*
# the user from the stale cookie, fire refresh_user → False again, and
# loop forever after a different user logs in on the same browser.
#
# We fix that by rotating the *revoked user's cookie_id* in the JupyterHub
# DB. Every signed JupyterHub cookie embeds cookie_id; JupyterHub looks the
# user up via `User.cookie_id == cookie_id_from_cookie`. After rotation, any
# stale cookie in the browser fails that lookup, the request becomes
# anonymous, and the bounce-to-login resolves cleanly — letting the new
# user's fresh OIDC set a clean cookie unimpeded.
# ---------------------------------------------------------------------------
from jupyterhub.utils import new_token as _new_token

# Users whose cookie_id we've already rotated in this revocation cycle.
# Reset when the revocation entry is cleared. Guarded by _revocation_lock.
_rotated_users: set[str] = set()


def _get_revocation_time(username: str) -> float:
    """Return the revocation timestamp for *username*, or 0."""
    revocations = _read_revocations()
    return revocations.get(username.lower(), 0)


def _clear_revocation(username: str) -> None:
    with _revocation_lock:
        revocations = _read_revocations()
        revocations.pop(username.lower(), None)
        _write_revocations(revocations)
        _rotated_users.discard(username.lower())


def _rotate_cookie_id_once(user) -> bool:
    """Invalidate the user's existing signed Hub cookies, once per revocation.

    Mutates ``user.orm_user.cookie_id`` to a fresh random value and commits.
    Subsequent ``orm.User.cookie_id == old`` lookups return None, so the
    browser's stale ``jupyterhub-hub-login`` cookie becomes a no-op.
    """
    key = user.name.lower()
    with _revocation_lock:
        if key in _rotated_users:
            return False
        _rotated_users.add(key)

    try:
        orm_user = getattr(user, "orm_user", None)
        db = getattr(user, "db", None)
        if orm_user is None or db is None:
            _bcl_logger.warning(
                "Cannot rotate cookie_id for %s: ORM user/db not accessible",
                user.name,
            )
            return False
        orm_user.cookie_id = _new_token()
        db.commit()
        _bcl_logger.info(
            "Rotated cookie_id for %s — stale Hub cookies now invalid", user.name
        )
        return True
    except Exception as exc:                                # noqa: BLE001
        _bcl_logger.error("Failed to rotate cookie_id for %s: %s", user.name, exc)
        return False


async def _refresh_user(authenticator, user, auth_state):
    _bcl_logger.info("refresh_user called for user=%s", user.name)
    if _is_user_revoked(user.name):
        revocation_ts = _get_revocation_time(user.name)
        # Check if the user re-authenticated after the revocation
        if auth_state and auth_state.get("access_token"):
            token_payload = _decode_jwt_payload(auth_state["access_token"])
            iat = token_payload.get("iat", 0)
            if iat > revocation_ts:
                _bcl_logger.info(
                    "refresh_user: %s re-authenticated after revocation (iat=%s > revoked=%s), clearing",
                    user.name, iat, revocation_ts,
                )
                _clear_revocation(user.name)
                return None
        _rotate_cookie_id_once(user)
        _bcl_logger.info("refresh_user: REVOKING session for %s — returning False", user.name)
        return False
    _bcl_logger.info("refresh_user: %s not revoked, proceeding with default refresh", user.name)
    return None

c.GenericOAuthenticator.refresh_user_hook = _refresh_user


# ---------------------------------------------------------------------------
# Catch natural session expiry (Keycloak SSO idle / max-lifespan timeouts)
# ---------------------------------------------------------------------------
# The refresh_user_hook above only rotates the cookie_id when a user is in
# the BCL revocation file — i.e. for explicit Keycloak logouts that arrived
# via the backchannel endpoint. Keycloak sessions also die quietly when they
# hit SSO Session Idle / Max Lifespan, with no BCL POST. Same for any session
# that ended while the BCL endpoint was disabled.
#
# In those cases OAuthenticator.refresh_user notices the dead refresh_token
# (HTTP 400 invalid_grant) and returns False — but the stale browser cookie
# still maps to the user's cookie_id in the DB, so the next login from the
# same browser gets identified as the dead user and bounces back to login
# in an OAuth loop.
#
# Wrap GenericOAuthenticator.refresh_user so any False result also rotates
# the cookie_id, neutralizing the stale browser cookie regardless of how
# the session ended.
# ---------------------------------------------------------------------------
from oauthenticator.generic import GenericOAuthenticator

_original_authenticator_refresh_user = GenericOAuthenticator.refresh_user


async def _refresh_user_with_rotation(self, user, *args, **kwargs):
    result = await _original_authenticator_refresh_user(self, user, *args, **kwargs)
    if result is False:
        _bcl_logger.info(
            "GenericOAuthenticator.refresh_user returned False for %s — "
            "rotating cookie_id to invalidate stale browser cookies",
            user.name,
        )
        _rotate_cookie_id_once(user)
    else:
        # Refresh succeeded (True/dict/None). Drop any stale rotation-dedup
        # entry so a future expiry of this same user can rotate again.
        with _revocation_lock:
            _rotated_users.discard(user.name.lower())
    return result


GenericOAuthenticator.refresh_user = _refresh_user_with_rotation


# ---------------------------------------------------------------------------
# Delete user tokens via JupyterHub REST API (invalidates singleuser cookie)
# ---------------------------------------------------------------------------
_JHUB_API_URL = "http://127.0.0.1:8081/hub/api"


def _delete_user_tokens_via_api(username: str) -> int:
    """Delete all OAuth/API tokens for *username* via the JupyterHub REST API.

    This invalidates the ``jupyterhub-user-{username}`` cookie so the notebook
    browser session ends, but the singleuser server keeps running.
    """
    if not _BCL_API_TOKEN:
        _bcl_logger.warning("No BCL_API_TOKEN configured, cannot delete tokens via API")
        return -1

    headers = {"Authorization": f"token {_BCL_API_TOKEN}"}
    deleted = 0
    try:
        with httpx.Client(timeout=10) as client:
            # List the user's tokens
            resp = client.get(f"{_JHUB_API_URL}/users/{username}/tokens", headers=headers)
            if resp.status_code != 200:
                _bcl_logger.warning("Failed to list tokens for %s: %s %s", username, resp.status_code, resp.text[:200])
                return -1
            data = resp.json()
            # JupyterHub 4.x splits the response into two lists by kind:
            #   "api_tokens"  → kind in {user, service, server}
            #   "oauth_tokens" → kind == "oauth"  (the browser-session tokens)
            #
            # We only want to invalidate the browser session for the singleuser
            # server — that's "oauth_tokens". Server tokens (which include the
            # running container's JUPYTERHUB_API_TOKEN) MUST be preserved, or
            # the live notebook container immediately starts getting 403
            # "Missing or invalid credentials" on its /activity pings.
            #
            # IMPORTANT: do NOT filter api_tokens by `oauth_client`. Server
            # tokens have an oauth_client linkage to the per-user OAuth client
            # (jupyterhub-user-<name>), so any filter that keys off
            # oauth_client truthiness will delete them too.
            if isinstance(data, dict):
                api_tokens = data.get("api_tokens", [])
                oauth_tokens = data.get("oauth_tokens", [])
            else:
                # Older JupyterHub: flat list, partition by kind.
                api_tokens = [t for t in data if isinstance(t, dict) and t.get("kind") != "oauth"]
                oauth_tokens = [t for t in data if isinstance(t, dict) and t.get("kind") == "oauth"]
            _bcl_logger.info(
                "Found %d api_tokens (preserved), %d oauth_tokens (to delete) for %s",
                len(api_tokens), len(oauth_tokens), username,
            )

            for token in oauth_tokens:
                token_id = token.get("id", "")
                del_resp = client.delete(
                    f"{_JHUB_API_URL}/users/{username}/tokens/{token_id}",
                    headers=headers,
                )
                if del_resp.status_code in (200, 204):
                    deleted += 1
                    _bcl_logger.info("Deleted OAuth token %s for %s", token_id, username)
                else:
                    _bcl_logger.warning("Failed to delete token %s: %s %s", token_id, del_resp.status_code, del_resp.text[:200])
    except Exception as e:
        _bcl_logger.error("Error deleting tokens for %s: %s", username, e)
        return -1

    _bcl_logger.info("Deleted %d/%d OAuth tokens for %s", deleted, len(oauth_tokens), username)
    return deleted


# ---------------------------------------------------------------------------
# Background HTTP server for receiving Keycloak backchannel logout POSTs
# ---------------------------------------------------------------------------
class _BCLogoutHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        _bcl_logger.info(fmt, *args)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        revocations = _read_revocations()
        self._json(200, {
            "endpoint": "/backchannel-logout",
            "method": "POST",
            "status": "ready",
            "pending_revocations": list(revocations.keys()),
        })

    def do_POST(self):
        _bcl_logger.info("=== BACKCHANNEL LOGOUT POST RECEIVED ===")
        _bcl_logger.info("Client: %s", self.client_address)
        _bcl_logger.info("Headers: %s", dict(self.headers))
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        _bcl_logger.info("Body length: %d", length)
        params = parse_qs(body)
        logout_token = params.get("logout_token", [""])[0]

        if not logout_token:
            _bcl_logger.warning("No logout_token in POST body. Raw body: %s", body[:500])
            self._json(400, {"error": "missing logout_token"})
            return

        _bcl_logger.info("logout_token (first 80 chars): %s", logout_token[:80])
        payload = _decode_jwt_payload(logout_token)
        _bcl_logger.info("Decoded JWT payload: %s", json.dumps(payload, default=str))
        if not payload:
            self._json(400, {"error": "invalid logout_token"})
            return

        username = payload.get("preferred_username") or payload.get("email")
        _bcl_logger.info("Username from token directly: %s", username)
        if not username:
            sub = payload.get("sub", "")
            _bcl_logger.info("No username in token, resolving sub=%s via Keycloak admin API", sub)
            if sub:
                username = _resolve_keycloak_sub(sub)
                _bcl_logger.info("Resolved username: %s", username)

        if not username:
            _bcl_logger.warning("FAILED: cannot determine user from token")
            self._json(400, {"error": "cannot determine user"})
            return

        _bcl_logger.info("Writing revocation for user: %s", username)
        _add_revocation(username)

        # Delete user's OAuth tokens via the JupyterHub API so the singleuser
        # notebook cookie is also invalidated.  The server keeps running.
        tokens_deleted = _delete_user_tokens_via_api(username)

        self._json(200, {"status": "ok", "user": username, "tokens_deleted": tokens_deleted})
        _bcl_logger.info("=== BACKCHANNEL LOGOUT COMPLETE for %s ===", username)


def _start_bcl_server():
    server = HTTPServer(("0.0.0.0", _BCL_PORT), _BCLogoutHandler)
    _bcl_logger.info("Backchannel logout server listening on port %d", _BCL_PORT)
    server.serve_forever()


threading.Thread(target=_start_bcl_server, daemon=True).start()


# ---------------------------------------------------------------------------
# Backchannel logout service — registered with JupyterHub so it can call the
# REST API to delete user tokens (invalidating the singleuser cookie).
# ---------------------------------------------------------------------------
_BCL_API_TOKEN = os.environ.get("BCL_API_TOKEN", "")
if _BCL_API_TOKEN:
    c.JupyterHub.services = [
        {
            "name": "backchannel-logout",
            "api_token": _BCL_API_TOKEN,
        }
    ]

c.JupyterHub.load_roles = [
    {
        "name": "user",
        "scopes": ["self", "admin:auth_state!user"],
    },
    {
        "name": "server",
        "scopes": [
            "users:activity!user",
            "access:servers!server",
            "admin:auth_state!user",
        ],
    },
]
# Grant the BCL service permission to read users and delete tokens
if _BCL_API_TOKEN:
    c.JupyterHub.load_roles.append({
        "name": "backchannel-logout-role",
        "services": ["backchannel-logout"],
        "scopes": ["admin:users", "tokens", "read:users"],
    })


# ---------------------------------------------------------------------------
# Cross-user next-URL guard
# ---------------------------------------------------------------------------
# After login JupyterHub redirects to whatever ?next= was in the login URL.
# If user B lands on user A's stale URL (typical after BCL leaves the
# address bar pointing at /user/aliceA/...), the post-login redirect would
# send B to /user/aliceA/... — which gets stuck in a per-user-OAuth bounce
# loop. From the user's perspective: "stuck in login."
#
# Wrap BaseHandler.get_next_url so we never redirect a freshly-authenticated
# user to a URL belonging to a different user; fall back to the user's own
# server URL in that case.
# ---------------------------------------------------------------------------
from urllib.parse import unquote, urlsplit, parse_qs
from jupyterhub.handlers.base import BaseHandler
from oauthenticator.oauth2 import OAuthCallbackHandler

_PER_USER_OAUTH_PREFIX = "jupyterhub-user-"


def _cross_user_target(next_url, user):
    """Return the *other* user's name if next_url targets them, else None.

    Catches two distinct ways JupyterHub embeds a username in a redirect:

      1. Direct path: /user/<other>/...
      2. Per-user OAuth authorize: /hub/api/oauth2/authorize
         ?client_id=jupyterhub-user-<other>&redirect_uri=/user/<other>/oauth_callback

    Both happen after BCL when the browser was sitting on a deep link to
    the previous user's server.
    """
    if not next_url:
        return None

    # (1) /user/<name>/...
    if next_url.startswith("/user/"):
        parts = next_url.split("/", 3)
        if len(parts) >= 3:
            path_user = unquote(parts[2])
            if path_user not in {user.name, user.escaped_name}:
                return path_user

    # (2) /hub/api/oauth2/authorize?client_id=jupyterhub-user-<name>&...
    if next_url.startswith("/hub/api/oauth2/authorize"):
        try:
            qs = parse_qs(urlsplit(next_url).query)
        except ValueError:
            return None
        for cid in qs.get("client_id", []):
            if cid.startswith(_PER_USER_OAUTH_PREFIX):
                # parse_qs decoded once; the username may still be percent-
                # encoded (e.g. "greece%40gmail.com"). Normalize then compare.
                client_user = unquote(cid[len(_PER_USER_OAUTH_PREFIX):])
                if client_user not in {user.name, user.escaped_name}:
                    return client_user

    return None


def _rewrite_if_cross_user(handler, user, next_url):
    if not user or not next_url:
        return next_url
    other = _cross_user_target(next_url, user)
    if other:
        _bcl_logger.info(
            "Rewriting cross-user next=%s for %s (was for %s)",
            next_url, user.name, other,
        )
        return handler.hub.base_url + "user/" + user.escaped_name + "/"
    return next_url


# (a) BaseHandler.get_next_url — generic path used by /hub/login, the user
# spawn-pending handler, etc.
_original_base_get_next_url = BaseHandler.get_next_url


def _base_get_next_url_owner_safe(self, user=None, default=None):
    next_url = _original_base_get_next_url(self, user=user, default=default)
    return _rewrite_if_cross_user(self, user, next_url)


BaseHandler.get_next_url = _base_get_next_url_owner_safe


# (b) OAuthCallbackHandler.get_next_url — oauthenticator overrides get_next_url
# on the OIDC callback handler to restore next_url from the OAuth state cookie.
# That codepath BYPASSES BaseHandler.get_next_url via MRO. We have to patch
# it separately or cross-user redirects on the post-OIDC redirect leak through.
_original_oauth_get_next_url = OAuthCallbackHandler.get_next_url


def _oauth_get_next_url_owner_safe(self, user=None):
    next_url = _original_oauth_get_next_url(self, user=user)
    return _rewrite_if_cross_user(self, user, next_url)


OAuthCallbackHandler.get_next_url = _oauth_get_next_url_owner_safe


# ---------------- Spawner (Docker) ----------------
c.JupyterHub.spawner_class = DockerSpawner
c.DockerSpawner.image = os.environ.get("DOCKER_NOTEBOOK_IMAGE")
notebook_dir = "/home/jovyan/work"
c.DockerSpawner.notebook_dir = notebook_dir
c.DockerSpawner.volumes = {"jhub-user-{username}": notebook_dir}
singleuser_env = dotenv_values("/srv/env/.env.singleuserr")
c.DockerSpawner.environment = dict(singleuser_env)
c.DockerSpawner.network_name = os.environ.get("DOCKER_NETWORK_NAME", "nginxproxy_energyguard_net")
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.remove = True
# Reduce the singleuser server's auth token cache from 300s (default) to 30s
# so that revoked tokens are detected quickly after backchannel logout.
c.DockerSpawner.args = ["--HubOAuth.cache_max_age=30"]

c.JupyterHub.hub_ip = "0.0.0.0"
c.JupyterHub.hub_connect_ip = "jupyterhub"


# ---------------- Dataset / Notebook provisioning ----------------
# The Data Management Server writes datasets and notebooks to a shared
# directory on the host: /home/energyguard/jupyterhub_data/
#   datasets/{username}/{dataset_name}/  →  mounted read-only  at /home/jovyan/work/datasets
#   notebooks/{username}/               →  mounted read-write at /home/jovyan/work/notebooks
#   pilot_datasets/{PARTNER}/           →  mounted read-only  at /home/jovyan/.pilot
#
# The JupyterHub container itself has /home/energyguard/jupyterhub_data
# bind-mounted as /jupyterhub_data (see docker-compose.yml), so the hook
# below can create the per-user directories on the host filesystem.
#
# Pilot datasets are platform-owned and byte-identical for every user, so they
# are NOT copied per user — there is one copy on disk, mounted read-only into
# every server, and the DMS provisions a symlink
#     work/datasets/<dataset_name> -> /home/jovyan/.pilot/<PARTNER>
# per user. Copying instead would multiply CEDER (~500 MB gzipped, several GB
# raw) by the number of users. The mount lives outside notebook_dir so the raw
# partner directories do not clutter the file browser; users reach them through
# the symlinks they asked for.

_JHUB_DATA_HOST = os.environ.get(
    "JUPYTERHUB_DATA_HOST_PATH", "/mnt/datadisk/volumes/jupyterhub_data"
)
_JHUB_DATA_CONTAINER = "/jupyterhub_data"  # as mounted in this JupyterHub container

# Must match PILOT_DATASETS_PREFIX / PILOT_MOUNT_PATH in the DMS .env — the DMS
# writes the exports here and points its symlinks at the mount path.
_PILOT_DIR_NAME = os.environ.get("PILOT_DATASETS_PREFIX", "pilot_datasets")
_PILOT_MOUNT_PATH = os.environ.get("PILOT_MOUNT_PATH", "/home/jovyan/.pilot")

DATASET_DIR_MODE = 0o755
NOTEBOOK_DIR_MODE = 0o777
# Auth dir stores the per-user mlflow-oidc-auth PAT cache. Writeable by
# jovyan inside the container (UID 1000). The token file itself is written
# 0o600 by the SDK.
AUTH_DIR_MODE = 0o777


async def pre_spawn_hook(spawner):
    username = spawner.user.name

    # Create per-user directories through the bind-mounted path so that they
    # exist on the host before DockerSpawner tries to bind-mount them into the
    # singleuser container.
    datasets_container_path = Path(_JHUB_DATA_CONTAINER) / "datasets" / username
    notebooks_container_path = Path(_JHUB_DATA_CONTAINER) / "notebooks" / username
    auth_container_path = Path(_JHUB_DATA_CONTAINER) / "auth" / username
    datasets_container_path.mkdir(parents=True, exist_ok=True)
    notebooks_container_path.mkdir(parents=True, exist_ok=True)
    auth_container_path.mkdir(parents=True, exist_ok=True)
    os.chmod(datasets_container_path, DATASET_DIR_MODE)
    os.chmod(notebooks_container_path, NOTEBOOK_DIR_MODE)
    os.chmod(auth_container_path, AUTH_DIR_MODE)

    # Shared, single-copy pilot data. Created here too so the bind-mount below
    # never causes Docker to invent a root-owned directory on the host when the
    # nightly export has not run yet.
    pilot_container_path = Path(_JHUB_DATA_CONTAINER) / _PILOT_DIR_NAME
    pilot_container_path.mkdir(parents=True, exist_ok=True)
    os.chmod(pilot_container_path, DATASET_DIR_MODE)

    # Tell DockerSpawner to bind-mount the host paths into the singleuser container.
    datasets_host = f"{_JHUB_DATA_HOST}/datasets/{username}"
    notebooks_host = f"{_JHUB_DATA_HOST}/notebooks/{username}"
    auth_host = f"{_JHUB_DATA_HOST}/auth/{username}"
    pilot_host = f"{_JHUB_DATA_HOST}/{_PILOT_DIR_NAME}"
    spawner.volumes[datasets_host] = {"bind": "/home/jovyan/work/datasets", "mode": "ro"}
    spawner.volumes[notebooks_host] = {"bind": "/home/jovyan/work/notebooks", "mode": "rw"}
    spawner.volumes[auth_host] = {"bind": "/srv/eg-auth", "mode": "rw"}
    # Read-only: pilot data is reference data, and every user shares this one
    # copy. "ro" is what makes the provisioned symlinks non-writable.
    spawner.volumes[pilot_host] = {"bind": _PILOT_MOUNT_PATH, "mode": "ro"}


c.Spawner.pre_spawn_hook = pre_spawn_hook
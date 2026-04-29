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
c.OAuthenticator.auth_refresh_age = 36000

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

# If you're behind Nginx Proxy Manager / reverse proxy, honor forwarded headers
c.JupyterHub.trust_xheaders = True

# Cookie security settings (no max_age — session expiry is handled by auth_refresh_age)
cookie_secure = os.environ.get("JH_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"}
c.JupyterHub.tornado_settings = {"cookie_options": {"secure": cookie_secure}}


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
# ---------------------------------------------------------------------------
def _get_revocation_time(username: str) -> float:
    """Return the revocation timestamp for *username*, or 0."""
    revocations = _read_revocations()
    return revocations.get(username.lower(), 0)


def _clear_revocation(username: str) -> None:
    with _revocation_lock:
        revocations = _read_revocations()
        revocations.pop(username.lower(), None)
        _write_revocations(revocations)


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
        _bcl_logger.info("refresh_user: REVOKING session for %s — returning False", user.name)
        return False
    _bcl_logger.info("refresh_user: %s not revoked, proceeding with default refresh", user.name)
    return None

c.GenericOAuthenticator.refresh_user_hook = _refresh_user


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
            # API returns {"api_tokens": [...]} not a bare list
            tokens = data.get("api_tokens", []) if isinstance(data, dict) else data

            # Delete only OAuth tokens (browser session). Skip API tokens —
            # the singleuser server's JUPYTERHUB_API_TOKEN is one of them, and
            # deleting it leaves the running container unable to talk to the
            # Hub (403 "Missing or invalid credentials").
            oauth_tokens = [
                t for t in tokens
                if isinstance(t, dict) and (
                    t.get("kind") == "oauth" or t.get("oauth_client")
                )
            ]
            _bcl_logger.info(
                "Found %d total tokens, %d OAuth tokens for %s",
                len(tokens), len(oauth_tokens), username,
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

# ---------------- Spawner (Docker) ----------------
c.JupyterHub.spawner_class = DockerSpawner
c.DockerSpawner.image = os.environ.get("DOCKER_NOTEBOOK_IMAGE")
notebook_dir = "/home/jovyan/work"
c.DockerSpawner.notebook_dir = notebook_dir
c.DockerSpawner.volumes = {"jhub-user-{username}": notebook_dir}
singleuser_env = dotenv_values("/srv/env/.env.singleuserr")
print(singleuser_env)
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
#
# The JupyterHub container itself has /home/energyguard/jupyterhub_data
# bind-mounted as /jupyterhub_data (see docker-compose.yml), so the hook
# below can create the per-user directories on the host filesystem.

_JHUB_DATA_HOST = os.environ.get(
    "JUPYTERHUB_DATA_HOST_PATH", "/home/energyguard/jupyterhub_data"
)
_JHUB_DATA_CONTAINER = "/jupyterhub_data"  # as mounted in this JupyterHub container

DATASET_DIR_MODE = 0o755
NOTEBOOK_DIR_MODE = 0o777


async def pre_spawn_hook(spawner):
    username = spawner.user.name

    # Create per-user directories through the bind-mounted path so that they
    # exist on the host before DockerSpawner tries to bind-mount them into the
    # singleuser container.
    datasets_container_path = Path(_JHUB_DATA_CONTAINER) / "datasets" / username
    notebooks_container_path = Path(_JHUB_DATA_CONTAINER) / "notebooks" / username
    datasets_container_path.mkdir(parents=True, exist_ok=True)
    notebooks_container_path.mkdir(parents=True, exist_ok=True)
    os.chmod(datasets_container_path, DATASET_DIR_MODE)
    os.chmod(notebooks_container_path, NOTEBOOK_DIR_MODE)

    # Tell DockerSpawner to bind-mount the host paths into the singleuser container.
    datasets_host = f"{_JHUB_DATA_HOST}/datasets/{username}"
    notebooks_host = f"{_JHUB_DATA_HOST}/notebooks/{username}"
    spawner.volumes[datasets_host] = {"bind": "/home/jovyan/work/datasets", "mode": "ro"}
    spawner.volumes[notebooks_host] = {"bind": "/home/jovyan/work/notebooks", "mode": "rw"}


c.Spawner.pre_spawn_hook = pre_spawn_hook
# energyguard_sdk/mlflow_sso/sso.py
from __future__ import annotations

import base64
import json
import os
import threading
import time
from urllib.parse import urlparse

import requests

# -----------------------------------------------------------------------------
# In-memory cache (per-kernel process)
# -----------------------------------------------------------------------------
_lock = threading.Lock()
_cached_token: str | None = None
_cached_exp: int | None = None  # epoch seconds

# Guard: avoid patching twice
_PATCHED_ATTR = "_eg_mlflow_sso_patched"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _debug(msg: str) -> None:
    if os.environ.get("EG_MLFLOW_SSO_DEBUG", "0") == "1":
        import sys
        print(f"[energyguard-sdk][mlflow-sso] {msg}", file=sys.stderr, flush=True)


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _jwt_exp(token: str) -> int | None:
    """Extract exp from JWT without verifying signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _mlflow_host() -> str:
    uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
    return _host(uri) if uri else ""


def _is_mlflow_url(url: str) -> bool:
    mh = _mlflow_host()
    return bool(mh) and _host(url) == mh


def _hub_vars() -> tuple[str, str, str] | None:
    hub_api = os.environ.get("JUPYTERHUB_API_URL", "").strip()
    hub_token = os.environ.get("JUPYTERHUB_API_TOKEN", "").strip()
    hub_user = os.environ.get("JUPYTERHUB_USER", "").strip()
    if not (hub_api and hub_token and hub_user):
        return None
    return hub_api.rstrip("/"), hub_token, hub_user


def _looks_like_keycloak_login(resp: requests.Response) -> bool:
    """
    Detect the 'Keycloak login HTML' failure mode that breaks MLflow JSON parsing.
    """
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ct:
        return True
    # Some proxies return HTML with wrong content-type; be defensive:
    body = (resp.text or "")[:2000].lower()
    return "<html" in body and ("sign in" in body or "keycloak" in body or "login-actions" in body)


# -----------------------------------------------------------------------------
# Token acquisition (from JupyterHub auth_state)
# -----------------------------------------------------------------------------
def get_access_token(force: bool = False, refresh_skew_seconds: int = 60) -> str | None:
    """
    Get a Keycloak access token from JupyterHub auth_state.
    Caches in memory and refreshes when near expiry.

    Requirements on the Hub side:
      - enable_auth_state=True
      - server scopes allow include_auth_state on /users/{name}
    """
    global _cached_token, _cached_exp

    hv = _hub_vars()
    if hv is None:
        return None
    hub_api, hub_token, hub_user = hv

    with _lock:
        now = int(time.time())

        if not force and _cached_token:
            if _cached_exp is None:
                # If no exp, assume usable (we'll retry on 401/403/HTML)
                return _cached_token
            if now < (_cached_exp - refresh_skew_seconds):
                return _cached_token

        # Fetch auth_state from Hub (requires include_auth_state + scopes)
        url = f"{hub_api}/users/{hub_user}"
        _debug(f"Fetching auth_state from {url}")

        r = requests.get(
            url,
            headers={"Authorization": f"token {hub_token}"},
            params={"include_auth_state": "1"},
            timeout=10,
        )
        r.raise_for_status()
        model = r.json()

        auth_state = model.get("auth_state") or {}
        token = auth_state.get("access_token")
        if not token:
            _debug("No access_token in auth_state (check enable_auth_state + scopes).")
            return None

        _cached_token = token
        _cached_exp = _jwt_exp(token)
        if _cached_exp:
            _debug(f"Token cached, exp={_cached_exp} (in {max(0, _cached_exp - now)}s)")
        else:
            _debug("Token cached, exp=unknown")

        return _cached_token


# -----------------------------------------------------------------------------
# requests patch (inject Authorization header for MLflow, refresh+retry)
# -----------------------------------------------------------------------------
def install_requests_patch() -> None:
    """
    Patch requests.Session.request so that any request to the MLflow host automatically
    gets Authorization: Bearer <Keycloak access token>.

    Also:
      - refreshes + retries once on 401/403
      - refreshes + retries once on "Keycloak login HTML" response
      - raises a clear RuntimeError if still getting login HTML (prevents MLflow JSON parse error)
    """
    if getattr(requests, _PATCHED_ATTR, False):
        return

    original = requests.Session.request

    def wrapped(self, method, url, **kwargs):
        if _is_mlflow_url(url):
            # Attach token
            tok = get_access_token(force=False)
            if tok:
                headers = kwargs.get("headers") or {}
                headers = dict(headers)
                headers["Authorization"] = f"Bearer {tok}"
                kwargs["headers"] = headers

            resp = original(self, method, url, **kwargs)

            # If we got HTML login page or rejection, refresh token and retry once
            if _looks_like_keycloak_login(resp) or resp.status_code in (401, 403, 500):
                _debug(
                    f"MLflow auth failed (status={resp.status_code}, html={_looks_like_keycloak_login(resp)}). "
                    "Refreshing token and retrying once."
                )
                tok2 = get_access_token(force=True)
                if tok2:
                    headers2 = dict(kwargs.get("headers") or {})
                    headers2["Authorization"] = f"Bearer {tok2}"
                    kwargs["headers"] = headers2
                    resp = original(self, method, url, **kwargs)

            # Still HTML? Raise a clearer error so MLflow doesn't choke on JSON
            if _looks_like_keycloak_login(resp):
                raise RuntimeError(
                    "MLflow request was redirected to Keycloak login HTML. "
                    "Your bearer token is missing/expired OR your proxy redirects API calls to interactive login. "
                    "Fix token refresh and/or configure the proxy to return 401 for API calls (no HTML redirect)."
                )

            return resp

        return original(self, method, url, **kwargs)

    requests.Session.request = wrapped
    setattr(requests, _PATCHED_ATTR, True)
    _debug("requests patch installed.")


def auto_install() -> None:
    """
    Safe "auto" entrypoint. Only patches when:
      - EG_MLFLOW_SSO_AUTO=1 (default)
      - MLFLOW_TRACKING_URI is set
      - JupyterHub env vars are present
    """
    if os.environ.get("EG_MLFLOW_SSO_AUTO", "1") != "1":
        return
    if not _mlflow_host():
        return
    if _hub_vars() is None:
        return
    install_requests_patch()
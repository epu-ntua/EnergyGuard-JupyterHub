"""Persistent MLflow PAT management for the EnergyGuard singleuser container.

Flow:

1. At every Python startup (via ``sitecustomize.py``) the SDK calls
   :func:`ensure_pat`.
2. The function reads ``$EG_MLFLOW_TOKEN_DIR/mlflow_token.json``
   (bind-mounted from the host so it survives container removal):

   - File exists, expiry more than :data:`REFRESH_SKEW_DAYS` away → load it
     into the env. No network call. This is the common path.
   - File exists, expiry within :data:`REFRESH_SKEW_DAYS` but still in the
     future → try to mint a fresh PAT. If the mint fails (e.g. the Keycloak
     access_token in JupyterHub's ``auth_state`` is expired), keep using the
     stored token — it's still valid for a while.
   - File missing or stored token expired → must mint. If that fails too,
     return ``False`` so ``sitecustomize.py`` falls back to the Bearer
     monkey patch.

3. A successful mint writes the new token back to disk (``0600``) atomically,
   under an ``fcntl`` advisory lock so concurrent kernels can't double-mint.

The PAT is never logged. mlflow-oidc-auth rotates the user's stored password
on every mint — so we mint as rarely as possible (once per ~10 months).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

import requests

LIFETIME_DAYS = 300        # ~10 months; mlflow-oidc-auth hard cap is 366.
REFRESH_SKEW_DAYS = 60     # refresh when stored token has <60d to live.
DEFAULT_TOKEN_DIR = "/srv/eg-auth"
TOKEN_FILENAME = "mlflow_token.json"
HTTP_TIMEOUT_S = 15


def _info(msg: str) -> None:
    print(f"[energyguard-sdk][mlflow-pat] {msg}", file=sys.stderr, flush=True)


def _debug(msg: str) -> None:
    if os.environ.get("EG_MLFLOW_SSO_DEBUG", "0") == "1":
        print(f"[energyguard-sdk][mlflow-pat][debug] {msg}", file=sys.stderr, flush=True)


def _token_dir() -> Path:
    return Path(os.environ.get("EG_MLFLOW_TOKEN_DIR", DEFAULT_TOKEN_DIR))


def _token_path() -> Path:
    return _token_dir() / TOKEN_FILENAME


def _mlflow_base() -> str:
    return os.environ.get("MLFLOW_TRACKING_URI", "").rstrip("/")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        return json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process advisory lock so concurrent kernels don't double-mint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_stored() -> Optional[dict]:
    path = _token_path()
    if not path.exists():
        return None
    try:
        with path.open("r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _debug(f"Could not read {path}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save_stored(record: dict) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(record, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _stored_freshness(record: dict) -> str:
    """Return 'fresh', 'expiring', or 'expired'."""
    exp = _parse_iso(record.get("expiration", ""))
    if exp is None:
        return "expired"
    now = datetime.now(timezone.utc)
    if exp <= now:
        return "expired"
    if exp <= now + timedelta(days=REFRESH_SKEW_DAYS):
        return "expiring"
    return "fresh"


def _get_keycloak_token() -> Optional[str]:
    from .sso import get_access_token

    return get_access_token(force=True)


def _mint(kc_token: str) -> Tuple[str, str, str]:
    """Mint a fresh PAT. Returns ``(username, token, expiration_iso)``."""
    base = _mlflow_base()
    if not base:
        raise RuntimeError("MLFLOW_TRACKING_URI not set")

    expiration = datetime.now(timezone.utc) + timedelta(days=LIFETIME_DAYS)
    expiration_iso = expiration.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    url = f"{base}/api/2.0/mlflow/users/access-token"
    _debug(f"PATCH {url} (exp={expiration_iso})")

    resp = requests.patch(
        url,
        json={"expiration": expiration_iso},
        headers={
            "Authorization": f"Bearer {kc_token}",
            "Content-Type": "application/json",
        },
        timeout=HTTP_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"mlflow-oidc-auth returned {resp.status_code}: {resp.text[:300]}"
        )
    token = (resp.json() or {}).get("token")
    if not token:
        raise RuntimeError("Mint response missing 'token'")

    payload = _jwt_payload(kc_token)
    username = (payload.get("email") or payload.get("preferred_username") or "").lower()
    if not username:
        raise RuntimeError("Cannot determine username from Keycloak token payload")

    return username, token, expiration_iso


def _apply_to_env(username: str, token: str) -> None:
    os.environ["MLFLOW_TRACKING_USERNAME"] = username
    os.environ["MLFLOW_TRACKING_PASSWORD"] = token
    # If a bearer token had been set previously, drop it so MLflow picks Basic.
    os.environ.pop("MLFLOW_TRACKING_TOKEN", None)


def _try_mint_and_save(reason: str) -> Optional[dict]:
    kc = _get_keycloak_token()
    if not kc:
        _info(f"PAT mint skipped ({reason}): no Keycloak access_token from Hub.")
        return None
    try:
        username, token, exp_iso = _mint(kc)
    except Exception as e:
        _info(f"PAT mint failed ({reason}): {e}")
        return None
    record = {
        "username": username,
        "token": token,
        "expiration": exp_iso,
        "minted_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        ),
    }
    try:
        _save_stored(record)
    except OSError as e:
        _info(f"PAT minted but could not persist to {_token_path()}: {e}")
    return record


def ensure_pat() -> bool:
    """Ensure ``MLFLOW_TRACKING_USERNAME``/``PASSWORD`` are set in env.

    Returns ``True`` if MLflow can authenticate via Basic auth on exit,
    ``False`` if no usable credentials are available (caller falls back to
    the Bearer monkey patch).
    """
    if not _mlflow_base():
        _info("MLFLOW_TRACKING_URI not set; skipping PAT setup.")
        return False

    try:
        with _file_lock(_token_path()):
            stored = _load_stored()
            state = _stored_freshness(stored) if stored else "missing"

            if state == "fresh":
                _apply_to_env(stored["username"], stored["token"])
                _debug(f"Stored PAT fresh (exp={stored['expiration']}).")
                return True

            if state == "expiring":
                # Try to refresh; on failure keep the still-valid stored token.
                _info(
                    f"PAT for {stored['username']} expires {stored['expiration']}; "
                    f"attempting refresh."
                )
                refreshed = _try_mint_and_save(reason="near expiry")
                if refreshed:
                    _apply_to_env(refreshed["username"], refreshed["token"])
                    _info(
                        f"PAT refreshed for {refreshed['username']} "
                        f"(new exp {refreshed['expiration']})."
                    )
                    return True
                # Refresh failed but stored is still usable.
                _apply_to_env(stored["username"], stored["token"])
                _info("PAT refresh failed; continuing with existing stored token.")
                return True

            # state == "expired" or "missing": must mint.
            minted = _try_mint_and_save(reason=state)
            if minted:
                _apply_to_env(minted["username"], minted["token"])
                _info(
                    f"PAT minted for {minted['username']} "
                    f"(exp {minted['expiration']}); cached at {_token_path()}."
                )
                return True
            return False
    except OSError as e:
        _info(f"Filesystem error around {_token_path()}: {e}")
        return False


if __name__ == "__main__":
    sys.exit(0 if ensure_pat() else 1)

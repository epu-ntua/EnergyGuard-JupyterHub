import os
import requests

def configure_mlflow():
    hub_token = os.environ.get("JUPYTERHUB_API_TOKEN")
    hub_api   = os.environ.get("JUPYTERHUB_API_URL")   # typically .../hub/api
    user      = os.environ.get("JUPYTERHUB_USER")

    if not hub_token or not hub_api or not user:
        return

    os.environ.setdefault("MLFLOW_TRACKING_URI", "https://mlflow.energy-guard.eu")

    # IMPORTANT:
    # /user may return auth_state: None on some JupyterHub versions.
    # /users/{name} is the reliable endpoint.  (see JupyterHub issue #5103)
    r = requests.get(
        f"{hub_api.rstrip('/')}/users/{user}",
        headers={"Authorization": f"token {hub_token}"},
        params={"include_auth_state": "1"},
        timeout=10,
    )
    r.raise_for_status()
    model = r.json()

    auth_state = model.get("auth_state") or {}
    access_token = auth_state.get("access_token")
    if not access_token:
        return

    os.environ["MLFLOW_TRACKING_TOKEN"] = access_token
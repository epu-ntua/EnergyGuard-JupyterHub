# sitecustomize.py — runs at every Python interpreter startup.
#
# Try the persistent-PAT flow first: read (or mint) an mlflow-oidc-auth
# personal access token, set MLFLOW_TRACKING_USERNAME/PASSWORD, and let
# MLflow's built-in basic-auth handle the rest. If that fails (e.g. the user
# has never logged into MLflow yet, so mlflow-oidc-auth has no user record to
# rotate), fall back to the Bearer-token requests monkey patch so MLflow
# calls still work using the live Keycloak access_token from auth_state.

try:
    from mlflow_sso.token_manager import ensure_pat
    from mlflow_sso.sso import auto_install

    if not ensure_pat():
        auto_install()
except Exception:
    pass

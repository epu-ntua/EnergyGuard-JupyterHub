import os
import sys

def _maybe_autoconfigure_mlflow():
    if os.environ.get("MLFLOW_AUTOLOGIN", "1") != "1":
        return
    try:
        import mlflow_authtoken
        mlflow_authtoken.configure_mlflow()
    except Exception as e:
        if os.environ.get("MLFLOW_AUTOLOGIN_DEBUG") == "1":
            print(f"[mlflow autologin] failed: {e!r}", file=sys.stderr, flush=True)

_maybe_autoconfigure_mlflow()

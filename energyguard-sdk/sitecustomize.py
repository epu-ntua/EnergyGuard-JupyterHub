# sitecustomize.py
try:
    from mlflow_sso.sso import auto_install
    auto_install()
except Exception as e:
    pass

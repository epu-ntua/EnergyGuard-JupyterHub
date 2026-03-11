import os
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

c.GenericOAuthenticator.oidc_issuer = ISSUER
c.GenericOAuthenticator.client_id = os.environ["KC_CLIENT_ID"]
c.GenericOAuthenticator.client_secret = os.environ["KC_CLIENT_SECRET"]

# JupyterHub callback URL (must match Keycloak client's redirect URI)
c.GenericOAuthenticator.oauth_callback_url = "https://jupyterhub.energy-guard.eu/hub/oauth_callback"

# Keycloak endpoints (explicit)
c.GenericOAuthenticator.authorize_url = f"{ISSUER}/protocol/openid-connect/auth"
c.GenericOAuthenticator.token_url     = f"{ISSUER}/protocol/openid-connect/token"
c.GenericOAuthenticator.userdata_url  = f"{ISSUER}/protocol/openid-connect/userinfo"
c.GenericOAuthenticator.userdata_method = "GET"


# Basic scopes + username claim
c.GenericOAuthenticator.scope = ["openid", "profile", "email", "groups", "offline_access"]
c.GenericOAuthenticator.username_claim = "preferred_username"

# Allow all authenticated users (tighten later with groups if you want)
c.GenericOAuthenticator.allow_all = True

# Optional: make Hub manage groups from claims (if you add groups mapper in Keycloak)
# c.GenericOAuthenticator.manage_groups = True
# c.GenericOAuthenticator.auth_state_groups_key = "groups"
# c.GenericOAuthenticator.allowed_groups = {"energyguard-users"}  # example
# c.GenericOAuthenticator.admin_groups = {"energyguard-admins"}   # example

# Optional but useful if you want tokens available to spawner / services
c.Authenticator.enable_auth_state = True
c.Authenticator.refresh_pre_spawn = True 

# instead of c.GenericOAuthenticator.logout_redirect_url = ...

post = "https://jupyterhub.energy-guard.eu/hub/login?next=%2Fhub%2F"
c.OAuthenticator.logout_redirect_url = (
    f"{ISSUER}/protocol/openid-connect/logout"
    f"?client_id={os.environ['KC_CLIENT_ID']}"
    f"&post_logout_redirect_uri={quote(post, safe='')}"
)


# ---------------- Hub basics ----------------
c.JupyterHub.bind_url = "http://0.0.0.0:8000"
c.JupyterHub.cookie_secret_file = "/srv/jupyterhub/jupyterhub_cookie_secret"
c.JupyterHub.db_url = "sqlite:////srv/jupyterhub/jupyterhub.sqlite"

# If you're behind Nginx Proxy Manager / reverse proxy, honor forwarded headers
c.JupyterHub.trust_xheaders = True

# Enable cross-site logout iframe compatibility (Keycloak front-channel logout).
# Security tradeoff: SameSite=None allows cookie sending in third-party contexts.
cookie_samesite = os.environ.get("JH_COOKIE_SAMESITE", "none").strip().lower()
cookie_secure = os.environ.get("JH_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"}
if cookie_samesite not in {"none", "lax", "strict", ""}:
    cookie_samesite = "none"

cookie_options = {"secure": cookie_secure}
if cookie_samesite:
    cookie_options["samesite"] = cookie_samesite
c.JupyterHub.tornado_settings = {"cookie_options": cookie_options}

# Needed so the single-user server (with JUPYTERHUB_API_TOKEN) can read its own auth_state
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

c.JupyterHub.hub_ip = "0.0.0.0"
c.JupyterHub.hub_connect_ip = "jupyterhub"


# ---------------- Dataset / Notebook provisioning ----------------
# The Data Management Server writes datasets and notebooks to a shared
# directory on the host: /home/energyguard/jupyterhub_data/
#   datasets/{username}/{dataset_name}/  →  mounted read-only  at /home/jovyan/datasets
#   notebooks/{username}/               →  mounted read-write at /home/jovyan/notebooks
#
# The JupyterHub container itself has /home/energyguard/jupyterhub_data
# bind-mounted as /jupyterhub_data (see docker-compose.yml), so the hook
# below can create the per-user directories on the host filesystem.

_JHUB_DATA_HOST = os.environ.get(
    "JUPYTERHUB_DATA_HOST_PATH", "/home/energyguard/jupyterhub_data"
)
_JHUB_DATA_CONTAINER = "/jupyterhub_data"  # as mounted in this JupyterHub container


async def pre_spawn_hook(spawner):
    username = spawner.user.name

    # Create per-user directories through the bind-mounted path so that they
    # exist on the host before DockerSpawner tries to bind-mount them into the
    # singleuser container.
    datasets_container_path = Path(_JHUB_DATA_CONTAINER) / "datasets" / username
    notebooks_container_path = Path(_JHUB_DATA_CONTAINER) / "notebooks" / username
    datasets_container_path.mkdir(parents=True, exist_ok=True)
    notebooks_container_path.mkdir(parents=True, exist_ok=True)

    # Tell DockerSpawner to bind-mount the host paths into the singleuser container.
    datasets_host = f"{_JHUB_DATA_HOST}/datasets/{username}"
    notebooks_host = f"{_JHUB_DATA_HOST}/notebooks/{username}"
    spawner.volumes[datasets_host] = {"bind": "/home/jovyan/datasets", "mode": "ro"}
    spawner.volumes[notebooks_host] = {"bind": "/home/jovyan/notebooks", "mode": "rw"}


c.Spawner.pre_spawn_hook = pre_spawn_hook

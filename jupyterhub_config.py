import os
from dockerspawner import DockerSpawner
from urllib.parse import quote

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
c.GenericOAuthenticator.scope = ["openid", "profile", "email"]
c.GenericOAuthenticator.username_claim = "preferred_username"

# Allow all authenticated users (tighten later with groups if you want)
c.GenericOAuthenticator.allow_all = True

# Optional: make Hub manage groups from claims (if you add groups mapper in Keycloak)
# c.GenericOAuthenticator.manage_groups = True
# c.GenericOAuthenticator.auth_state_groups_key = "groups"
# c.GenericOAuthenticator.allowed_groups = {"energyguard-users"}  # example
# c.GenericOAuthenticator.admin_groups = {"energyguard-admins"}   # example

# Optional but useful if you want tokens available to spawner / services
# c.Authenticator.enable_auth_state = True

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

# ---------------- Spawner (Docker) ----------------
c.JupyterHub.spawner_class = DockerSpawner
c.DockerSpawner.image = os.environ.get("DOCKER_NOTEBOOK_IMAGE", "jupyter/base-notebook:latest")

notebook_dir = "/home/jovyan/work"
c.DockerSpawner.notebook_dir = notebook_dir
c.DockerSpawner.volumes = {"jhub-user-{username}": notebook_dir}

c.DockerSpawner.network_name = os.environ.get("DOCKER_NETWORK_NAME", "nginxproxy_energyguard_net")
c.DockerSpawner.use_internal_ip = True

c.JupyterHub.hub_ip = "0.0.0.0"
c.JupyterHub.hub_connect_ip = "jupyterhub"

# EnergyGuard JupyterHub Deployment

This document describes how JupyterHub is deployed in the EnergyGuard platform (ICCS premises). It gives users constant access to notebooks without GPU resources.

---

## Architecture Overview

JupyterHub runs as a **Docker based deployment** with two container images.

1. **Hub container.** The central JupyterHub process. It handles authentication, user management and the spawning of notebook servers.
2. **Singleuser containers.** One per user, each running a full JupyterLab environment with a preconfigured Python kernel and the packages listed below.

The Hub uses **DockerSpawner** to start an isolated Docker container for each user on demand. All containers share the Docker network `nginxproxy_energyguard_net` and sit behind an Nginx reverse proxy.

```
                          ┌─────────────────┐
                          │  Nginx Reverse  │
                          │      Proxy      │
                          └────────┬────────┘
                          ┌────────▼────────┐
                          │    JupyterHub   │
                          │ (Hub Container) │
                          │  - Auth (OIDC)  │
                          │ - DockerSpawner │
                          └────────┬────────┘
                                   │ spawns via Docker
            ┌──────────────────────┼──────────────────────┐
  ┌─────────▼─────────┐  ┌─────────▼─────────┐  ┌─────────▼─────────┐
  │     Singleuser    │  │     Singleuser    │  │     Singleuser    │
  │       User A      │  │       User B      │  │       User C      │
  │    (JupyterLab)   │  │    (JupyterLab)   │  │    (JupyterLab)   │
  └────┬──────────┬───┘  └────┬──────────┬───┘  └────┬──────────┬───┘
       │          │           │          │           │          │
  ┌────▼───────┐  │      ┌────▼───────┐  │      ┌────▼───────┐  │
  │User A only │  │      │User B only │  │      │User C only │  │
  │work/     rw│  │      │work/     rw│  │      │work/     rw│  │
  │datasets  ro│  │      │datasets  ro│  │      │datasets  ro│  │
  │notebooks rw│  │      │notebooks rw│  │      │notebooks rw│  │
  │auth      rw│  │      │auth      rw│  │      │auth      rw│  │
  └────────────┘  │      └────────────┘  │      └────────────┘  │
                  └──────────────────────┼──────────────────────┘
                                         │ read-only, shared
                           ┌─────────────▼────────────┐
                           │     pilot_datasets/      │
                           │ ONE shared copy on disk  │
                           │ (nightly export by DMS)  │
                           └──────────────────────────┘
```

Each singleuser container gets its own `work/` volume and its own `datasets/`, `notebooks/` and `auth/` directories, which no other user can see. All containers also mount the one shared copy of the pilot data. The paths are listed in [Datasets and Notebooks Volumes](#datasets-and-notebooks-volumes).

---

## Hub Container

The Hub image (`Dockerfile`) is built on `python:3.11-slim` and installs JupyterHub 4, DockerSpawner 13, OAuthenticator and `configurable-http-proxy`. It listens on port **8009** for the Hub and on port **8002** for Keycloak backchannel logout.

### Authentication (Keycloak OIDC)

Users log in through **Keycloak** with OpenID Connect. The Hub is registered as the `jupyterhub` client in the `EnergyGuard` realm, and its callback URL is `https://jupyterhub.energy-guard.eu/hub/oauth_callback`. Every user who can log in to Keycloak is allowed in, and the JupyterHub username is the Keycloak `preferred_username`.

Auth state is enabled (`enable_auth_state = True`). The Hub stores the user's Keycloak access token in its auth state, and the singleuser containers can read it. The MLflow integration described below depends on this.

The Hub checks each user's session with Keycloak every 30 seconds (`auth_refresh_age = 30`) and again before every spawn.

### Roles and scopes

Two custom roles let a singleuser server read the auth state of the user who owns it, and no one else's.

| Role | Scopes |
|------|--------|
| `user` | `self`, `admin:auth_state!user` |
| `server` | `users:activity!user`, `access:servers!server`, `admin:auth_state!user` |

The scopes `self`, `users:activity!user` and `access:servers!server` are JupyterHub defaults. Both roles add `admin:auth_state!user`, which lets the singleuser container call `/hub/api/users/{username}?include_auth_state=1` and get the user's stored Keycloak access token. The `!user` filter limits this to the owner's own auth state, so one user's server cannot read another user's token.

### Logout

Logging out of JupyterHub sends the browser to the Keycloak end session endpoint. This signs the user out of Keycloak and then returns them to the JupyterHub login page.

Logging out of any other EnergyGuard service also ends the JupyterHub session. Keycloak sends a backchannel logout request to a small HTTP server that the Hub runs on port **8002**. When it receives one, the Hub

1. adds the user to a revocation list (`/srv/jupyterhub/revoked_users.json`), where they stay for 5 minutes,
2. deletes the user's browser OAuth tokens through the JupyterHub API, so the notebook tab is logged out while the server itself keeps running,
3. rejects the user's session on their next Hub request and invalidates any old login cookie in the browser.

Logging in to Keycloak again after the revocation clears it. Singleuser servers cache Hub tokens for 30 seconds, so a logout reaches the notebook within about that time.

Deleting tokens through the API requires `BCL_API_TOKEN` in `.env`. Without it the revocation still works, but the notebook tab stays logged in until its token expires.

After login, the Hub never redirects a user to a URL that belongs to another user's server. It sends them to their own server instead. This prevents a login loop when a browser still has the previous user's notebook URL open.

### Hub environment variables (`.env`)

| Variable | Description |
|----------|-------------|
| `KC_REALM` | Keycloak realm (default `EnergyGuard`) |
| `KC_BASE_URL` | Public Keycloak base URL |
| `KC_CLIENT_ID` | Keycloak client ID (`jupyterhub`) |
| `KC_CLIENT_SECRET` | Keycloak client secret |
| `JUPYTERHUB_CRYPT_KEY` | Key that encrypts the stored auth state |
| `DOCKER_NOTEBOOK_IMAGE` | Singleuser image (`energyguard-singleuser:latest`) |
| `DOCKER_NETWORK_NAME` | Shared Docker network (default `nginxproxy_energyguard_net`) |
| `JH_COOKIE_SECURE` | Set the Secure flag on Hub cookies (default `true`) |
| `BCL_API_TOKEN` | API token for the backchannel logout service (optional) |
| `BCL_PORT` | Port of the backchannel logout server (default `8002`) |
| `JUPYTERHUB_DATA_HOST_PATH` | Host path of the shared data directory (default `/mnt/datadisk/volumes/jupyterhub_data`) |
| `PILOT_DATASETS_PREFIX` | Name of the pilot data directory (default `pilot_datasets`) |
| `PILOT_MOUNT_PATH` | Container path of the pilot data mount (default `/home/jovyan/.pilot`) |

---

## Singleuser Container (Custom Kernel)

The singleuser image (`Dockerfile.singleuser`) is spawned for each user. It is built on `jupyter/base-notebook:latest`.

### Default kernel `eg-default` (EnergyGuard Python)

A custom IPython kernel named **`eg-default`**, shown as "EnergyGuard (Python)", is installed system wide. It is the **default kernel** for all new notebooks (set in `/etc/jupyter/jupyter_server_config.py`).

### Installed Packages

| Package | Version |
|---------|---------|
| `jupyterhub` | 4.1.6 |
| `mlflow` | 3.8.1 |
| `torch` | 2.9.1 |
| `pytorch_lightning` | 2.6.1 |
| `pandas` | 2.3.3 |
| `numpy` | 2.3.1 |
| `scikit-learn` | 1.8.0 |
| `matplotlib` | 3.10.8 |
| `pyarrow` | 22.0.0 |
| `boto3` | 1.42.34 |
| `minio` | 7.2.20 |
| `requests` | 2.32.5 |
| `python-dotenv` | 1.2.1 |
| `tqdm` | 4.67.1 |

The image also includes the `zip` command line tool and the **energyguard-sdk** (see below).

### Environment Variables

These are set in `Dockerfile.singleuser`.

| Variable | Value | Description |
|----------|-------|-------------|
| `MLFLOW_TRACKING_URI` | `https://mlflow.energy-guard.eu/` | URL of the MLflow server |
| `MLFLOW_S3_ENDPOINT_URL` | `https://minio-backend.energy-guard.eu/` | MinIO S3 endpoint for artifact storage |
| `EG_MLFLOW_SSO_AUTO` | `1` | Enable the Bearer token fallback (`0` or `1`) |
| `EG_MLFLOW_SSO_DEBUG` | `0` | Enable SDK debug logging (`0` or `1`) |

The SDK also reads `EG_MLFLOW_TOKEN_DIR`, the directory of the MLflow token file. It is not set in the image, so the SDK uses `/srv/eg-auth`.

---

## EnergyGuard SDK

The SDK lives in `energyguard-sdk/`.

MLflow is protected by Keycloak through mlflow-oidc-auth, and the MLflow Python client cannot log in with OIDC by itself. The energyguard-sdk logs users in to MLflow automatically, so calls like `mlflow.start_run()` or `mlflow.log_metric()` work with no authentication code.

### How it works

`sitecustomize.py` runs every time Python starts. It first tries to set up a **personal access token (PAT)**. If that fails, it installs a **Bearer token patch** for `requests`.

#### 1. Personal access token

This is handled by `ensure_pat()` in `mlflow_sso/token_manager.py`.

The SDK keeps an MLflow PAT in `/srv/eg-auth/mlflow_token.json`. This directory is bind mounted from the host, so the token survives when the container is removed.

* If the stored token has more than 60 days left, the SDK uses it with no network call.
* If it has less than 60 days left, the SDK tries to get a new one and keeps the old one if that fails.
* If there is no token or it has expired, the SDK gets a new one.

To get a new PAT, the SDK takes the user's Keycloak token from the Hub and calls `PATCH /api/2.0/mlflow/users/access-token` on MLflow. The new token is valid for 300 days and is saved with mode `0600`. A file lock stops two kernels from requesting a token at the same time. The SDK then sets `MLFLOW_TRACKING_USERNAME` and `MLFLOW_TRACKING_PASSWORD`, and MLflow uses basic authentication.

Getting a new PAT fails if the user has never logged in to the MLflow web UI, because mlflow-oidc-auth has no account for them yet.

#### 2. Bearer token fallback

This is handled by `auto_install()` in `mlflow_sso/sso.py`. It runs only when no PAT is available, `EG_MLFLOW_SSO_AUTO=1`, `MLFLOW_TRACKING_URI` is set and the JupyterHub variables (`JUPYTERHUB_API_URL`, `JUPYTERHUB_API_TOKEN`, `JUPYTERHUB_USER`) are present. It patches `requests.Session.request`.

* `get_access_token()` reads the user's Keycloak token from the Hub API and caches it in memory. It fetches it again when it is less than 60 seconds from expiry.
* Every request to the MLflow host gets an `Authorization: Bearer` header with this token.
* On a 401, 403 or 500 response, or when Keycloak returns its HTML login page, the SDK fetches the token again and retries once. If it still gets the login page, it raises a `RuntimeError` that explains the problem.
* A lock prevents concurrent token refreshes.

### From the User's Perspective

Users write standard MLflow code with no authentication code.

```python
import mlflow

mlflow.set_experiment("my-experiment")
with mlflow.start_run():
    mlflow.log_param("lr", 0.01)
    mlflow.log_metric("accuracy", 0.95)
```

Users should log in to the MLflow web UI once before their first MLflow call from a notebook. Setting `EG_MLFLOW_SSO_DEBUG=1` prints the SDK's debug messages to stderr.

---

## Datasets and Notebooks Volumes

Each user gets three personal directories bind mounted into their container, plus one directory shared by **all** users.

| Mount | Host Path | Container Path | Mode |
|-------|-----------|---------------|------|
| **Datasets** | `{data}/datasets/{username}` | `/home/jovyan/work/datasets` | **Read-only** |
| **Notebooks** | `{data}/notebooks/{username}` | `/home/jovyan/work/notebooks` | **Read-write** |
| **Auth** | `{data}/auth/{username}` | `/srv/eg-auth` | **Read-write** |
| **Pilot datasets** | `{data}/pilot_datasets` | `/home/jovyan/.pilot` | **Read-only**, shared |

`{data}` is the shared host directory set by `JUPYTERHUB_DATA_HOST_PATH` (default `/mnt/datadisk/volumes/jupyterhub_data`). It is also mounted into the Hub container at `/jupyterhub_data`, so the pre spawn hook can create these paths.

`PILOT_DATASETS_PREFIX` and `PILOT_MOUNT_PATH` default to the same values as in the Data Management Server (DMS). If you change one of them, change it in **both** services. Otherwise the DMS will create symlinks to a path that is not mounted.

### Provisioning

A **pre spawn hook** in `jupyterhub_config.py` runs before each user container starts. It

1. creates the user's `datasets/`, `notebooks/` and `auth/` directories on the host if they do not exist,
2. creates the shared `pilot_datasets/` directory if it does not exist, so that Docker does not create it owned by root,
3. sets the directory permissions,
4. adds the bind mounts to the spawner configuration.

Mounts are added each time a server starts. A user whose server is already running keeps the old mounts until they stop and start it.

### Data Flow

The **Data Management Server** (a separate FastAPI service) fills the datasets and notebooks directories by downloading files from MinIO. When a user's container starts, the files are already in `/home/jovyan/work/datasets/` and `/home/jovyan/work/notebooks/`.

* **Datasets** are read-only, so users cannot modify their data by accident.
* **Notebooks** are read-write, so users can edit and save their work.
* **Auth** holds the user's MLflow token file written by the SDK.
* Each user also has a personal Docker volume (`jhub-user-{username}`) mounted at `/home/jovyan/work/` for any other files they create.

### Pilot datasets

The pilot data consists of the seven partner datasets (`RDN`, `CEDER`, `BER`, `CEA`, `CARTIF`, `REA`, `ENGREEN`). It is the same for every user, so there is **one copy on disk** for everyone. CEDER alone has about 127M rows.

Every night the DMS exports each partner from the CARTIF data lake to `{data}/pilot_datasets/{PARTNER}/{PARTNER}.csv.gz`. This directory is mounted **read-only** at `/home/jovyan/.pilot` in every singleuser container. The DMS endpoint `POST /api/v1/provision/pilot` needs this mount. It gives a user access by creating a symlink in their datasets directory.

```
/home/jovyan/work/datasets/{dataset_name}  ->  /home/jovyan/.pilot/{PARTNER}
```

The DMS creates the symlink on the host, inside the user's datasets directory. It appears in a running server immediately, with no restart. A restart is only needed once, for a server that was started before the `.pilot` mount existed.

The symlink points to a path inside the container, so on the host it looks broken. This is expected. It works inside the container.

The `.pilot` mount is outside `/home/jovyan/work`, so users do not see the raw partner directories in the file browser. They only see the datasets they added, under the names they chose.

In a notebook, read the data as usual.

```python
pd.read_csv('datasets/REA Pilot Data/REA.csv.gz')
```

---

## Networking

All services (Hub, singleuser containers, Nginx proxy, MLflow and others) share the external Docker network `nginxproxy_energyguard_net`. Singleuser containers reach the Hub through Docker's internal DNS name `jupyterhub`, and the Hub reaches them by their internal IP.

---

## External Services

| Service | URL | Purpose |
|---------|-----|---------|
| **JupyterHub** | `https://jupyterhub.energy-guard.eu` | This deployment |
| **Keycloak** | `https://keycloak.toolbox.epu.ntua.gr` | Identity provider (OIDC) |
| **MLflow** | `https://mlflow.energy-guard.eu` | Experiment tracking |
| **MinIO** | `https://minio-backend.energy-guard.eu` | S3 compatible object storage |

---

## Quick Start

Before starting, make sure that

1. `.env` exists with the Hub configuration (copy `.env.example` and fill in the values),
2. the Docker network `nginxproxy_energyguard_net` exists,
3. Keycloak has a `jupyterhub` client in the `EnergyGuard` realm, with the Hub callback URL as a redirect URI and a backchannel logout URL that reaches port 8002 of the Hub,
4. the shared data directory exists on the host.

Build both images and start the Hub.

```bash
# Singleuser image
docker build -t energyguard-singleuser:latest -f Dockerfile.singleuser .

# Hub image
docker compose build

# Start the Hub
docker compose up -d
```

`jupyterhub_config.py` is mounted read-only over the copy inside the image (see `docker-compose.yml`), so after a config change you only need to restart the Hub.

```bash
docker compose restart jupyterhub
```

Changes to the pre spawn hook do not need a rebuild of the singleuser image either, because mounts are added when each server starts. Changes to `Dockerfile.singleuser` or to `energyguard-sdk/` need a rebuild of the singleuser image. In both cases, users with a running server must stop and start it to get the change.

---

## File Structure

```
JupyterHub/
├── docker-compose.yml          # Hub container orchestration
├── Dockerfile                  # Hub image (JupyterHub + DockerSpawner)
├── Dockerfile.singleuser       # Singleuser image (JupyterLab + kernel + SDK)
├── jupyterhub_config.py        # Hub configuration (auth, logout, spawner, volumes, hooks)
├── .env.example                # Example Hub environment variables
└── energyguard-sdk/            # SDK for MLflow login
    ├── pyproject.toml          # Package metadata
    ├── sitecustomize.py        # Runs at Python startup
    └── mlflow_sso/
        ├── __init__.py
        ├── token_manager.py    # MLflow personal access token
        └── sso.py              # Bearer token fallback (requests patch)
```

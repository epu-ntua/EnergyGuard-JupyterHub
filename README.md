# EnergyGuard JupyterHub Deployment

This document describes how JupyterHub is deployed in the EnergyGuard platform (ICCS premises) for constant access without GPU resources.

---

## Architecture Overview

JupyterHub runs as a **Docker-based deployment** with two main container images:

1. **Hub container** — the central JupyterHub process that handles authentication, user management, and spawning individual notebook servers.
2. **Singleuser containers** — one per user, each running a full JupyterLab environment with a pre-configured Python kernel and all necessary packages.

The Hub uses **DockerSpawner** to create isolated Docker containers for each user on demand. All containers communicate over a shared Docker network (`nginxproxy_energyguard_net`) behind an Nginx reverse proxy.

```
                    ┌─────────────────┐
                    │  Nginx Reverse  │
                    │     Proxy       │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   JupyterHub    │
                    │  (Hub Container)│
                    │  - Auth (OIDC)  │
                    │  - DockerSpawner│
                    └────────┬────────┘
                             │  spawns via Docker
              ┌──────────────┼─────────────┐
              │              │             │
        ┌─────▼──────┐ ┌─────▼─────┐ ┌─────▼─────┐
        │ Singleuser │ │ Singleuser│ │ Singleuser│
        │  User A    │ │  User B   │ │  User C   │
        │ (JupyterLab│ │           │ │           │
        │  + Kernel) │ │           │ │           │
        └────────────┘ └───────────┘ └───────────┘
```

---

## Hub Container

### Authentication (Keycloak OIDC)

Users authenticate via **Keycloak** using the OpenID Connect protocol. The Hub is registered as a Keycloak client in the `EnergyGuard` realm.

Key aspects:
- **Auth state persistence:** Enabled (`enable_auth_state = True`). The user's Keycloak access token is stored in JupyterHub's auth state and made available to singleuser containers. This is critical for the MLflow SSO integration described below.
<!-- - **Logout:** Configured to perform Keycloak front-channel logout so users are signed out of all connected services. -->

Custom roles grant singleuser servers permission to read the spawning user's auth state (and only of that user):

- **`user` role** — scopes: `self`, `admin:auth_state!user`
- **`server` role** — scopes: `users:activity!user`, `access:servers!server`, `admin:auth_state!user`

Most of these scopes (`self`, `users:activity!user`, `access:servers!server`) are JupyterHub defaults:

The addition to both roles is `admin:auth_state!user`, which allows the singleuser container to call the Hub's `/hub/api/users/{username}/auth-state` endpoint and retrieve the user's stored Keycloak access token. The `!user` suffix is a JupyterHub scope filter that restricts access to the auth state of that specific user only — a singleuser server cannot read another user's token.

This allows the energyguard-sdk running inside each container to fetch the user's Keycloak token from the Hub API so the energyguard-sdk can perform MLflow calls

---

## Singleuser Container (Custom Kernel)

**Dockerfile:** `Dockerfile.singleuser`

Built on `jupyter/base-notebook:latest`, this is the image spawned for each user.

### Default Kernel: `eg-default` (EnergyGuard Python)

A custom IPython kernel named **`eg-default`** is installed system-wide and set as the **default kernel** for all new notebooks (via `jupyter_server_config.py`).

### Installed Packages

| Package | Version | 
|---------|---------|
| `mlflow` | 3.8.1 | 
| `torch` | 2.10.0 | 
| `pandas` | 2.3.3 | 
| `numpy` | 2.4.1 | 
| `scikit-learn` | 1.8.0 | 
| `matplotlib` | 3.10.8 | 
| `pyarrow` | 22.0.0 | 
| `boto3` | 1.42.34 | 
| `minio` | 7.2.20 | 
| `requests` | 2.32.5 |
| `python-dotenv` | 1.2.1 | 
| `tqdm` | 4.67.1 | 

Additionally, the **energyguard-sdk** is installed (see below).

### Environment Variables

The singleuser containers use the following environment variables:

| Variable | Description |
|----------|-------------|
| `MLFLOW_TRACKING_URI` | URL of the MLflow server |
| `MLFLOW_S3_ENDPOINT_URL` | MinIO S3 endpoint for artifact storage |
| `EG_MLFLOW_SSO_AUTO` | Enable automatic MLflow SSO (set to `0` or`1`) |
| `EG_MLFLOW_SSO_DEBUG` | Enable SDK debug logging (set to `0` or `1`) |

---

## EnergyGuard SDK

**Location:** `energyguard-sdk/`

The energyguard-sdk solves the following problem: **MLflow is protected behind Keycloak authentication**, but the MLflow Python client does not natively support OIDC. The SDK transparently injects the user's Keycloak access token into all HTTP requests made to MLflow, so that calls like `mlflow.log_metric()` or `mlflow.start_run()` work without any manual authentication.


### Key Components

1. **`sitecustomize.py`** — Runs automatically when Python starts. Calls `auto_install()` which checks if the environment is a JupyterHub singleuser container (by looking for `JUPYTERHUB_API_URL`, `JUPYTERHUB_API_TOKEN`, etc.) and if so, patches the `requests` library.

2. **`mlflow_sso/sso.py`** — Core implementation:
   - **`get_access_token()`** — Fetches the user's Keycloak token from JupyterHub's auth state API. Tokens are cached in memory and automatically refreshed before expiration.
   - **`install_requests_patch()`** — Patches `requests.Session.request()` to intercept requests to the MLflow host and inject the Bearer token.
   - **Retry logic** — On 401, 403, or 500 responses (or if a Keycloak login page HTML is detected instead of JSON), the SDK forces a token refresh and retries the request once.
   - **Thread-safe** — Token acquisition uses a lock to prevent concurrent refresh races.

### From the User's Perspective

Users write standard MLflow code with zero authentication boilerplate:

```python
import mlflow

mlflow.set_experiment("my-experiment")
with mlflow.start_run():
    mlflow.log_param("lr", 0.01)
    mlflow.log_metric("accuracy", 0.95)
    # Everything just works — auth is handled automatically
```

---

## Datasets and Notebooks Volumes

Each user gets two dedicated directories that are bind-mounted into their container:

| Mount | Container Path | Mode |
|-------|---------------|------|
| **Datasets** | `/home/jovyan/work/datasets` | **Read-only** |
| **Notebooks** | `/home/jovyan/work/notebooks` | **Read-write** |

### Provisioning

A **pre-spawn hook** in `jupyterhub_config.py` runs before each user container starts:

1. Creates the user's `datasets/` and `notebooks/` directories on the host if they don't exist
2. Sets appropriate permissions
3. Adds the bind-mount entries to the spawner configuration


### Data Flow

Datasets and notebooks are populated by the **Data Management Server** (a separate FastAPI service), which downloads files from MinIO into the host directories. When a user logs in and their container spawns, the files are already available at `/home/jovyan/work/datasets/` and `/home/jovyan/work/notebooks/`.

- **Datasets** are read-only to prevent accidental modification of shared data.
- **Notebooks** are read-write so users can edit and save their work.
- Each user also has a personal persistent volume (`jhub-user-{username}`) mounted at `/home/jovyan/work/` for any other files they create.

---

## Networking

All services (Hub, singleuser containers, Nginx proxy, MLflow, etc.) share the external Docker network `nginxproxy_energyguard_net`. The Hub's singleuser containers communicate with the Hub via Docker-internal DNS (`jupyterhub`), avoiding external routing.

---

## External Services

| Service | URL | Purpose |
|---------|-----|---------|
| **Keycloak** | `https://keycloak.toolbox.epu.ntua.gr` | Identity provider (OIDC) |
| **MLflow** | `https://mlflow.energy-guard.eu` | Experiment tracking |
| **MinIO** | `https://minio-backend.energy-guard.eu` | S3-compatible object storage |

---

## Quick Start

```bash
# Build both images
docker compose build

# Start the Hub
docker compose up -d

# The singleuser image (energyguard-singleuser:latest) must also be built:
docker build -t energyguard-singleuser:latest -f Dockerfile.singleuser .
```

Ensure the following are in place:
1. `.env` — Hub configuration (Keycloak credentials, crypt key, etc.)
3. The Docker network `nginxproxy_energyguard_net` exists
4. Keycloak is configured with a `jupyterhub` client in the `EnergyGuard` realm

---

## File Structure

```
JupyterHub/
├── docker-compose.yml          # Hub container orchestration
├── Dockerfile                  # Hub image (JupyterHub + DockerSpawner)
├── Dockerfile.singleuser       # Singleuser image (JupyterLab + kernel + SDK)
├── jupyterhub_config.py        # Hub configuration (auth, spawner, volumes, hooks)
├── .env.example                # Example hub environment variables
├── energyguard-sdk/            # Custom SDK for MLflow SSO
│   ├── pyproject.toml          # Package metadata
│   ├── sitecustomize.py        # Auto-init on Python startup
│   └── mlflow_sso/
│       ├── __init__.py
│       └── sso.py              # Token injection and request patching
└── python_env_files/           # Legacy scripts (superseded by SDK)
    ├── mlflow_authtoken.py
    └── sitecustomize.py
```

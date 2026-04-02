FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl nodejs npm \
  && npm install -g configurable-http-proxy \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "jupyterhub==4.*" \
    "dockerspawner==13.*" \
    "oauthenticator" \
    "jupyterlab==4.*" \
    "dotenv" \
    "httpx"

# Create config dir + copy config from build context
RUN mkdir -p /srv/jupyterhub
RUN mkdir -p /srv/env
COPY ./jupyterhub_config.py /srv/jupyterhub/jupyterhub_config.py

WORKDIR /srv/jupyterhub
CMD ["jupyterhub", "-f", "./jupyterhub_config.py"]

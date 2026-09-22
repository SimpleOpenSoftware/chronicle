"""Stable managed service URLs; the gateway chooses an instance per request."""

import os
from urllib.parse import quote


def gateway_url(deployment: str, endpoint: str) -> str:
    url = os.environ.get("SERVICE_GATEWAY_URL", "").rstrip("/")
    if not url:
        raise ValueError("Managed service references require SERVICE_GATEWAY_URL")
    return f"{url}/deployments/{quote(deployment, safe='')}/proxy/{quote(endpoint, safe='')}"


def gateway_token() -> str:
    token = os.environ.get("SERVICE_GATEWAY_TOKEN", "")
    if not token:
        raise ValueError("Managed service references require SERVICE_GATEWAY_TOKEN")
    return token

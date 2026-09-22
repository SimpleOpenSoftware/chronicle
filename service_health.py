"""Shared endpoint readiness and model identity evaluation."""

import httpx


def body_ready(data):
    return not isinstance(data, dict) or (
        data.get("status")
        not in ("loading", "initializing", "unhealthy", "degraded", "error")
        and data.get("healthy") is not False
        and data.get("ready") is not False
    )


def field(data, key):
    for part in key.split("."):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


async def probe(client, endpoint):
    try:
        response = await client.get(endpoint.health_url, timeout=3)
        response.raise_for_status()
        data = response.json()
        if not body_ready(data):
            return {"healthy": False, "reason": "Not ready", "data": data}
        for key, value in endpoint.readiness.items():
            if field(data, key) != value:
                return {
                    "healthy": False,
                    "reason": f"Readiness mismatch: {key}",
                    "data": data,
                }
        if endpoint.identity_url:
            identity_response = await client.get(endpoint.identity_url, timeout=3)
            identity_response.raise_for_status()
            identity_data = identity_response.json()
            for key, value in endpoint.identity.items():
                if field(identity_data, key) != value:
                    return {
                        "healthy": False,
                        "reason": f"Model identity mismatch: {key}",
                        "data": data,
                    }
        return {"healthy": True, "reason": "Ready", "data": data}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        return {"healthy": False, "reason": str(exc), "data": {}}

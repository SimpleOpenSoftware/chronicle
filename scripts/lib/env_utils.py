#!/usr/bin/env python3
"""
Common utilities for reading and processing environment variables
"""

import os
from pathlib import Path
from typing import Dict, Set

from dotenv import dotenv_values


def get_config_env_variables() -> Set[str]:
    """Get list of variable names defined in config.env"""
    config_env_path = Path(__file__).parent.parent.parent / "config.env"

    if not config_env_path.exists():
        return set()

    return set(dotenv_values(str(config_env_path)).keys())


def get_resolved_env_vars() -> Dict[str, str]:
    """Get all config.env variables with their resolved values from environment"""
    config_variables = get_config_env_variables()
    resolved_vars = {}

    for var_name in config_variables:
        var_value = os.environ.get(var_name)
        if var_value:  # Only include if variable has a value
            resolved_vars[var_name] = var_value

    return resolved_vars


def classify_secrets(
    variables: Dict[str, str],
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Classify variables into secrets and regular config"""
    secrets = {}
    config = {}

    secret_patterns = ["SECRET", "KEY", "TOKEN", "PASSWORD"]

    for var_name, var_value in variables.items():
        if any(pattern in var_name.upper() for pattern in secret_patterns):
            secrets[var_name] = var_value
        else:
            config[var_name] = var_value

    return config, secrets


def get_skaffold_variables() -> Set[str]:
    """Get only the variables needed by Skaffold templates"""
    return {
        "APPLICATION_NAMESPACE",
        "INFRASTRUCTURE_NAMESPACE",
        "BACKEND_HOST",
        "WEBUI_HOST",
        "SPEAKER_HOST",
        "EXTERNAL_DOMAIN",
        "BACKEND_NODEPORT",
        "WEBUI_NODEPORT",
        "COMPUTE_MODE",
        "VITE_ALLOWED_HOSTS",
        "SPEAKER_NODE",
        "APP_NODE",
        "TAILSCALE_HOSTNAME",
        "TAILSCALE_PROXY_GROUP",
    }


# Skaffold variables that may be empty: an empty value turns the feature off
# (APP_NODE: no node pin; TAILSCALE_HOSTNAME: no Tailscale ingress).
OPTIONAL_SKAFFOLD_VARIABLES = {
    "APP_NODE",
    "TAILSCALE_HOSTNAME",
    "TAILSCALE_PROXY_GROUP",
}


def format_variable(var_name: str, var_value: str, service_name: str = None) -> str:
    """Format a variable based on service-specific rules."""
    if service_name == "skaffold" and var_name == "VITE_ALLOWED_HOSTS":
        return f'{var_name}="{var_value}"'
    return f"{var_name}={var_value}"

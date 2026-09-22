"""Plugin service for accessing the global plugin router.

This module provides singleton access to the plugin router, allowing
worker jobs to trigger plugins without accessing FastAPI app state directly.
"""

import asyncio
import hashlib
import importlib
import inspect
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml
from dotenv import dotenv_values
from dotenv import set_key as dotenv_set_key

import backend.services.privacy as privacy
from backend.config_loader import get_plugins_yml_path
from backend.models.memory_space import DeferredSpaceEvent
from backend.plugins import BasePlugin, PluginConnectivityError, PluginRouter
from backend.plugins.events import PluginEvent
from backend.plugins.router import PluginHealth
from backend.plugins.services import PluginServices
from backend.prompt_registry import get_prompt_registry
from backend.redis_factory import create_sync_redis
from backend.services.observability import record_event_sync

logger = logging.getLogger(__name__)

# Global plugin router instance
_plugin_router: Optional[PluginRouter] = None

# Redis key for signaling worker restart (consumed by orchestrator's HealthMonitor)
WORKER_RESTART_KEY = "chronicle:worker_restart_requested"


def _get_plugins_dir() -> Path:
    """Get external plugins directory.

    Priority: PLUGINS_DIR env var > Docker path > local dev path.
    """
    env_dir = os.getenv("PLUGINS_DIR")
    if env_dir:
        return Path(env_dir)
    docker_path = Path("/app/plugins")
    if docker_path.is_dir():
        return docker_path
    # Local dev: plugin_service.py is at <repo>/backend/src/backend/services/
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "plugins"


def load_plugin_env(plugin_id: str) -> Dict[str, str]:
    """Load per-plugin .env file from plugins/{id}/.env.

    Args:
        plugin_id: Plugin identifier (directory name)

    Returns:
        Dict of env var names to values. Empty dict if file doesn't exist.
    """
    plugins_dir = _get_plugins_dir()
    env_path = plugins_dir / plugin_id / ".env"

    if not env_path.exists():
        return {}

    try:
        values = dotenv_values(str(env_path))
        return {k: v for k, v in values.items() if v is not None}
    except Exception as e:
        logger.warning(f"Failed to read plugin .env for '{plugin_id}': {e}")
        return {}


def save_plugin_env(plugin_id: str, env_vars: Dict[str, str]) -> Path:
    """Save environment variables to plugins/{id}/.env.

    Merges new values into existing per-plugin .env file.
    Creates the file if it doesn't exist.

    Args:
        plugin_id: Plugin identifier (directory name)
        env_vars: Dict of env var names to values to write

    Returns:
        Path to the written .env file
    """
    plugins_dir = _get_plugins_dir()
    plugin_dir = plugins_dir / plugin_id
    env_path = plugin_dir / ".env"

    # Ensure plugin directory and .env file exist
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.touch()

    # Load existing values and merge
    existing = load_plugin_env(plugin_id)
    existing.update(env_vars)

    # Write all values using python-dotenv (preserves comments in existing files)
    for key, value in existing.items():
        dotenv_set_key(str(env_path), key, value, quote_mode="never")

    logger.info(f"Saved {len(env_vars)} env var(s) to {env_path}")
    return env_path


def expand_env_vars(value: Any, extra_env: Optional[Dict[str, str]] = None) -> Any:
    """
    Recursively expand environment variables in configuration values.

    Supports ${ENV_VAR} syntax. Checks extra_env first (if provided),
    then falls back to os.environ. If neither has the variable,
    the original placeholder is kept.

    Args:
        value: Configuration value (can be str, dict, list, or other)
        extra_env: Optional dict of additional env vars to check before os.environ

    Returns:
        Value with environment variables expanded

    Examples:
        >>> os.environ['MY_TOKEN'] = 'secret123'
        >>> expand_env_vars('token: ${MY_TOKEN}')
        'token: secret123'
        >>> expand_env_vars({'token': '${MY_TOKEN}'})
        {'token': 'secret123'}
    """
    if isinstance(value, str):
        # Pattern: ${ENV_VAR} or ${ENV_VAR:-default}
        def replacer(match):
            var_expr = match.group(1)
            # Support default values: ${VAR:-default}
            if ":-" in var_expr:
                var_name, default = var_expr.split(":-", 1)
                var_name = var_name.strip()
                if extra_env and var_name in extra_env:
                    return extra_env[var_name]
                return os.environ.get(var_name, default.strip())
            else:
                var_name = var_expr.strip()
                if extra_env and var_name in extra_env:
                    return extra_env[var_name]
                env_value = os.environ.get(var_name)
                if env_value is None:
                    logger.warning(
                        f"Environment variable '{var_name}' not found, "
                        f"keeping placeholder: ${{{var_name}}}"
                    )
                    return match.group(0)  # Keep original placeholder
                return env_value

        return re.sub(r"\$\{([^}]+)\}", replacer, value)

    elif isinstance(value, dict):
        return {k: expand_env_vars(v, extra_env=extra_env) for k, v in value.items()}

    elif isinstance(value, list):
        return [expand_env_vars(item, extra_env=extra_env) for item in value]

    else:
        return value


def load_plugin_config(
    plugin_id: str, orchestration_config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Load complete plugin configuration from multiple sources.

    Configuration is loaded and merged in this order:
    1. Plugin-specific config.yml (non-secret settings)
    2. Expand environment variables from .env (secrets)
    3. Merge orchestration settings from config/plugins.yml
       (enabled, events, condition, priority, modes)

    Args:
        plugin_id: Plugin identifier (e.g., 'email_summarizer')
        orchestration_config: Orchestration settings from config/plugins.yml

    Returns:
        Complete merged plugin configuration

    Example:
        >>> load_plugin_config('email_summarizer', {'enabled': True, 'events': [...]})
        {
            'enabled': True,
            'events': ['conversation.complete'],
            'condition': {'type': 'always'},
            'subject_prefix': 'Conversation Summary',
            'smtp_host': 'smtp.gmail.com',  # Expanded from ${SMTP_HOST}
            ...
        }
    """
    config = {}

    # 1. Load plugin-specific config.yml if it exists
    try:
        plugins_dir = _get_plugins_dir()
        plugin_config_path = plugins_dir / plugin_id / "config.yml"

        if plugin_config_path.exists():
            logger.debug(f"Loading plugin config from: {plugin_config_path}")
            with open(plugin_config_path, "r") as f:
                plugin_config = yaml.safe_load(f) or {}
                config.update(plugin_config)
                logger.debug(
                    f"Loaded {len(plugin_config)} config keys for '{plugin_id}'"
                )
        else:
            logger.debug(
                f"No config.yml found for plugin '{plugin_id}' at {plugin_config_path}"
            )

    except Exception as e:
        logger.warning(f"Failed to load config.yml for plugin '{plugin_id}': {e}")

    # 2. Expand environment variables (per-plugin .env first, then os.environ)
    plugin_env = load_plugin_env(plugin_id)
    config = expand_env_vars(config, extra_env=plugin_env)

    # 3. Merge orchestration settings from config/plugins.yml
    config["enabled"] = orchestration_config.get("enabled", False)
    config["events"] = orchestration_config.get("events", [])
    config["condition"] = orchestration_config.get("condition", {"type": "always"})
    config["priority"] = orchestration_config.get("priority", 100)
    config["modes"] = orchestration_config.get("modes", [])

    # config/plugins.yml is orchestration-only; plugin settings live in the
    # plugin's own config.yml. Warn about extra keys instead of silently
    # dropping them (e.g. an `actions:` block that never takes effect).
    ignored_keys = set(orchestration_config) - {
        "enabled",
        "events",
        "condition",
        "priority",
        "modes",
    }
    if ignored_keys:
        logger.warning(
            f"Plugin '{plugin_id}': ignoring non-orchestration key(s) "
            f"{sorted(ignored_keys)} in config/plugins.yml — plugin settings "
            f"belong in plugins/{plugin_id}/config.yml"
        )

    # Add plugin ID for reference
    config["plugin_id"] = plugin_id

    logger.debug(
        f"Plugin '{plugin_id}' config merged: enabled={config['enabled']}, "
        f"events={config['events']}, keys={list(config.keys())}"
    )

    return config


def get_plugin_router() -> Optional[PluginRouter]:
    """Get the global plugin router instance.

    Returns:
        Plugin router instance if initialized, None otherwise
    """
    global _plugin_router
    return _plugin_router


def set_plugin_router(router: PluginRouter) -> None:
    """Set the global plugin router instance.

    This should be called during app initialization in app_factory.py.

    Args:
        router: Initialized plugin router instance
    """
    global _plugin_router
    _plugin_router = router
    logger.info("Plugin router registered with plugin service")


def extract_env_var_name(value: str) -> Optional[str]:
    """Extract environment variable name from ${ENV_VAR} or ${ENV_VAR:-default} syntax.

    Args:
        value: String potentially containing ${ENV_VAR} reference

    Returns:
        Environment variable name if found, None otherwise

    Examples:
        >>> extract_env_var_name('${SMTP_HOST}')
        'SMTP_HOST'
        >>> extract_env_var_name('${SMTP_PORT:-587}')
        'SMTP_PORT'
        >>> extract_env_var_name('plain text')
        None
    """
    if not isinstance(value, str):
        return None

    match = re.search(r"\$\{([^}:]+)", value)
    if match:
        return match.group(1).strip()
    return None


def infer_field_type(key: str, value: Any) -> Dict[str, Any]:
    """Infer field schema from config key and value.

    Args:
        key: Configuration field key (e.g., 'smtp_password')
        value: Configuration field value

    Returns:
        Field schema dictionary with type, label, default, etc.

    Examples:
        >>> infer_field_type('smtp_password', '${SMTP_PASSWORD}')
        {'type': 'password', 'label': 'SMTP Password', 'secret': True, 'env_var': 'SMTP_PASSWORD', 'required': True}

        >>> infer_field_type('max_sentences', 3)
        {'type': 'number', 'label': 'Max Sentences', 'default': 3}
    """
    # Generate human-readable label from key
    label = key.replace("_", " ").title()

    # Check for environment variable reference
    if isinstance(value, str) and "${" in value:
        env_var = extract_env_var_name(value)
        if not env_var:
            return {"type": "string", "label": label, "default": value}

        # Determine if this is a secret based on env var name
        secret_keywords = ["PASSWORD", "TOKEN", "KEY", "SECRET", "APIKEY", "API_KEY"]
        is_secret = any(keyword in env_var.upper() for keyword in secret_keywords)

        # Extract default value if present (${VAR:-default})
        default_value = None
        if ":-" in value:
            default_match = re.search(r":-([^}]+)", value)
            if default_match:
                default_value = default_match.group(1).strip()
                # Try to parse boolean/number defaults
                if default_value.lower() in ("true", "false"):
                    default_value = default_value.lower() == "true"
                elif default_value.isdigit():
                    default_value = int(default_value)

        schema = {
            "type": "password" if is_secret else "string",
            "label": label,
            "secret": is_secret,
            "env_var": env_var,
            "required": is_secret,  # Secrets are required
        }

        if default_value is not None:
            schema["default"] = default_value
            schema["required"] = False

        return schema

    # Boolean values
    elif isinstance(value, bool):
        return {"type": "boolean", "label": label, "default": value}

    # Numeric values
    elif isinstance(value, int):
        return {"type": "number", "label": label, "default": value}

    elif isinstance(value, float):
        return {"type": "number", "label": label, "default": value, "step": 0.1}

    # List values
    elif isinstance(value, list):
        return {"type": "array", "label": label, "default": value}

    # Object/dict values
    elif isinstance(value, dict):
        return {"type": "object", "label": label, "default": value}

    # String values (fallback)
    else:
        return {
            "type": "string",
            "label": label,
            "default": str(value) if value is not None else "",
        }


def load_schema_yml(plugin_id: str) -> Optional[Dict[str, Any]]:
    """Load optional schema.yml override for a plugin.

    Args:
        plugin_id: Plugin identifier

    Returns:
        Schema dictionary if schema.yml exists, None otherwise
    """
    try:
        plugins_dir = _get_plugins_dir()
        schema_path = plugins_dir / plugin_id / "schema.yml"

        if schema_path.exists():
            logger.debug(f"Loading schema override from: {schema_path}")
            with open(schema_path, "r") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load schema.yml for plugin '{plugin_id}': {e}")

    return None


def infer_schema_from_config(
    plugin_id: str, config_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Infer configuration schema from plugin config.yml.

    This function analyzes the config.yml file to generate a JSON schema
    for rendering forms in the frontend. It can be overridden by providing
    a schema.yml file in the plugin directory.

    Args:
        plugin_id: Plugin identifier
        config_dict: Configuration dictionary from config.yml

    Returns:
        Schema dictionary with 'settings' and 'env_vars' sections

    Example:
        >>> config = {'subject_prefix': 'Summary', 'smtp_password': '${SMTP_PASSWORD}'}
        >>> schema = infer_schema_from_config('email_summarizer', config)
        >>> schema['settings']['subject_prefix']['type']
        'string'
        >>> schema['env_vars']['SMTP_PASSWORD']['type']
        'password'
    """
    # Check for explicit schema.yml override
    explicit_schema = load_schema_yml(plugin_id)
    if explicit_schema:
        logger.info(f"Using explicit schema.yml for plugin '{plugin_id}'")
        return explicit_schema

    # Infer schema from config values
    settings_schema = {}
    env_vars_schema = {}

    for key, value in config_dict.items():
        field_schema = infer_field_type(key, value)

        # Separate env vars from regular settings
        if field_schema.get("env_var"):
            env_var_name = field_schema["env_var"]
            env_vars_schema[env_var_name] = field_schema
        else:
            settings_schema[key] = field_schema

    return {"settings": settings_schema, "env_vars": env_vars_schema}


def mask_secrets_in_config(
    config: Dict[str, Any],
    schema: Dict[str, Any],
    plugin_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Mask secret values in configuration for frontend display.

    Args:
        config: Configuration dictionary with actual values
        schema: Schema dictionary identifying secret fields
        plugin_env: Optional per-plugin env vars (checked before os.environ)

    Returns:
        Configuration with secrets masked as '••••••••••••'

    Example:
        >>> config = {'smtp_password': 'actual_password'}
        >>> schema = {'env_vars': {'SMTP_PASSWORD': {'secret': True}}}
        >>> masked = mask_secrets_in_config(config, schema)
        >>> masked['smtp_password']
        '••••••••••••'
    """
    masked_config = config.copy()

    # Get list of secret environment variable names
    secret_env_vars = set()
    for env_var, field_schema in schema.get("env_vars", {}).items():
        if field_schema.get("secret", False):
            secret_env_vars.add(env_var)

    # Mask values that reference secret environment variables
    for key, value in masked_config.items():
        if isinstance(value, str):
            env_var = extract_env_var_name(value)
            if env_var and env_var in secret_env_vars:
                # Check if env var is set in per-plugin .env or os.environ
                is_set = bool(
                    (plugin_env and plugin_env.get(env_var)) or os.environ.get(env_var)
                )
                masked_config[key] = "••••••••••••" if is_set else ""

    return masked_config


def get_plugin_metadata(
    plugin_id: str, plugin_class: Type[BasePlugin], orchestration_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Get complete metadata for a plugin including schema and current config.

    Args:
        plugin_id: Plugin identifier
        plugin_class: Plugin class type
        orchestration_config: Orchestration config from plugins.yml

    Returns:
        Complete plugin metadata for frontend
    """
    # Load plugin config.yml
    try:
        plugins_dir = _get_plugins_dir()
        plugin_config_path = plugins_dir / plugin_id / "config.yml"

        config_dict = {}
        if plugin_config_path.exists():
            with open(plugin_config_path, "r") as f:
                config_dict = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load config for plugin '{plugin_id}': {e}")
        config_dict = {}

    # Infer schema
    config_schema = infer_schema_from_config(plugin_id, config_dict)

    # Get plugin metadata from class
    plugin_name = getattr(plugin_class, "name", plugin_id.replace("_", " ").title())
    plugin_description = getattr(plugin_class, "description", "")
    supports_testing = hasattr(plugin_class, "test_connection")

    # Load per-plugin env vars
    plugin_env = load_plugin_env(plugin_id)

    # Mask secrets in current config
    current_config = load_plugin_config(plugin_id, orchestration_config)
    masked_config = mask_secrets_in_config(
        current_config, config_schema, plugin_env=plugin_env
    )

    # Mark which env vars are set (check per-plugin .env first, then os.environ)
    for env_var_name, env_var_schema in config_schema.get("env_vars", {}).items():
        resolved = plugin_env.get(env_var_name) or os.environ.get(env_var_name)
        env_var_schema["is_set"] = bool(resolved)
        if env_var_schema.get("secret") and env_var_schema["is_set"]:
            env_var_schema["value"] = "••••••••••••"
        else:
            env_var_schema["value"] = resolved or ""

    # Determine runtime health status from the live router
    # Map internal statuses to frontend-expected values:
    #   initialized → active, failed → error, registered → disabled
    _STATUS_MAP = {"initialized": "active", "failed": "error", "registered": "disabled"}
    health_status = "unknown"
    health_error = None
    router = get_plugin_router()
    if router and plugin_id in router.plugin_health:
        h = router.plugin_health[plugin_id]
        health_status = _STATUS_MAP.get(h.status, h.status)
        health_error = h.error
    elif not orchestration_config.get("enabled", False):
        health_status = "disabled"

    result = {
        "plugin_id": plugin_id,
        "name": plugin_name,
        "description": plugin_description,
        "enabled": orchestration_config.get("enabled", False),
        "status": health_status,
        "supports_testing": supports_testing,
        "config_schema": config_schema,
        "current_config": masked_config,
        "orchestration": {
            "enabled": orchestration_config.get("enabled", False),
            "events": orchestration_config.get("events", []),
            "condition": orchestration_config.get("condition", {"type": "always"}),
            "priority": orchestration_config.get("priority", 100),
            "modes": orchestration_config.get("modes", []),
        },
    }
    if health_error:
        result["error"] = health_error
    return result


def discover_plugins() -> Dict[str, Type[BasePlugin]]:
    """
    Discover plugins in the plugins directory.

    Scans the plugins directory for subdirectories containing plugin.py files.
    Each plugin must:
    1. Have a plugin.py file with a class inheriting from BasePlugin
    2. Export exactly one BasePlugin subclass in __init__.py

    Discovery works by scanning module exports for BasePlugin subclasses,
    so no naming convention between directory name and class name is required.

    Returns:
        Dictionary mapping plugin_id (directory name) to plugin class

    Example:
        plugins/
        ├── homeassistant/
        │   ├── __init__.py  (exports HomeAssistantPlugin)
        │   └── plugin.py    (defines HomeAssistantPlugin)

        Returns: {'homeassistant': HomeAssistantPlugin}
    """
    discovered_plugins = {}

    plugins_dir = _get_plugins_dir()
    if not plugins_dir.is_dir():
        logger.warning(f"Plugins directory not found: {plugins_dir}")
        return discovered_plugins

    # Add plugins dir to sys.path so plugin packages can be imported directly
    plugins_dir_str = str(plugins_dir)
    if plugins_dir_str not in sys.path:
        sys.path.insert(0, plugins_dir_str)

    logger.info(f"Scanning for plugins in: {plugins_dir}")

    # Scan for plugin directories in deterministic order (skip hidden/underscore dirs)
    for item in sorted(plugins_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(("_", ".")):
            continue

        plugin_id = item.name
        plugin_file = item / "plugin.py"

        if not plugin_file.exists():
            logger.debug(f"Skipping '{plugin_id}': no plugin.py found")
            continue

        try:
            # Import the plugin package directly (it's on sys.path now)
            logger.debug(f"Attempting to import plugin: {plugin_id}")
            plugin_module = importlib.import_module(plugin_id)

            # Scan module exports for BasePlugin subclasses, deduplicate by id()
            seen_ids = set()
            plugin_classes = []
            for attr_name in dir(plugin_module):
                attr = getattr(plugin_module, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, BasePlugin)
                    and attr is not BasePlugin
                    and id(attr) not in seen_ids
                ):
                    seen_ids.add(id(attr))
                    plugin_classes.append(attr)

            if len(plugin_classes) == 0:
                logger.warning(
                    f"Plugin '{plugin_id}': no BasePlugin subclass found in __init__.py. "
                    f"Make sure to export your plugin class: from .plugin import YourPlugin"
                )
                continue

            if len(plugin_classes) > 1:
                class_names = [cls.__name__ for cls in plugin_classes]
                logger.warning(
                    f"Plugin '{plugin_id}': found multiple BasePlugin subclasses "
                    f"{class_names}, expected exactly 1. Using first: {class_names[0]}"
                )

            plugin_class = plugin_classes[0]
            discovered_plugins[plugin_id] = plugin_class
            logger.info(f"Discovered plugin: '{plugin_id}' ({plugin_class.__name__})")

        except ImportError as e:
            logger.warning(f"Failed to import plugin '{plugin_id}': {e}")
        except Exception as e:
            logger.error(f"Error discovering plugin '{plugin_id}': {e}", exc_info=True)

    logger.info(f"Plugin discovery complete: {len(discovered_plugins)} plugin(s) found")
    return discovered_plugins


def _build_plugin_router() -> Optional[PluginRouter]:
    """Build a new plugin router from configuration without touching the global.

    This is the internal builder used by both init_plugin_router() (first startup)
    and reload_plugins() (hot-reload). It never reads or writes _plugin_router.

    Returns:
        Fully-built plugin router with plugins registered (but not yet async-initialized),
        or None if construction fails
    """
    try:
        router = PluginRouter()

        # Load plugin configuration
        plugins_yml = get_plugins_yml_path()
        logger.info(f"Looking for plugins config at: {plugins_yml}")
        logger.info(f"File exists: {plugins_yml.exists()}")

        if plugins_yml.exists():
            with open(plugins_yml, "r") as f:
                plugins_config = yaml.safe_load(f)
                # Expand environment variables in configuration
                plugins_config = expand_env_vars(plugins_config)
                plugins_data = plugins_config.get("plugins", {})

            logger.info(
                f"Loaded plugins config with {len(plugins_data)} plugin(s): {list(plugins_data.keys())}"
            )

            # Discover all plugins via auto-discovery
            discovered_plugins = discover_plugins()

            # Initialize each plugin listed in config/plugins.yml
            for plugin_id, orchestration_config in plugins_data.items():
                logger.info(
                    f"Processing plugin '{plugin_id}', enabled={orchestration_config.get('enabled', False)}"
                )
                if not orchestration_config.get("enabled", False):
                    continue

                try:
                    # Check if plugin was discovered
                    if plugin_id not in discovered_plugins:
                        logger.warning(
                            f"Plugin '{plugin_id}' not found. "
                            f"Make sure the plugin directory exists in plugins/ with proper structure."
                        )
                        continue

                    # Load complete plugin configuration (merges plugin config.yml + .env + orchestration)
                    plugin_config = load_plugin_config(plugin_id, orchestration_config)

                    # Get plugin class from discovered plugins
                    plugin_class = discovered_plugins[plugin_id]

                    # Instantiate and register the plugin
                    plugin = plugin_class(plugin_config)

                    # Let plugin register its prompts with the prompt registry
                    try:
                        plugin.register_prompts(get_prompt_registry())
                    except Exception as e:
                        logger.debug(
                            f"Plugin '{plugin_id}' prompt registration skipped: {e}"
                        )

                    # Note: async initialization happens in app_factory lifespan or reload_plugins
                    router.register_plugin(plugin_id, plugin)
                    logger.info(f"Plugin '{plugin_id}' registered successfully")

                except Exception as e:
                    logger.error(
                        f"Failed to register plugin '{plugin_id}': {e}", exc_info=True
                    )

            logger.info(
                f"Plugin registration complete: {len(router.plugins)} plugin(s) registered"
            )
        else:
            logger.info("No plugins.yml found, plugins disabled")

        # Attach PluginServices for cross-plugin and system interaction
        services = PluginServices(router=router)
        router.set_services(services)

        return router

    except Exception as e:
        logger.error(f"Failed to build plugin router: {e}", exc_info=True)
        return None


def init_plugin_router() -> Optional[PluginRouter]:
    """Initialize the plugin router from configuration.

    This is called during app startup to create and install the global plugin router.
    For hot-reload, use reload_plugins() instead.

    Returns:
        Initialized plugin router, or None if no plugins configured
    """
    global _plugin_router

    if _plugin_router is not None:
        logger.warning("Plugin router already initialized")
        return _plugin_router

    router = _build_plugin_router()
    if router:
        _plugin_router = router
        logger.info("Plugin router installed as global singleton")
    return _plugin_router


async def initialize_plugins(router: PluginRouter) -> Dict[str, List]:
    """Initialize all enabled plugins on a router, recording health per plugin.

    Shared by every process that hosts a plugin router (FastAPI app, streaming
    worker, wakeword worker, RQ workers, hot-reload). Failure handling:

    - PluginConnectivityError -> DEGRADED: the plugin's external dependency is
      unreachable (e.g. Home Assistant on a powered-off server). Logged as a
      single-line warning, and run_plugin_recovery() retries it with backoff.
    - Any other exception -> FAILED: real config/setup error, logged with
      traceback.

    Returns:
        Summary dict: {"initialized": [ids], "degraded": [ids],
        "failed": [{"plugin_id", "error"}]}
    """
    summary: Dict[str, List] = {"initialized": [], "degraded": [], "failed": []}
    for plugin_id, plugin in router.plugins.items():
        if not plugin.enabled:
            continue
        try:
            await plugin.initialize()
            router.mark_plugin_initialized(plugin_id)
            summary["initialized"].append(plugin_id)
            logger.info(f"Plugin '{plugin_id}' initialized")
        except PluginConnectivityError as e:
            router.mark_plugin_degraded(plugin_id, str(e))
            summary["degraded"].append(plugin_id)
            logger.warning(
                f"Plugin '{plugin_id}' degraded — dependency unreachable, "
                f"will retry in background: {e}"
            )
        except Exception as e:
            router.mark_plugin_failed(plugin_id, str(e))
            summary["failed"].append({"plugin_id": plugin_id, "error": str(e)})
            logger.error(
                f"Failed to initialize plugin '{plugin_id}': {e}", exc_info=True
            )
    return summary


async def run_plugin_recovery(
    router: PluginRouter,
    *,
    initial_delay: float = 30.0,
    max_delay: float = 600.0,
    tick: float = 15.0,
    health_interval: float = 300.0,
) -> None:
    """Background recovery loop for plugins with unhealthy external dependencies.

    Run as a long-lived asyncio task in each process that hosts a plugin router.
    Two jobs:

    1. Re-run initialize() for DEGRADED/FAILED plugins with per-plugin
       exponential backoff (initial_delay doubling up to max_delay). On success
       the plugin flips to INITIALIZED and a "recovered" system event is
       recorded, so health state reflects reality without a process restart.
    2. Every health_interval, run health_check() on INITIALIZED plugins; a
       not-ok result demotes the plugin to DEGRADED, which feeds it back into
       (1). This catches dependencies that go down mid-day, not just at boot.

    Cancellation-safe; an unexpected error in one tick never kills the loop.
    """
    delays: Dict[str, float] = {}  # plugin_id -> current backoff delay
    next_due: Dict[str, float] = {}  # plugin_id -> monotonic time of next retry
    last_health_probe = time.monotonic()

    while True:
        try:
            await asyncio.sleep(tick)
            now = time.monotonic()

            # --- 1. Retry init for degraded/failed plugins, with backoff ---
            for plugin_id, plugin in router.plugins.items():
                health = router.plugin_health.get(plugin_id)
                if health is None or not plugin.enabled:
                    continue
                if health.status not in (PluginHealth.DEGRADED, PluginHealth.FAILED):
                    delays.pop(plugin_id, None)
                    next_due.pop(plugin_id, None)
                    continue

                if plugin_id not in next_due:
                    # First time we see this plugin unhealthy: schedule, don't retry yet
                    delays[plugin_id] = initial_delay
                    next_due[plugin_id] = now + initial_delay
                    continue
                if now < next_due[plugin_id]:
                    continue

                was_failed = health.status == PluginHealth.FAILED
                try:
                    await plugin.initialize()
                    router.mark_plugin_initialized(plugin_id)
                    delays.pop(plugin_id, None)
                    next_due.pop(plugin_id, None)
                    logger.info(f"Plugin '{plugin_id}' recovered")
                    record_event_sync(
                        severity="info",
                        category="plugin",
                        source=plugin_id,
                        title=f"Plugin '{plugin_id}' recovered",
                        detail="initialize() succeeded on background retry",
                        metadata={"plugin_id": plugin_id},
                        incident_key=f"plugin-dependency:{plugin_id}",
                        resolves_incident=True,
                    )
                except PluginConnectivityError as e:
                    # Still unreachable — quiet debug log, back off further
                    health.error = str(e)
                    delay = min(delays.get(plugin_id, initial_delay) * 2, max_delay)
                    delays[plugin_id] = delay
                    next_due[plugin_id] = now + delay
                    logger.debug(
                        f"Plugin '{plugin_id}' still unreachable, next retry in {delay:.0f}s: {e}"
                    )
                except Exception as e:
                    # Only record a FAILED event on the transition, not every retry
                    if was_failed:
                        health.error = str(e)
                    else:
                        router.mark_plugin_failed(plugin_id, str(e))
                    delay = min(delays.get(plugin_id, initial_delay) * 2, max_delay)
                    delays[plugin_id] = delay
                    next_due[plugin_id] = now + delay
                    logger.warning(
                        f"Plugin '{plugin_id}' retry failed, next retry in {delay:.0f}s: {e}"
                    )

            # --- 2. Periodic health probe: demote initialized plugins that went down ---
            if now - last_health_probe >= health_interval:
                last_health_probe = now
                for plugin_id, plugin in router.plugins.items():
                    health = router.plugin_health.get(plugin_id)
                    if (
                        health is None
                        or not plugin.enabled
                        or health.status != PluginHealth.INITIALIZED
                    ):
                        continue
                    try:
                        result = await asyncio.wait_for(
                            plugin.health_check(), timeout=10
                        )
                    except Exception as e:
                        result = {"ok": False, "message": str(e)}
                    if not result.get("ok", False):
                        router.mark_plugin_degraded(
                            plugin_id,
                            str(result.get("message") or "health check failed"),
                        )
                        logger.warning(
                            f"Plugin '{plugin_id}' health check failed, marked degraded: "
                            f"{result.get('message')}"
                        )

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Plugin recovery loop tick failed (loop continues)")


async def ensure_plugin_router() -> Optional[PluginRouter]:
    """Get or initialize the plugin router with all plugins initialized.

    This is the standard pattern for worker processes that need the plugin router.
    It handles the get-or-init-then-initialize sequence in one call.

    Returns:
        Initialized plugin router, or None if no plugins configured
    """
    plugin_router = get_plugin_router()
    if plugin_router:
        return plugin_router

    logger.info("Initializing plugin router in worker process...")
    plugin_router = init_plugin_router()
    if plugin_router:
        await initialize_plugins(plugin_router)
    return plugin_router


async def dispatch_plugin_event(
    event: PluginEvent,
    user_id: str,
    data: dict,
    metadata: Optional[dict] = None,
    description: str = "",
    require_router: bool = False,
) -> Optional[list]:
    """Dispatch an event to the plugin system with standard logging.

    Handles the common pattern of: ensure router -> dispatch event -> log results.

    Args:
        event: Plugin event to dispatch
        user_id: User ID for the event
        data: Event-specific data dict
        metadata: Optional metadata dict
        description: Log context (e.g., "conversation=abc123, memories=5")
        require_router: If True and no router, raise RuntimeError instead of returning None

    Returns:
        List of plugin results, or None if no router available

    Raises:
        RuntimeError: If require_router=True and no plugin router is available
    """

    privacy_snapshot = await privacy.guard_payload(
        user_id, {"data": data, "metadata": metadata}
    )
    plugin_router = await ensure_plugin_router()

    if not plugin_router:
        if require_router:
            raise RuntimeError(
                f"Plugin router could not be initialized in worker process. "
                f"{event.value} event will NOT be dispatched!"
            )
        return None

    await privacy.assert_current(user_id, privacy_snapshot)
    logger.info(f"🔌 DISPATCH: {event.value} event ({description})")

    plugin_results = await plugin_router.dispatch_event(
        event=event,
        user_id=user_id,
        data=data,
        metadata=metadata or {},
    )
    await privacy.assert_current(user_id, privacy_snapshot)

    result_count = len(plugin_results) if plugin_results else 0
    logger.info(f"🔌 RESULT: {event.value} dispatched to {result_count} plugins")

    if plugin_results:
        for result in plugin_results:
            if result.message:
                logger.info(f"  Plugin result: {result.message}")

    return plugin_results


_SPACE_TERMINAL_EVENT_ORDER = {
    PluginEvent.TRANSCRIPT_BATCH: 10,
    PluginEvent.CONVERSATION_COMPLETE: 20,
    PluginEvent.MEMORY_PROCESSED: 30,
}


async def dispatch_or_defer_space_event(
    *,
    event: PluginEvent,
    user_id: str,
    data: dict,
    memory_space_id: Optional[str],
    source_kind: str,
    source_id: str,
    metadata: Optional[dict] = None,
    description: str = "",
    require_router: bool = False,
) -> Optional[list]:
    """Dispatch a Main event or durably defer an isolated-space terminal event.

    Streaming events are deliberately suppressed inside spaces. Terminal events are
    stored exactly once and released only when their source is accepted by a merge.
    """
    if not memory_space_id:
        return await dispatch_plugin_event(
            event=event,
            user_id=user_id,
            data=data,
            metadata=metadata,
            description=description,
            require_router=require_router,
        )
    if event == PluginEvent.TRANSCRIPT_STREAMING:
        logger.debug(
            "Suppressing %s for isolated memory space %s", event.value, memory_space_id
        )
        return None
    causal_order = _SPACE_TERMINAL_EVENT_ORDER.get(event)
    if causal_order is None:
        logger.debug(
            "Suppressing non-terminal %s for isolated memory space %s",
            event.value,
            memory_space_id,
        )
        return None

    stable_material = ":".join(
        (str(user_id), memory_space_id, source_kind, source_id, event.value)
    )
    idempotency_key = (
        "space-event:" + hashlib.sha256(stable_material.encode("utf-8")).hexdigest()
    )
    existing = await DeferredSpaceEvent.find_one(
        DeferredSpaceEvent.idempotency_key == idempotency_key
    )
    if existing is None:
        event_doc = DeferredSpaceEvent(
            user_id=str(user_id),
            space_id=memory_space_id,
            source_kind=source_kind,
            source_id=source_id,
            event_type=event.value,
            idempotency_key=idempotency_key,
            causal_order=causal_order,
            data=data,
            metadata={**(metadata or {}), "idempotency_key": idempotency_key},
            description=description,
        )
        try:
            await event_doc.insert()
        except Exception:
            # Concurrent terminal jobs may race. The unique idempotency index is the
            # authority; re-read before deciding this was a persistence failure.
            existing = await DeferredSpaceEvent.find_one(
                DeferredSpaceEvent.idempotency_key == idempotency_key
            )
            if existing is None:
                raise
    logger.info(
        "Deferred %s for isolated memory space %s source=%s",
        event.value,
        memory_space_id,
        source_id,
    )
    return None


async def cleanup_plugin_router() -> None:
    """Clean up the plugin router and all registered plugins."""
    global _plugin_router

    if _plugin_router:
        try:
            if _plugin_router._services:
                await _plugin_router._services.cleanup()
            await _plugin_router.cleanup_all()
            logger.info("Plugin router cleanup complete")
        except Exception as e:
            logger.error(f"Error during plugin router cleanup: {e}")
        finally:
            _plugin_router = None


async def reload_plugins(app=None) -> Dict[str, Any]:
    """Hot-reload all plugins by building a new router and atomically swapping it in.

    The old router continues serving requests while the new one is being built.
    The global _plugin_router is only replaced once the new router is fully
    initialized, so concurrent callers of get_plugin_router() never see None.

    Steps:
    1. Purge sys.modules entries for plugin packages (so importlib re-reads from disk)
    2. Build and initialize a new router (old router still active)
    3. Atomic swap: replace global _plugin_router with the new router
    4. Clean up old plugin instances (close SMTP, HA sessions, etc.)
    5. Update app.state if app provided

    Args:
        app: Optional FastAPI app instance to update app.state.plugin_router

    Returns:
        Result dict with reload status, counts, and timing
    """
    global _plugin_router
    start = time.monotonic()

    old_router = _plugin_router
    old_count = len(old_router.plugins) if old_router else 0

    # 1. Purge sys.modules for plugin packages only (before re-importing)
    plugins_dir = _get_plugins_dir()
    purged_modules = []
    if plugins_dir.is_dir():
        plugin_names = {
            item.name
            for item in plugins_dir.iterdir()
            if item.is_dir() and not item.name.startswith(("_", "."))
        }
        for mod_name in list(sys.modules.keys()):
            top_level = mod_name.split(".")[0]
            if top_level in plugin_names:
                del sys.modules[mod_name]
                purged_modules.append(mod_name)
        if purged_modules:
            logger.info(f"Purged {len(purged_modules)} cached plugin modules")

    # 2. Build a new router (old router still serves requests during this)
    new_router = _build_plugin_router()

    # 3. Initialize each plugin on the new router
    initialized = []
    failed = []
    if new_router:
        summary = await initialize_plugins(new_router)
        initialized = summary["initialized"] + summary["degraded"]
        failed = summary["failed"]

    # 4. Atomic swap — from this point, all callers see the new router
    _plugin_router = new_router

    # 5. Update app.state if provided
    if app and new_router:
        app.state.plugin_router = new_router

    # 6. Clean up old router *after* the swap (best-effort, never blocks the new router)
    if old_router:
        try:
            if old_router._services:
                await old_router._services.cleanup()
            await old_router.cleanup_all()
        except Exception as e:
            logger.warning(f"Error during old plugin router cleanup: {e}")

    elapsed = time.monotonic() - start
    new_count = len(new_router.plugins) if new_router else 0

    result = {
        "success": True,
        "previous_plugin_count": old_count,
        "new_plugin_count": new_count,
        "initialized": initialized,
        "failed": failed,
        "purged_modules": len(purged_modules),
        "elapsed_seconds": round(elapsed, 3),
    }
    logger.info(
        f"Plugin reload complete: {new_count} plugins loaded "
        f"({len(initialized)} initialized, {len(failed)} failed) in {elapsed:.3f}s"
    )
    return result


def signal_worker_restart() -> None:
    """Write a Redis key to signal the worker orchestrator to restart all workers.

    The orchestrator's HealthMonitor polls for this key and triggers a restart
    when found. The key is consumed (deleted) after the restart is initiated.

    Uses its own short-lived Redis connection so it works regardless of the
    plugin router's lifecycle (e.g. during or after a failed reload).
    """
    try:
        client = create_sync_redis(decode_responses=True)
        try:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            client.set(WORKER_RESTART_KEY, timestamp)
            logger.info(
                f"Worker restart signal sent via Redis key '{WORKER_RESTART_KEY}'"
            )
        finally:
            client.close()
    except Exception as e:
        logger.error(f"Failed to send worker restart signal: {e}")

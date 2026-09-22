"""
OmegaConf-based configuration management for Chronicle.

Provides unified config loading with environment variable interpolation.
"""

import logging
import os
import threading
import warnings
from pathlib import Path
from typing import Optional

from omegaconf import DictConfig, OmegaConf

logger = logging.getLogger(__name__)

# Global config cache
_config_cache: Optional[DictConfig] = None
_config_warnings: tuple[str, ...] = ()
_config_lock = threading.RLock()

# Runtime overrides registered by save_config_section(), keyed by section path.
# Re-applied on every load_config() so they survive cache reloads even when
# CONFIG_FILE points away from config.yml (test environments).
_runtime_overrides: dict = {}


def get_config_dir() -> Path:
    """Get config directory path (single source of truth)."""
    config_dir = os.getenv("CONFIG_DIR", "/app/config")
    return Path(config_dir)


def get_plugins_yml_path() -> Path:
    """
    Get path to plugins.yml file (single source of truth).

    Returns:
        Path to plugins.yml
    """
    return get_config_dir() / "plugins.yml"


def load_config(force_reload: bool = False) -> DictConfig:
    """
    Load and merge configuration using OmegaConf.

    Merge priority (later overrides earlier):
    1. config/defaults.yml (shipped defaults)
    2. config/config.yml (user overrides)
    3. Environment variables (via ${oc.env:VAR,default} syntax)

    Args:
        force_reload: If True, reload from disk even if cached

    Returns:
        Merged DictConfig with all settings
    """
    global _config_warnings
    if _config_cache is not None and not force_reload:
        return _config_cache
    with _config_lock:
        if _config_cache is not None and not force_reload:
            return _config_cache
        # Keep diagnostics attached to the actual load, rather than doing a new
        # YAML parse on every health poll just to rediscover its warnings.
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            config = _load_config_uncached()
        _config_warnings = tuple(str(item.message) for item in captured)
        for item in captured:
            warnings.warn_explicit(
                item.message, item.category, item.filename, item.lineno
            )
        return config


def get_config_load_warnings() -> tuple[str, ...]:
    """Warnings from the active configuration; loading occurs only when uncached."""
    with _config_lock:
        load_config()
        return _config_warnings


def _load_config_uncached() -> DictConfig:
    global _config_cache
    config_dir = get_config_dir()
    defaults_path = config_dir / "defaults.yml"

    # Support CONFIG_FILE env var for test configurations
    config_file = os.getenv("CONFIG_FILE", "config.yml")
    # Handle both absolute paths and relative filenames
    if os.path.isabs(config_file):
        config_path = Path(config_file)
    else:
        config_path = config_dir / config_file

    # Load defaults
    defaults = {}
    if defaults_path.exists():
        try:
            defaults = OmegaConf.load(defaults_path)
            logger.info(f"Loaded defaults from {defaults_path}")
        except Exception as e:
            logger.warning(f"Could not load defaults from {defaults_path}: {e}")

    # Load user config
    user_config = {}
    if config_path.exists():
        try:
            user_config = OmegaConf.load(config_path)
            logger.info(f"Loaded config from {config_path}")
        except Exception as e:
            logger.error(f"Error loading config from {config_path}: {e}")

    # Merge configurations (user config overrides defaults)
    # OmegaConf.merge replaces lists entirely, so we need custom merge
    # for the 'models' list: merge by name so defaults models that aren't
    # in user config are still available.
    default_models_config = defaults.get("models") if defaults else None
    user_models_config = user_config.get("models") if user_config else None
    default_models = (
        OmegaConf.to_container(default_models_config, resolve=False)
        if default_models_config is not None
        else []
    )
    user_models = (
        OmegaConf.to_container(user_models_config, resolve=False)
        if user_models_config is not None
        else []
    )

    merged = OmegaConf.merge(defaults, user_config)

    # Name-based merge: user models override defaults, but default-only models are kept
    if default_models and user_models:
        user_model_names = {m.get("name") for m in user_models if isinstance(m, dict)}
        extra_defaults = [
            m
            for m in default_models
            if isinstance(m, dict) and m.get("name") not in user_model_names
        ]
        if extra_defaults:
            all_models = user_models + extra_defaults
            merged["models"] = OmegaConf.create(all_models)
            logger.info(
                f"Merged {len(extra_defaults)} default-only models into config: "
                f"{[m.get('name') for m in extra_defaults]}"
            )

    # Re-apply runtime overrides saved via save_config_section(). When
    # CONFIG_FILE points away from config.yml (test environments), the saved
    # values exist only in memory — without this they silently revert on the
    # next reload, so a runtime toggle only "took" until some unrelated code
    # path refreshed the config.
    for section_path, values in _runtime_overrides.items():
        OmegaConf.update(merged, section_path, values, merge=True)

    # Cache result
    _config_cache = merged

    logger.info("Configuration loaded successfully with OmegaConf")
    return merged


def reload_config() -> DictConfig:
    """Reload configuration from disk (invalidate cache)."""
    return load_config(force_reload=True)


def get_backend_config(section: Optional[str] = None) -> DictConfig:
    """
    Get backend configuration section.

    Args:
        section: Optional subsection (e.g., 'diarization', 'cleanup')

    Returns:
        DictConfig for backend section or subsection
    """
    cfg = load_config()
    if "backend" not in cfg:
        return OmegaConf.create({})

    backend_cfg = cfg.backend
    if section:
        return backend_cfg.get(section, OmegaConf.create({}))
    return backend_cfg


def get_service_config(service_name: str) -> DictConfig:
    """
    Get service configuration section.

    Args:
        service_name: Service name (e.g., 'speaker_recognition', 'asr_services')

    Returns:
        DictConfig for service section
    """
    cfg = load_config()
    return cfg.get(service_name, OmegaConf.create({}))


def save_config_section(section_path: str, values: dict) -> bool:
    """
    Update a config section and save to config.yml.

    Also updates the in-memory config cache so changes take effect immediately,
    even when CONFIG_FILE points to a different file (e.g., in test environments).

    Args:
        section_path: Dot-separated path (e.g., 'backend.diarization')
        values: Dict with new values

    Returns:
        True if saved successfully
    """
    with _config_lock:
        try:
            config_path = get_config_dir() / "config.yml"

            # Load existing config. Must be a DictConfig even when the file doesn't
            # exist yet — OmegaConf.update() raises "Unexpected type" on a plain
            # dict, which made the very first runtime settings save fail silently
            # on installs without a config.yml.
            existing_config = OmegaConf.create({})
            if config_path.exists():
                existing_config = OmegaConf.load(config_path)

            # Update section using dot notation
            OmegaConf.update(existing_config, section_path, values, merge=True)

            # Save back to file
            OmegaConf.save(existing_config, config_path)

            # Register a runtime override BEFORE reloading: when CONFIG_FILE points
            # to a different file than config.yml (test configs), the value we just
            # saved is not in the file load_config() reads, so it must be re-applied
            # on every load — a one-shot in-memory patch would silently revert on
            # the next reload_config() from any code path.
            _runtime_overrides[section_path] = values

            # Reload config from the primary config file (CONFIG_FILE env var);
            # load_config() re-applies _runtime_overrides on top.
            reload_config()

            logger.info(f"Saved config section '{section_path}' to {config_path}")
            return True

        except Exception as e:
            logger.error(f"Error saving config section '{section_path}': {e}")
            return False


def _load_raw_config() -> DictConfig:
    """Load the raw (unmerged, unresolved) config.yml.

    Unlike ``load_config()`` this does NOT merge defaults.yml or resolve
    ``${oc.env:...}`` interpolations — it returns exactly what is persisted in
    config.yml. Callers that edit model definitions or want to display the
    *reference* form of a secret (e.g. ``${oc.env:OPENAI_API_KEY}``) rather than
    the resolved value must read through here.
    """
    config_path = get_config_dir() / "config.yml"
    if config_path.exists():
        return OmegaConf.load(config_path)
    return OmegaConf.create({})


def get_raw_models() -> list:
    """Return the raw ``models`` list from config.yml as plain dicts.

    Values are NOT resolved, so ``${oc.env:...}`` references are preserved. This
    is the list to mutate-and-write-back when adding/editing/deleting a model;
    note it excludes default-only models that live solely in defaults.yml.
    """
    raw = _load_raw_config()
    models = raw.get("models", []) or []
    return OmegaConf.to_container(models, resolve=False)


def save_models_list(models: list) -> bool:
    """Persist the full ``models`` list to config.yml (replace semantics).

    OmegaConf merge replaces lists wholesale, so model add/edit/delete must pass
    the COMPLETE intended list here (read it via ``get_raw_models()`` first,
    mutate, then save). After writing, the config cache is invalidated; callers
    must also ``load_models_config(force_reload=True)`` to refresh the model
    registry (kept out of here to avoid a config_loader→model_registry import).
    """
    with _config_lock:
        try:
            config_path = get_config_dir() / "config.yml"

            existing_config = OmegaConf.create({})
            if config_path.exists():
                existing_config = OmegaConf.load(config_path)

            existing_config["models"] = OmegaConf.create(models)
            OmegaConf.save(existing_config, config_path)

            reload_config()
            logger.info(f"Saved {len(models)} models to {config_path}")
            return True

        except Exception as e:
            logger.error(f"Error saving models list: {e}")
            return False

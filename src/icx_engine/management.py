from __future__ import annotations
from icx_engine.models.config import AppConfig, BaseConnection, LLMConfig
from icx_engine.config_manager import ConfigManager


class ManagementError(Exception):
    pass


def resolve_connection(config: AppConfig, target: str) -> BaseConnection:
    connections = config.connections
    if not connections:
        raise ManagementError("No connections configured. Run `icx connection --add` first.")
    if target.isdigit():
        idx = int(target) - 1
        if idx < 0 or idx >= len(connections):
            raise ManagementError(
                f"Invalid selection. Please choose a value between 1 and {len(connections)}."
            )
        return connections[idx]
    for conn in connections:
        if conn.domain == target:
            return conn
    raise ManagementError(f"No connection matching '{target}' found.")


def resolve_llm_profile(config: AppConfig, target: str) -> tuple[str, LLMConfig]:
    names = list(config.llm_profiles.keys())
    if not names:
        raise ManagementError("No AI profiles configured. Run `icx model --add` first.")
    if target.isdigit():
        idx = int(target) - 1
        if idx < 0 or idx >= len(names):
            raise ManagementError(
                f"Invalid selection. Please choose a value between 1 and {len(names)}."
            )
        name = names[idx]
        return name, config.llm_profiles[name]
    if target in config.llm_profiles:
        return target, config.llm_profiles[target]
    raise ManagementError(f"No AI profile matching '{target}' found.")


def disconnect(config: AppConfig, target: str) -> AppConfig:
    conn = resolve_connection(config, target)
    ConfigManager.delete_connection_secrets(conn)
    new_connections = [c for c in config.connections if not (c.connector_type == conn.connector_type and c.domain == conn.domain)]
    conn_key = f"{conn.connector_type}:{conn.domain}"
    new_default = None if config.default_connection == conn_key else config.default_connection
    return config.model_copy(update={"connections": new_connections, "default_connection": new_default})


def set_default_connection(config: AppConfig, target: str) -> AppConfig:
    conn = resolve_connection(config, target)
    return config.model_copy(update={"default_connection": f"{conn.connector_type}:{conn.domain}"})


def use_ai_profile(config: AppConfig, target: str) -> AppConfig:
    name, _ = resolve_llm_profile(config, target)
    return config.model_copy(update={"current_llm_profile": name})


def unset_llm_profile(config: AppConfig, target: str) -> AppConfig:
    name, _ = resolve_llm_profile(config, target)
    ConfigManager.delete_llm_profile_secrets(name)
    new_profiles = {k: v for k, v in config.llm_profiles.items() if k != name}
    new_current = None if config.current_llm_profile == name else config.current_llm_profile
    return config.model_copy(update={"llm_profiles": new_profiles, "current_llm_profile": new_current})


def unset_llm_channel(config: AppConfig, target: str, channel: int) -> AppConfig:
    name, profile = resolve_llm_profile(config, target)
    if channel == 1:
        if profile.image_config is not None:
            raise ManagementError(
                f"Cannot remove the text channel while an image channel is configured. "
                f"Use `icx model --remove {target}` to remove the entire profile."
            )
        return unset_llm_profile(config, target)
    if channel == 2:
        if profile.image_config is None:
            raise ManagementError(f"Profile '{name}' has no image channel to remove.")
        ConfigManager.delete_llm_image_secrets(name)
        new_profile = profile.model_copy(update={"image_config": None})
        new_profiles = {**config.llm_profiles, name: new_profile}
        return config.model_copy(update={"llm_profiles": new_profiles})
    raise ManagementError(f"Invalid channel {channel}. Use 1 (text) or 2 (image).")

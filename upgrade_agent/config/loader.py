import os
from pathlib import Path

import yaml


def load_env_config(config_dir: Path, env_name: str) -> dict:
    config_path = config_dir / f"{env_name.lower()}.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return expand_env_vars(config)


def expand_env_vars(obj):
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(v) for v in obj]
    return obj

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from .models import AppConfig

logger = logging.getLogger(__name__)

def _config_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base = Path(local_appdata)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "Bamboozle"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def load_config() -> AppConfig:
    path = _config_path()
    if not path.exists():
        logger.info("No config file found at %s, using defaults", path)
        return AppConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig.model_validate(data)
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to load config: %s", e)
        backup = path.with_suffix(".json.bak")
        shutil.copy2(path, backup)
        logger.info("Backed up corrupt config to %s", backup)
        return AppConfig()


def save_config(config: AppConfig) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Config saved to %s", path)

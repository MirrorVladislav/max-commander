from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "token": "",
    "device_id": "",
    "phone": "",
    "device_type": "WEB",
    "app_version": "25.12.13",
    "poll_interval": 5.0,
    "notification_sounds": True,
    "work_dir": "cache",
    "download_dir": "downloads",
    "send_fake_telemetry": False,
    "reconnect": True,
}


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            self.save(dict(DEFAULT_CONFIG))
            return dict(DEFAULT_CONFIG)

        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config.json must contain a JSON object")

        config = dict(DEFAULT_CONFIG)
        config.update(data)
        return config

    def save(self, config: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def update_session(self, **fields: Any) -> dict[str, Any]:
        config = self.load()
        config.update(fields)
        self.save(config)
        return config

    def clear_session(self) -> dict[str, Any]:
        config = self.load()
        config["token"] = ""
        config["device_id"] = ""
        self.save(config)
        return config

    def clear_account(self) -> dict[str, Any]:
        config = self.load()
        config["token"] = ""
        config["device_id"] = ""
        config["phone"] = ""
        config["device_type"] = ""
        self.save(config)
        return config

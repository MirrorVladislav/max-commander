from pathlib import Path

from .app_config import ConfigStore
from .session import MaxSession
from .state import AppState
from .tui import build_app


CONFIG_PATH = Path(__file__).resolve().with_name("config.json")


def _resolve_config_path(base_path: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = base_path.parent / path
    return str(path)


def main() -> None:
    print("MAIN STARTED")

    config_store = ConfigStore(CONFIG_PATH)
    config = config_store.load()
    token_value = str(config.get("token", "") or "").strip()
    device_id_value = str(config.get("device_id", "") or "").strip()
    use_cache_credentials = token_value.lower() == "cache" and device_id_value.lower() == "cache"

    session = MaxSession(
        token=None if use_cache_credentials else (token_value or None),
        device_id=None if use_cache_credentials else (device_id_value or None),
        phone=str(config.get("phone", "") or "").strip(),
        device_type=str(config.get("device_type", "WEB") or "WEB").strip(),
        app_version=str(config.get("app_version", "25.12.13") or "25.12.13").strip(),
        work_dir=_resolve_config_path(CONFIG_PATH, str(config.get("work_dir", "cache"))),
        download_dir=_resolve_config_path(CONFIG_PATH, str(config.get("download_dir", "downloads"))),
        send_fake_telemetry=bool(config.get("send_fake_telemetry", False)),
        reconnect=bool(config.get("reconnect", True)),
        use_cache_credentials=use_cache_credentials,
    )
    state = AppState(
        session,
        poll_interval=float(config.get("poll_interval", 5.0)),
        notification_sounds=bool(config.get("notification_sounds", True)),
        save_session_callback=config_store.update_session,
        clear_session_callback=config_store.clear_session,
        clear_account_callback=config_store.clear_account,
    )
    app = build_app(session, state)
    app.run()


if __name__ == "__main__":
    main()

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

    session = MaxSession(
        token=str(config.get("token", "") or "").strip(),
        device_id=str(config.get("device_id", "") or "").strip() or None,
        phone=str(config.get("phone", "") or "").strip(),
        device_type=str(config.get("device_type", "WEB") or "WEB").strip(),
        app_version=str(config.get("app_version", "25.12.13") or "25.12.13").strip(),
        work_dir=_resolve_config_path(CONFIG_PATH, str(config.get("work_dir", "cache"))),
        download_dir=_resolve_config_path(CONFIG_PATH, str(config.get("download_dir", "downloads"))),
        send_fake_telemetry=bool(config.get("send_fake_telemetry", False)),
        reconnect=bool(config.get("reconnect", True)),
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

# session.py

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import aiohttp
from pymax import MaxClient
from pymax.core import SocketMaxClient
from pymax.crud import Database
from pymax.exceptions import Error as PyMaxError
from pymax.exceptions import SocketNotConnectedError, SocketSendError, WebSocketNotConnectedError
from pymax.payloads import UserAgentPayload
from pymax.payloads import FetchChatsPayload
from pymax.files import Photo, File
from pymax.static.enum import Opcode


# =========================
# DATA MODELS
# =========================

@dataclass(slots=True)
class UserInfo:
    user_id: int
    display_name: str
    first_name: str = ""
    last_name: str = ""
    username: str | None = None
    phone: str | None = None
    description: str | None = None
    avatar_url: str | None = None
    is_online: bool | None = None
    last_seen: int | None = None
    raw: Any = None


@dataclass(slots=True)
class AttachmentInfo:
    kind: str  # photo | file | video | unknown
    attach_id: str
    name: str | None = None
    ext: str | None = None
    size: int | None = None
    url: str | None = None
    token: str | None = None
    width: int | None = None
    height: int | None = None
    message_id: int | None = None
    chat_id: int | None = None
    raw: Any = None


@dataclass(slots=True)
class MessageInfo:
    message_id: int
    chat_id: int
    sender_id: int | None
    sender_name: str
    timestamp_ms: int | None
    text: str
    attachments: list[AttachmentInfo] = field(default_factory=list)
    reply_to: int | None = None
    reply_sender_name: str | None = None
    reply_preview_text: str | None = None
    is_outgoing: bool = False
    is_unread: bool = False
    raw: Any = None

    @property
    def timestamp_str(self) -> str:
        if not self.timestamp_ms:
            return "{??/??/????-??:??}"
        dt = datetime.fromtimestamp(self.timestamp_ms / 1000)
        return dt.strftime("{%d/%m/%Y-%H:%M}")


@dataclass(slots=True)
class ChatInfo:
    chat_id: int
    name: str
    type: str
    first_name: str = ""
    last_name: str = ""
    participant_ids: list[int] = field(default_factory=list)
    unread_count: int = 0
    last_text: str | None = None
    is_service: bool = False
    raw: Any = None

    @property
    def is_group(self) -> bool:
        chat_type = (self.type or "").upper()
        return not self.is_service and (
            self.chat_id < 0 or chat_type in {"CHAT", "GROUP", "CHANNEL"}
        )


@dataclass(slots=True)
class AuthChallenge:
    phone: str
    device_type: str
    temp_token: str
    device_id: str


@dataclass(slots=True)
class AuthResult:
    phone: str
    device_type: str
    token: str
    device_id: str
    me: UserInfo | None = None


@dataclass(slots=True)
class ActiveSessionInfo:
    client: str
    info: str
    location: str
    time: int
    current: bool = False


class SessionCredentialsMissingError(RuntimeError):
    pass


class SessionExpiredError(RuntimeError):
    pass


# =========================
# SESSION
# =========================

class MaxSession:
    def __init__(
        self,
        *,
        token: str | None = None,
        device_id: str | UUID | None = None,
        phone: str = "",
        device_type: str = "WEB",
        app_version: str = "25.12.13",
        work_dir: str = "cache",
        download_dir: str = "downloads",
        send_fake_telemetry: bool = False,
        reconnect: bool = True,
        use_cache_credentials: bool = False,
    ) -> None:
        self.token = (token or "").strip()
        self.device_id = None if not device_id else (UUID(str(device_id)) if not isinstance(device_id, UUID) else device_id)
        self.phone = phone.strip() or "+70000000000"
        self.device_type = (device_type or "WEB").strip().upper()
        self.app_version = app_version
        self.work_dir = work_dir
        self.download_dir = download_dir
        self.send_fake_telemetry = send_fake_telemetry
        self.reconnect = reconnect
        self.use_cache_credentials = bool(use_cache_credentials)
        self._explicit_credentials_supplied = bool(self.token) and self.device_id is not None
        self._cache_prepared_for_explicit_credentials = False

        self._client: MaxClient | None = None
        self._client_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._started = asyncio.Event()
        self._closed = False

        self._user_cache: dict[int, UserInfo] = {}
        self._chat_name_cache: dict[int, str] = {}
        self._attachment_index: dict[str, AttachmentInfo] = {}
        self._auth_client: MaxClient | None = None
        self._auth_challenge: AuthChallenge | None = None

    # -------------------------
    # lifecycle
    # -------------------------

    @property
    def client(self) -> MaxClient:
        if self._client is None:
            raise RuntimeError("Session is not initialized")
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "is_connected", False))

    @property
    def has_credentials(self) -> bool:
        if bool(self.token) and self.device_id is not None:
            return True
        if self.use_cache_credentials:
            return self._has_cached_credentials()
        return False

    @property
    def me_id(self) -> int:
        me = getattr(self.client, "me", None)
        return int(getattr(me, "id", 0) or 0)

    @property
    def me_name(self) -> str:
        me = getattr(self.client, "me", None)
        return self._display_name_from_user_obj(me) if me else "Unknown"

    def _has_profile(self) -> bool:
        return self._client is not None and getattr(self._client, "me", None) is not None

    @staticmethod
    def _normalize_device_type(device_type: str | None) -> str:
        return (device_type or "WEB").strip().upper()

    @staticmethod
    def _is_login_device_type(device_type: str) -> bool:
        return device_type in {"DESKTOP", "ANDROID", "IOS"}

    @staticmethod
    def _is_invalid_session_error(exc: Exception) -> bool:
        return isinstance(exc, PyMaxError) and getattr(exc, "error", None) == "login.token"

    def _build_user_agent(self, device_type: str | None = None) -> UserAgentPayload:
        return UserAgentPayload(
            device_type=self._normalize_device_type(device_type or self.device_type),
            app_version=self.app_version,
        )

    def _has_cached_credentials(self) -> bool:
        try:
            token = Database(self.work_dir).get_auth_token()
        except Exception:
            return False
        return bool(str(token or "").strip())

    def _make_client(
        self,
        *,
        phone: str,
        token: str | None,
        device_id: UUID | None,
        device_type: str,
        work_dir: str,
        reconnect: bool,
    ) -> MaxClient:
        normalized_type = self._normalize_device_type(device_type)
        client_cls = MaxClient if normalized_type == "WEB" else SocketMaxClient
        return client_cls(
            phone=phone,
            token=token,
            device_id=device_id,
            headers=self._build_user_agent(normalized_type),
            work_dir=work_dir,
            send_fake_telemetry=self.send_fake_telemetry,
            reconnect=reconnect,
        )

    def _create_client(self) -> MaxClient:
        if not self.has_credentials:
            raise SessionCredentialsMissingError("Session credentials are missing")

        self._prepare_cache_for_explicit_credentials()

        client = self._make_client(
            phone=self.phone,
            token=self.token,
            device_id=self.device_id,
            device_type=self.device_type,
            work_dir=self.work_dir,
            reconnect=self.reconnect,
        )

        @client.on_start
        async def _on_start() -> None:
            self._started.set()

        return client

    def _reset_runtime_state(self) -> None:
        self._user_cache.clear()
        self._chat_name_cache.clear()
        self._attachment_index.clear()

    def _clear_runtime_cache_dirs(self, *, recreate: bool) -> None:
        work_path = Path(self.work_dir)
        shutil.rmtree(work_path, ignore_errors=True)
        if recreate:
            work_path.mkdir(parents=True, exist_ok=True)

    def _prepare_cache_for_explicit_credentials(self) -> None:
        if not self._explicit_credentials_supplied or self._cache_prepared_for_explicit_credentials:
            return
        self._clear_runtime_cache_dirs(recreate=True)
        self._cache_prepared_for_explicit_credentials = True

    async def connect(self, *, timeout: float = 30.0, force_restart: bool = False) -> None:
        if not self.has_credentials:
            raise SessionCredentialsMissingError("Session credentials are missing")

        async with self._connect_lock:
            self._closed = False

            need_new_client = (
                force_restart
                or self._client is None
                or self._client_task is None
                or self._client_task.done()
            )

            if need_new_client:
                old_client = self._client
                if old_client is not None:
                    with contextlib.suppress(Exception):
                        await old_client.close()

                self._started = asyncio.Event()
                self._client = self._create_client()
                self._client_task = asyncio.create_task(self._client.start())

            if self.is_connected and self._has_profile():
                return

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not (self.is_connected and self._has_profile()):
                task = self._client_task
                if task is not None and task.done():
                    if task.cancelled():
                        raise RuntimeError("MAX client task was cancelled")
                    exc = task.exception()
                    if exc is not None:
                        if self._is_invalid_session_error(exc):
                            raise SessionExpiredError("Эта сессия просрочена, войдите заново") from exc
                        raise exc
                    force_restart = True
                    self._started = asyncio.Event()
                    self._client = self._create_client()
                    self._client_task = asyncio.create_task(self._client.start())
                    task = self._client_task

                if loop.time() >= deadline:
                    raise SessionExpiredError("Эта сессия просрочена, войдите заново")
                await asyncio.sleep(0.1)

    async def ensure_connected(self, *, timeout: float = 15.0, force_restart: bool = False) -> None:
        if self.is_connected and self._has_profile() and not force_restart:
            return
        await self.connect(timeout=timeout, force_restart=force_restart)

    @staticmethod
    def _is_retryable_connection_error(exc: Exception) -> bool:
        if isinstance(exc, (WebSocketNotConnectedError, SocketNotConnectedError, SocketSendError)):
            return True
        if isinstance(exc, RuntimeError):
            text = str(exc)
            if "Send and wait failed" in text or "WebSocket is not connected" in text or "Socket is not connected" in text:
                return True
        return False

    async def _call_with_connection(self, func, *, retry: bool = True):
        for attempt in range(2 if retry else 1):
            await self.ensure_connected(force_restart=(attempt > 0))
            try:
                async with self._request_lock:
                    return await func()
            except Exception as e:
                if self._is_invalid_session_error(e):
                    raise SessionExpiredError("Эта сессия просрочена, войдите заново") from e
                if not self._is_retryable_connection_error(e) and self.is_connected:
                    raise
                if attempt + 1 >= (2 if retry else 1):
                    raise
        raise RuntimeError("Connection retry failed")

    async def close(self) -> None:
        await self.cancel_auth_flow()
        if self._client is None or self._closed:
            return
        self._closed = True
        await self._client.close()
        task = self._client_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._client_task = None
        self._client = None

    def update_credentials(
        self,
        *,
        token: str,
        device_id: str | UUID,
        phone: str,
        device_type: str,
    ) -> None:
        self.token = (token or "").strip()
        self.device_id = UUID(str(device_id)) if not isinstance(device_id, UUID) else device_id
        self.phone = phone.strip() or "+70000000000"
        self.device_type = self._normalize_device_type(device_type)
        self.use_cache_credentials = False
        self._explicit_credentials_supplied = bool(self.token) and self.device_id is not None
        self._cache_prepared_for_explicit_credentials = False
        self._reset_runtime_state()

    def clear_credentials(self) -> None:
        self.token = ""
        self.device_id = None
        self._explicit_credentials_supplied = False
        self._cache_prepared_for_explicit_credentials = False

    def clear_account_identity(self) -> None:
        self.clear_credentials()
        self.phone = "+70000000000"
        self.device_type = "WEB"
        self._reset_runtime_state()

    async def clear_runtime_cache(self, *, recreate: bool = True) -> None:
        await self.close()
        self._clear_runtime_cache_dirs(recreate=recreate)
        self._reset_runtime_state()

    def build_session_payload(self) -> dict[str, str]:
        current_token = self.token
        current_device_id = str(self.device_id) if self.device_id is not None else ""
        if self._client is not None:
            current_token = str(getattr(self._client, "_token", "") or current_token or "").strip()
            raw_device_id = getattr(self._client, "_device_id", None)
            if raw_device_id is not None:
                current_device_id = str(raw_device_id)
        return {
            "token": current_token,
            "device_id": current_device_id,
            "phone": self.phone,
            "device_type": self.device_type,
            "app_version": self.app_version,
        }

    async def request_login_code(self, phone: str, device_type: str) -> AuthChallenge:
        normalized_type = self._normalize_device_type(device_type)
        if not self._is_login_device_type(normalized_type):
            raise ValueError("Поддерживаются только DESKTOP, ANDROID или IOS")

        await self.clear_runtime_cache()
        auth_work_dir = str(Path(self.work_dir) / "_auth")
        client = self._make_client(
            phone=phone.strip(),
            token=None,
            device_id=None,
            device_type=normalized_type,
            work_dir=auth_work_dir,
            reconnect=False,
        )
        try:
            await client.connect(client.user_agent)
            temp_token = await client.request_code(phone.strip())
        except Exception:
            with contextlib.suppress(Exception):
                await client.close()
            raise

        challenge = AuthChallenge(
            phone=phone.strip(),
            device_type=normalized_type,
            temp_token=temp_token,
            device_id=str(getattr(client, "_device_id", "")),
        )
        self._auth_client = client
        self._auth_challenge = challenge
        return challenge

    async def complete_login_code(self, code: str) -> AuthResult:
        if self._auth_client is None or self._auth_challenge is None:
            raise RuntimeError("Login flow is not started")

        clean_code = (code or "").strip()
        if len(clean_code) != 6 or not clean_code.isdigit():
            raise ValueError("Код должен состоять из 6 цифр")

        client = self._auth_client
        challenge = self._auth_challenge

        await client.login_with_code(challenge.temp_token, clean_code, start=False)
        await client._sync(client.user_agent)

        token = str(getattr(client, "_token", "") or "").strip()
        device_id = str(getattr(client, "_device_id", "") or "").strip()
        me = getattr(client, "me", None)
        me_info = self._build_user_info(me) if me is not None else None

        if not token or not device_id:
            raise RuntimeError("Не удалось получить token или device_id")

        await self.cancel_auth_flow()
        await self.clear_runtime_cache()
        self.update_credentials(
            token=token,
            device_id=device_id,
            phone=challenge.phone,
            device_type=challenge.device_type,
        )

        return AuthResult(
            phone=challenge.phone,
            device_type=challenge.device_type,
            token=token,
            device_id=device_id,
            me=me_info,
        )

    async def cancel_auth_flow(self) -> None:
        client = self._auth_client
        self._auth_client = None
        self._auth_challenge = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def list_active_sessions(self) -> list[ActiveSessionInfo]:
        sessions = await self._call_with_connection(lambda: self.client.get_sessions())
        result = [
            ActiveSessionInfo(
                client=str(getattr(item, "client", "") or ""),
                info=str(getattr(item, "info", "") or ""),
                location=str(getattr(item, "location", "") or ""),
                time=int(getattr(item, "time", 0) or 0),
                current=bool(getattr(item, "current", False)),
            )
            for item in sessions
        ]
        result.sort(key=lambda item: (not item.current, -(item.time or 0), item.client))
        return result

    async def close_other_sessions(self) -> None:
        await self._call_with_connection(lambda: self.client.close_all_sessions(), retry=False)

    async def logout_current_session(self) -> None:
        logout_error: Exception | None = None
        try:
            await self._call_with_connection(lambda: self.client.logout(), retry=False)
        except Exception as e:
            logout_error = e
        finally:
            await self.clear_runtime_cache(recreate=False)
            self.clear_credentials()

        if logout_error is not None:
            raise logout_error

    # -------------------------
    # helpers
    # -------------------------

    @staticmethod
    def _safe_get(obj: Any, *names: str, default=None):
        if isinstance(obj, dict):
            for name in names:
                if name in obj:
                    return obj[name]
            return default
        for name in names:
            if hasattr(obj, name):
                return getattr(obj, name)
        return default

    @staticmethod
    def _normalize_user_ids(participants: dict[Any, Any] | None) -> list[int]:
        if not participants:
            return []
        result: list[int] = []
        for uid in participants.keys():
            try:
                result.append(int(uid))
            except Exception:
                continue
        return result

    @staticmethod
    def _ext_from_name(name: str | None) -> str | None:
        if not name:
            return None
        suffix = Path(name).suffix.lower()
        return suffix or None

    def _extract_unread_count(self, chat_obj: Any) -> int:
        direct = self._safe_get(
            chat_obj,
            "unread_count",
            "unreadCount",
            "counter",
            "unread",
            "newMessages",
            "new_messages",
            "messageCounter",
            "unreadMessagesCount",
            default=None,
        )
        if direct is not None:
            try:
                return int(direct)
            except Exception:
                pass

        if isinstance(chat_obj, dict):
            for key, value in chat_obj.items():
                key_lower = str(key).lower()
                if isinstance(value, dict) and key_lower in {"readstate", "read_state", "counters"}:
                    nested = self._extract_unread_count(value)
                    if nested:
                        return nested
                if key_lower in {
                    "unread",
                    "unreadcount",
                    "unread_count",
                    "newmessages",
                    "new_messages",
                    "unreadmessagescount",
                    "messagecounter",
                    "counter",
                }:
                    try:
                        return int(value)
                    except Exception:
                        continue
        return 0

    def _last_message_is_outgoing(self, chat_obj: Any) -> bool:
        if self.me_id is None:
            return False

        last_message = self._safe_get(chat_obj, "last_message", "lastMessage", default=None)
        if not last_message:
            return False

        sender_id = self._safe_get(last_message, "sender", "sender_id", default=None)
        try:
            return int(sender_id) == int(self.me_id)
        except Exception:
            return False

    @staticmethod
    async def _http_download(url: str, path: str) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                data = await resp.read()

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def _display_name_from_user_obj(self, user: Any) -> str:
        if user is None:
            return "Unknown"

        first_name, last_name = self._extract_user_name_parts(user)
        full_name = f"{first_name} {last_name}".strip()

        if full_name:
            return full_name

        username = self._safe_get(user, "username", "nick", "login", default=None)
        if username:
            return username

        text = str(user)
        if ": " in text:
            return text.split(": ", 1)[1].strip()

        return text

    def _extract_user_name_parts(self, user: Any) -> tuple[str, str]:
        first_name = self._safe_get(user, "first_name", "firstName", default="") or ""
        last_name = self._safe_get(user, "last_name", "lastName", default="") or ""
        if first_name or last_name:
            return str(first_name), str(last_name)

        names = self._safe_get(user, "names", default=None)
        if isinstance(names, list):
            for item in names:
                item_first = self._safe_get(item, "first_name", "firstName", default="") or ""
                item_last = self._safe_get(item, "last_name", "lastName", default="") or ""
                if item_first or item_last:
                    return str(item_first), str(item_last)

                item_name = self._safe_get(item, "name", default="") or ""
                if item_name:
                    return str(item_name), ""

        return "", ""

    def _build_user_info(self, user: Any) -> UserInfo:
        user_id = int(self._safe_get(user, "id", "user_id", default=0) or 0)
        first_name, last_name = self._extract_user_name_parts(user)
        info = UserInfo(
            user_id=user_id,
            display_name=self._display_name_from_user_obj(user),
            first_name=first_name,
            last_name=last_name,
            username=self._safe_get(user, "username", "nick", "login", default=None),
            phone=self._safe_get(user, "phone", default=None),
            description=self._safe_get(user, "description", "about", default=None),
            avatar_url=self._safe_get(
                user, "avatar_url", "photo_url", "base_icon_url", "icon_url", default=None
            ),
            is_online=self._safe_get(
                user, "online", "is_online", "isOnline", "onlineStatus", default=None
            ),
            last_seen=self._safe_get(
                user, "last_seen", "lastSeen", "last_activity", "lastActivity", default=None
            ),
            raw=user,
        )
        self._user_cache[info.user_id] = info
        return info

    async def _resolve_user(self, user_id: int) -> UserInfo | None:
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            cached = self.client.get_cached_user(user_id)
            if cached:
                return self._build_user_info(cached)
        except Exception:
            pass

        try:
            user = await self.client.get_user(user_id)
            if user:
                return self._build_user_info(user)
        except Exception:
            pass

        try:
            users = await self.client.fetch_users([user_id])
            if isinstance(users, list) and users:
                return self._build_user_info(users[0])
            if isinstance(users, dict):
                maybe_user = users.get(user_id) or users.get(str(user_id))
                if maybe_user:
                    return self._build_user_info(maybe_user)
        except Exception:
            pass

        return None

    async def _resolve_chat_name(self, chat_obj: Any) -> str:
        name, _, _ = await self._resolve_chat_title_parts(chat_obj)
        return name

    async def _resolve_chat_title_parts(self, chat_obj: Any) -> tuple[str, str, str]:
        chat_id = int(self._safe_get(chat_obj, "id", default=0) or 0)
        if chat_id == 0:
            return "Избранное", "", ""

        if chat_id in self._chat_name_cache:
            return self._chat_name_cache[chat_id], "", ""

        chat_type = str(self._safe_get(chat_obj, "type", default="UNKNOWN") or "UNKNOWN").upper()
        options = self._safe_get(chat_obj, "options", default={}) or {}
        participants = self._safe_get(chat_obj, "participants", default={}) or {}
        participant_ids = self._normalize_user_ids(participants)
        other_ids = [uid for uid in participant_ids if uid != self.me_id]
        title = self._safe_get(chat_obj, "title", "name", default=None)
        if title is not None:
            title = str(title)

        if options.get("SERVICE_CHAT"):
            if other_ids:
                user = await self._resolve_user(other_ids[0])
                if user:
                    self._chat_name_cache[chat_id] = user.display_name
                    return user.display_name, user.first_name, user.last_name
            self._chat_name_cache[chat_id] = "MAX"
            return "MAX", "", ""

        if chat_type == "DIALOG" and other_ids:
            user = await self._resolve_user(other_ids[0])
            if user:
                self._chat_name_cache[chat_id] = user.display_name
                return user.display_name, user.first_name, user.last_name

        if title:
            self._chat_name_cache[chat_id] = title
            return title, "", ""

        if other_ids:
            user = await self._resolve_user(other_ids[0])
            if user:
                self._chat_name_cache[chat_id] = user.display_name
                return user.display_name, user.first_name, user.last_name

        fallback = f"chat:{chat_id}"
        self._chat_name_cache[chat_id] = fallback
        return fallback, "", ""

    async def _get_all_dialog_objects(self) -> list[Any]:
        await self.ensure_connected()

        raw_dialogs = await self._fetch_dialog_entries_raw()
        if raw_dialogs:
            return raw_dialogs

        dialogs_obj = getattr(self.client, "dialogs", None)

        if dialogs_obj:
            if isinstance(dialogs_obj, dict):
                values = list(dialogs_obj.values())
                if values:
                    return values
            elif isinstance(dialogs_obj, list):
                if dialogs_obj:
                    return dialogs_obj

        try:
            chats = await self.client.fetch_chats()
        except WebSocketNotConnectedError:
            return []
        return chats or []

    async def _fetch_dialog_entries_raw(self) -> list[dict[str, Any]]:
        if not self.is_connected:
            return []

        sender = getattr(self.client, "_send_and_wait", None)
        if sender is None:
            return []

        payload = FetchChatsPayload(marker=int(time.time() * 1000)).model_dump(by_alias=True)
        try:
            data = await self._call_with_connection(
                lambda: sender(opcode=Opcode.CHATS_LIST, payload=payload),
            )
        except WebSocketNotConnectedError:
            return []
        except Exception:
            return []

        raw_payload = data.get("payload", {}) if isinstance(data, dict) else {}
        chats = raw_payload.get("chats", [])
        return chats if isinstance(chats, list) else []

    def _build_attachment_info(
        self,
        *,
        chat_id: int,
        message_id: int,
        att: Any,
    ) -> AttachmentInfo:
        att_type = str(self._safe_get(att, "type", default="")).upper()

        if "PHOTO" in att_type or att.__class__.__name__ == "PhotoAttach":
            photo_id = str(self._safe_get(att, "photo_id", "id", default=""))
            info = AttachmentInfo(
                kind="photo",
                attach_id=photo_id,
                name=f"{photo_id}.jpg",
                ext=".jpg",
                url=self._safe_get(att, "base_url", default=None),
                token=self._safe_get(att, "photo_token", "token", default=None),
                width=self._safe_get(att, "width", default=None),
                height=self._safe_get(att, "height", default=None),
                message_id=message_id,
                chat_id=chat_id,
                raw=att,
            )
            self._attachment_index[photo_id] = info
            return info

        if "FILE" in att_type or att.__class__.__name__ == "FileAttach":
            file_id = str(self._safe_get(att, "file_id", "id", default=""))
            file_name = self._safe_get(att, "name", "file_name", "filename", default=None)
            info = AttachmentInfo(
                kind="file",
                attach_id=file_id,
                name=file_name,
                ext=self._ext_from_name(file_name),
                size=self._safe_get(att, "size", default=None),
                token=self._safe_get(att, "token", default=None),
                message_id=message_id,
                chat_id=chat_id,
                raw=att,
            )
            self._attachment_index[file_id] = info
            return info

        if "VIDEO" in att_type or att.__class__.__name__ == "VideoAttach":
            video_id = str(self._safe_get(att, "video_id", "id", default=""))
            info = AttachmentInfo(
                kind="video",
                attach_id=video_id,
                message_id=message_id,
                chat_id=chat_id,
                raw=att,
            )
            self._attachment_index[video_id] = info
            return info

        unknown_id = str(self._safe_get(att, "id", default="unknown"))
        info = AttachmentInfo(
            kind="unknown",
            attach_id=unknown_id,
            message_id=message_id,
            chat_id=chat_id,
            raw=att,
        )
        self._attachment_index[unknown_id] = info
        return info

    def _build_attachment_label(self, att: Any) -> str:
        att_type = str(self._safe_get(att, "type", default="")).upper()

        if "PHOTO" in att_type or att.__class__.__name__ == "PhotoAttach":
            photo_id = str(self._safe_get(att, "photo_id", "id", default=""))
            return f"[photo:{photo_id}]"

        if "FILE" in att_type or att.__class__.__name__ == "FileAttach":
            file_id = str(self._safe_get(att, "file_id", "id", default=""))
            file_name = self._safe_get(att, "name", "file_name", "filename", default=None)
            suffix = self._ext_from_name(file_name) or ""
            return f"[file:{file_id}{suffix}]"

        if "VIDEO" in att_type or att.__class__.__name__ == "VideoAttach":
            video_id = str(self._safe_get(att, "video_id", "id", default=""))
            return f"[video:{video_id}]"

        attach_id = str(self._safe_get(att, "id", default="unknown"))
        return f"[attach:{attach_id}]"

    async def _resolve_message_sender_name(self, msg: Any, chat_name: str | None = None) -> str:
        sender_id = self._safe_get(msg, "sender", "sender_id", default=None)
        try:
            sender_id = int(sender_id) if sender_id is not None else None
        except Exception:
            sender_id = None

        if sender_id == self.me_id:
            return "Вы"
        if sender_id is not None:
            user = await self._resolve_user(sender_id)
            return user.display_name if user else (chat_name or str(sender_id))
        return chat_name or "Unknown"

    async def _extract_reply_preview(
        self,
        reply_link: Any,
        *,
        chat_name: str | None = None,
    ) -> tuple[int | None, str | None, str | None]:
        if reply_link is None:
            return None, None, None

        reply_msg = self._safe_get(reply_link, "message", default=None)
        reply_to = self._safe_get(reply_link, "message_id", "messageId", default=None)

        if reply_msg is not None:
            reply_to = self._safe_get(reply_msg, "id", default=reply_to)

        try:
            reply_to_int = int(reply_to) if reply_to is not None else None
        except Exception:
            reply_to_int = None

        if reply_msg is None:
            return reply_to_int, None, None

        reply_sender_name = await self._resolve_message_sender_name(reply_msg, chat_name=chat_name)
        reply_text = (self._safe_get(reply_msg, "text", default="") or "").strip()

        if not reply_text:
            reply_attaches = self._safe_get(reply_msg, "attaches", default=[]) or []
            if reply_attaches:
                reply_text = " ".join(self._build_attachment_label(att) for att in reply_attaches)

        return reply_to_int, reply_sender_name, reply_text or None

    async def _build_message_info(self, chat_id: int, msg: Any, chat_name: str | None = None) -> MessageInfo:
        message_id = int(self._safe_get(msg, "id", default=0) or 0)
        sender_id = self._safe_get(msg, "sender", "sender_id", default=None)
        try:
            sender_id = int(sender_id) if sender_id is not None else None
        except Exception:
            sender_id = None

        sender_name = await self._resolve_message_sender_name(msg, chat_name=chat_name)

        attaches = self._safe_get(msg, "attaches", default=[]) or []
        att_infos = [
            self._build_attachment_info(chat_id=chat_id, message_id=message_id, att=att)
            for att in attaches
        ]

        reply_link = self._safe_get(msg, "link", default=None)
        reply_to, reply_sender_name, reply_preview_text = await self._extract_reply_preview(
            reply_link,
            chat_name=chat_name,
        )

        return MessageInfo(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            timestamp_ms=self._safe_get(msg, "time", "timestamp", default=None),
            text=self._safe_get(msg, "text", default="") or "",
            attachments=att_infos,
            reply_to=reply_to,
            reply_sender_name=reply_sender_name,
            reply_preview_text=reply_preview_text,
            is_outgoing=(sender_id == self.me_id),
            raw=msg,
        )

    # -------------------------
    # public API
    # -------------------------

    async def get_me(self) -> UserInfo:
        await self.ensure_connected()
        me = getattr(self.client, "me", None)
        if me is None:
            await self.ensure_connected(force_restart=True)
            me = getattr(self.client, "me", None)
        if me is None:
            raise RuntimeError("Client has no 'me'")
        return self._build_user_info(me)

    async def get_user_info(self, user_id: int) -> UserInfo | None:
        await self.ensure_connected()
        return await self._resolve_user(user_id)

    async def list_dialogs(self) -> list[ChatInfo]:
        dialogs = await self._get_all_dialog_objects()
        result: list[ChatInfo] = []

        for chat in dialogs:
            chat_id = int(self._safe_get(chat, "id", default=0) or 0)
            name, first_name, last_name = await self._resolve_chat_title_parts(chat)
            participants = self._safe_get(chat, "participants", default={}) or {}
            participant_ids = self._normalize_user_ids(participants)
            options = self._safe_get(chat, "options", default={}) or {}
            last_message = self._safe_get(chat, "last_message", "lastMessage", default=None)

            last_text = None
            if last_message:
                last_text = self._safe_get(last_message, "text", default=None)

            unread_count = self._extract_unread_count(chat)
            if unread_count > 0 and self._last_message_is_outgoing(chat):
                unread_count = 0

            result.append(
                ChatInfo(
                    chat_id=chat_id,
                    name=name,
                    type=str(self._safe_get(chat, "type", default="UNKNOWN")),
                    first_name=first_name,
                    last_name=last_name,
                    participant_ids=participant_ids,
                    unread_count=unread_count,
                    last_text=last_text,
                    is_service=bool(options.get("SERVICE_CHAT")),
                    raw=chat,
                )
            )

        return result

    async def get_chat_info(self, chat_id: int) -> ChatInfo | None:
        dialogs = await self._get_all_dialog_objects()
        for chat in dialogs:
            if int(self._safe_get(chat, "id", default=0) or 0) == chat_id:
                name, first_name, last_name = await self._resolve_chat_title_parts(chat)
                participants = self._safe_get(chat, "participants", default={}) or {}
                participant_ids = self._normalize_user_ids(participants)
                options = self._safe_get(chat, "options", default={}) or {}
                last_message = self._safe_get(chat, "last_message", "lastMessage", default=None)
                last_text = self._safe_get(last_message, "text", default=None) if last_message else None
                unread_count = self._extract_unread_count(chat)
                if unread_count > 0 and self._last_message_is_outgoing(chat):
                    unread_count = 0
                return ChatInfo(
                    chat_id=chat_id,
                    name=name,
                    type=str(self._safe_get(chat, "type", default="UNKNOWN")),
                    first_name=first_name,
                    last_name=last_name,
                    participant_ids=participant_ids,
                    unread_count=unread_count,
                    last_text=last_text,
                    is_service=bool(options.get("SERVICE_CHAT")),
                    raw=chat,
                )
        return None

    async def get_history(
        self,
        chat_id: int,
        *,
        from_time: int | None = None,
        backward: int = 20,
        forward: int = 0,
    ) -> list[MessageInfo]:
        chat = await self.get_chat_info(chat_id)
        chat_name = chat.name if chat else None

        history = await self._call_with_connection(
            lambda: self.client.fetch_history(
                chat_id=chat_id,
                from_time=from_time,
                forward=forward,
                backward=backward,
            )
        )

        if not history:
            return []

        result: list[MessageInfo] = []
        for msg in history:
            result.append(await self._build_message_info(chat_id, msg, chat_name=chat_name))
        return result

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        notify: bool = True,
    ) -> MessageInfo | None:
        chat = await self.get_chat_info(chat_id)
        result = await self._call_with_connection(
            lambda: self.client.send_message(
                text,
                chat_id,
                notify=notify,
                reply_to=reply_to,
            )
        )
        if not result:
            return None
        return await self._build_message_info(chat_id, result, chat_name=(chat.name if chat else None))

    async def send_photo(
        self,
        chat_id: int,
        path: str,
        *,
        text: str = "",
        reply_to: int | None = None,
        notify: bool = True,
    ) -> MessageInfo | None:
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        chat = await self.get_chat_info(chat_id)
        photo = Photo(path=path)
        result = await self._call_with_connection(
            lambda: self.client.send_message(
                text,
                chat_id,
                notify=notify,
                attachment=photo,
                reply_to=reply_to,
            )
        )
        if not result:
            return None
        return await self._build_message_info(chat_id, result, chat_name=(chat.name if chat else None))

    async def send_file(
        self,
        chat_id: int,
        path: str,
        *,
        text: str = "",
        reply_to: int | None = None,
        notify: bool = True,
    ) -> MessageInfo | None:
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        chat = await self.get_chat_info(chat_id)
        file_obj = File(path=path)
        result = await self._call_with_connection(
            lambda: self.client.send_message(
                text,
                chat_id,
                notify=notify,
                attachment=file_obj,
                reply_to=reply_to,
            )
        )
        if not result:
            return None
        return await self._build_message_info(chat_id, result, chat_name=(chat.name if chat else None))

    async def mark_read(self, chat_id: int, message_id: int) -> int:
        sender = getattr(self.client, "_send_and_wait", None)
        if sender is None:
            raise RuntimeError("MAX client has no _send_and_wait")

        payload = {
            "type": "READ_MESSAGE",
            "chatId": int(chat_id),
            "messageId": int(message_id),
            "mark": int(time.time() * 1000),
        }
        data = await self._call_with_connection(
            lambda: sender(opcode=Opcode.CHAT_MARK, payload=payload),
        )
        result = data.get("payload", {}) if isinstance(data, dict) else {}
        return int(self._safe_get(result, "unread", default=0) or 0)

    async def download_attachment(
        self,
        attachment: AttachmentInfo,
        *,
        save_path: str | None = None,
    ) -> str:
        if not attachment.chat_id or not attachment.message_id:
            raise ValueError("Attachment has no chat_id/message_id")

        if attachment.kind == "photo":
            if not attachment.url:
                raise RuntimeError("Photo attachment has no URL")
            filename = save_path or os.path.join(
                self.download_dir,
                attachment.name or f"photo_{attachment.attach_id}.jpg",
            )
            await self._http_download(attachment.url, filename)
            return os.path.abspath(filename)

        if attachment.kind == "file":
            req = await self._call_with_connection(
                lambda: self.client.get_file_by_id(
                    attachment.chat_id,
                    attachment.message_id,
                    int(attachment.attach_id),
                )
            )
            if not req:
                raise RuntimeError("get_file_by_id returned None")

            url = (
                self._safe_get(req, "url", default=None)
                or self._safe_get(req, "download_url", default=None)
                or self._safe_get(req, "file_url", default=None)
                or self._safe_get(req, "base_url", default=None)
            )
            if not url:
                raise RuntimeError("No file URL found in get_file_by_id response")

            filename = save_path or os.path.join(
                self.download_dir,
                attachment.name or f"file_{attachment.attach_id}",
            )
            await self._http_download(url, filename)
            return os.path.abspath(filename)

        raise NotImplementedError(f"Download for kind={attachment.kind} is not implemented")

    def find_cached_attachment(self, attach_id: str | int) -> AttachmentInfo | None:
        return self._attachment_index.get(str(attach_id))

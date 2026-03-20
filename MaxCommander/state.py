# state.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
import re

from pymax.exceptions import WebSocketNotConnectedError

from .session import (
    ActiveSessionInfo,
    AuthResult,
    MaxSession,
    ChatInfo,
    MessageInfo,
    AttachmentInfo,
    SessionCredentialsMissingError,
    SessionExpiredError,
)


@dataclass(slots=True)
class DialogListState:
    items: list[ChatInfo] = field(default_factory=list)
    selected_index: int = 0

    def clear(self) -> None:
        self.items.clear()
        self.selected_index = 0

    @property
    def is_empty(self) -> bool:
        return len(self.items) == 0

    @property
    def selected(self) -> ChatInfo | None:
        if self.is_empty:
            return None
        if self.selected_index < 0:
            self.selected_index = 0
        if self.selected_index >= len(self.items):
            self.selected_index = len(self.items) - 1
        return self.items[self.selected_index]

    def set_items(self, items: Iterable[ChatInfo]) -> None:
        old_selected_id = self.selected.chat_id if self.selected else None

        self.items = list(items)

        if not self.items:
            self.selected_index = 0
            return

        if old_selected_id is not None:
            for i, item in enumerate(self.items):
                if item.chat_id == old_selected_id:
                    self.selected_index = i
                    return

        self.selected_index = min(self.selected_index, len(self.items) - 1)
        self.selected_index = max(self.selected_index, 0)

    def move_up(self, step: int = 1) -> None:
        if self.is_empty:
            return
        self.selected_index = max(0, self.selected_index - step)

    def move_down(self, step: int = 1) -> None:
        if self.is_empty:
            return
        self.selected_index = min(len(self.items) - 1, self.selected_index + step)

    def select_by_chat_id(self, chat_id: int) -> bool:
        for i, item in enumerate(self.items):
            if item.chat_id == chat_id:
                self.selected_index = i
                return True
        return False

    def set_unread_count(self, chat_id: int, unread_count: int) -> bool:
        for item in self.items:
            if item.chat_id == chat_id:
                if item.unread_count == unread_count:
                    return False
                item.unread_count = unread_count
                return True
        return False


@dataclass(slots=True)
class MessageListState:
    chat_id: int | None = None
    chat_name: str | None = None
    items: list[MessageInfo] = field(default_factory=list)
    selected_index: int = 0
    oldest_time: int | None = None
    newest_time: int | None = None
    has_more_history: bool = True
    is_loading: bool = False
    scroll_to_bottom_on_refresh: bool = False
    center_selection_on_refresh: bool = False

    def clear(self) -> None:
        self.chat_id = None
        self.chat_name = None
        self.items.clear()
        self.selected_index = 0
        self.oldest_time = None
        self.newest_time = None
        self.has_more_history = True
        self.is_loading = False
        self.scroll_to_bottom_on_refresh = False
        self.center_selection_on_refresh = False

    @property
    def is_empty(self) -> bool:
        return len(self.items) == 0

    @property
    def selected(self) -> MessageInfo | None:
        if self.is_empty:
            return None
        if self.selected_index < 0:
            self.selected_index = 0
        if self.selected_index >= len(self.items):
            self.selected_index = len(self.items) - 1
        return self.items[self.selected_index]

    def set_chat(self, chat_id: int, chat_name: str) -> None:
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.items.clear()
        self.selected_index = 0
        self.oldest_time = None
        self.newest_time = None
        self.has_more_history = True
        self.is_loading = False
        self.scroll_to_bottom_on_refresh = False
        self.center_selection_on_refresh = False

    def replace_items(self, messages: list[MessageInfo]) -> None:
        self.items = sorted(messages, key=lambda m: (m.timestamp_ms or 0, m.message_id))
        self.selected_index = max(0, len(self.items) - 1 if self.items else 0)
        self.scroll_to_bottom_on_refresh = bool(self.items)
        self.center_selection_on_refresh = False
        self._recalc_bounds()

    def prepend_items(self, messages: list[MessageInfo]) -> int:
        """
        Добавляет более старые сообщения вверх.
        Возвращает количество реально новых сообщений.
        """
        if not messages:
            return 0

        existing_ids = {m.message_id for m in self.items}
        new_items = [m for m in messages if m.message_id not in existing_ids]
        if not new_items:
            return 0

        old_selected_id = self.selected.message_id if self.selected else None

        self.items = sorted(new_items + self.items, key=lambda m: (m.timestamp_ms or 0, m.message_id))

        if old_selected_id is not None:
            for i, msg in enumerate(self.items):
                if msg.message_id == old_selected_id:
                    self.selected_index = i
                    break

        self.scroll_to_bottom_on_refresh = False
        self.center_selection_on_refresh = False
        self._recalc_bounds()
        return len(new_items)

    def append_items(self, messages: list[MessageInfo]) -> int:
        """
        Добавляет новые сообщения вниз.
        Возвращает количество реально новых сообщений.
        """
        if not messages:
            return 0

        existing_ids = {m.message_id for m in self.items}
        new_items = [m for m in messages if m.message_id not in existing_ids]
        if not new_items:
            return 0

        old_selected_id = self.selected.message_id if self.selected else None
        was_at_bottom = self.is_at_bottom()

        self.items = sorted(self.items + new_items, key=lambda m: (m.timestamp_ms or 0, m.message_id))

        if old_selected_id is not None:
            for i, msg in enumerate(self.items):
                if msg.message_id == old_selected_id:
                    self.selected_index = i
                    break
            else:
                self.selected_index = max(0, len(self.items) - 1)
        else:
            self.selected_index = max(0, len(self.items) - 1)

        self.scroll_to_bottom_on_refresh = was_at_bottom
        self.center_selection_on_refresh = False
        self._recalc_bounds()
        return len(new_items)

    def add_sent_message(self, msg: MessageInfo) -> None:
        self.append_items([msg])
        self.selected_index = max(0, len(self.items) - 1)
        self.scroll_to_bottom_on_refresh = True
        self.center_selection_on_refresh = False

    def move_up(self, step: int = 1) -> None:
        if self.is_empty:
            return
        self.selected_index = max(0, self.selected_index - step)
        self.scroll_to_bottom_on_refresh = False
        self.center_selection_on_refresh = False

    def move_down(self, step: int = 1) -> None:
        if self.is_empty:
            return
        self.selected_index = min(len(self.items) - 1, self.selected_index + step)
        self.scroll_to_bottom_on_refresh = False
        self.center_selection_on_refresh = False

    def is_at_top(self) -> bool:
        return self.selected_index <= 0

    def is_at_bottom(self) -> bool:
        return self.selected_index >= max(0, len(self.items) - 1)

    def _recalc_bounds(self) -> None:
        if not self.items:
            self.oldest_time = None
            self.newest_time = None
            return

        times = [m.timestamp_ms for m in self.items if m.timestamp_ms is not None]
        if not times:
            self.oldest_time = None
            self.newest_time = None
            return

        self.oldest_time = min(times)
        self.newest_time = max(times)


@dataclass(slots=True)
class SearchResult:
    message_id: int
    chat_id: int
    sender_name: str
    timestamp_ms: int | None
    preview_text: str


@dataclass(slots=True)
class SearchState:
    active: bool = False
    input_mode: bool = False
    query: str = ""
    chat_id: int | None = None
    results: list[SearchResult] = field(default_factory=list)
    selected_index: int = 0
    oldest_time: int | None = None
    has_more_history: bool = False
    scanned_message_ids: set[int] = field(default_factory=set)

    def clear(self) -> None:
        self.active = False
        self.input_mode = False
        self.query = ""
        self.chat_id = None
        self.results.clear()
        self.selected_index = 0
        self.oldest_time = None
        self.has_more_history = False
        self.scanned_message_ids.clear()

    @property
    def visible_item_count(self) -> int:
        return len(self.results) + (1 if self.has_more_history else 0)

    @property
    def selected_result(self) -> SearchResult | None:
        if not self.results:
            return None
        if self.selected_index < 0:
            self.selected_index = 0
        if self.selected_index >= len(self.results):
            return None
        return self.results[self.selected_index]

    @property
    def is_load_more_selected(self) -> bool:
        return self.has_more_history and self.selected_index == len(self.results)

    def set_results(
        self,
        *,
        chat_id: int,
        query: str,
        results: list[SearchResult],
        oldest_time: int | None,
        has_more_history: bool,
        scanned_ids: set[int],
    ) -> None:
        self.active = True
        self.input_mode = False
        self.chat_id = chat_id
        self.query = query
        self.results = list(results)
        self.selected_index = 0
        self.oldest_time = oldest_time
        self.has_more_history = has_more_history
        self.scanned_message_ids = set(scanned_ids)

    def append_results(
        self,
        results: list[SearchResult],
        *,
        oldest_time: int | None,
        has_more_history: bool,
        scanned_ids: set[int],
    ) -> None:
        self.results.extend(results)
        self.oldest_time = oldest_time
        self.has_more_history = has_more_history
        self.scanned_message_ids.update(scanned_ids)
        max_index = max(0, self.visible_item_count - 1)
        self.selected_index = min(self.selected_index, max_index)


@dataclass(slots=True)
class StatusState:
    text: str = ""
    level: str = "info"  # info | warning | error | success

    def set(self, text: str, level: str = "info") -> None:
        self.text = text
        self.level = level

    def clear(self) -> None:
        self.text = ""
        self.level = "info"


@dataclass(slots=True)
class PollEvent:
    dialogs_changed: bool = False
    messages_changed: bool = False
    status_changed: bool = False
    new_message_count: int = 0
    error_text: str | None = None
    play_sound: bool = False

    @property
    def has_updates(self) -> bool:
        return (
            self.dialogs_changed
            or self.messages_changed
            or self.status_changed
            or self.error_text is not None
            or self.play_sound
        )


@dataclass(slots=True)
class ModalState:
    kind: str | None = None
    title: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.kind is not None

    def open(self, kind: str, title: str, **payload) -> None:
        self.kind = kind
        self.title = title
        self.payload = dict(payload)

    def close(self) -> None:
        self.kind = None
        self.title = ""
        self.payload.clear()


@dataclass(slots=True)
class ComposeState:
    text: str = ""
    reply_to_message_id: int | None = None
    attachment_path: str | None = None
    attachment_kind: str | None = None  # photo | file

    def clear(self) -> None:
        self.text = ""
        self.reply_to_message_id = None
        self.attachment_path = None
        self.attachment_kind = None


class AppState:
    """
    Центральное состояние Max Commander.

    Здесь нет прямой работы с pymax — только:
    - храним данные
    - вызываем MaxSession
    - готовим данные для TUI
    """

    def __init__(
        self,
        session: MaxSession,
        *,
        poll_interval: float = 5.0,
        poll_refresh_current_chat: bool = True,
        notification_sounds: bool = True,
        save_session_callback=None,
        clear_session_callback=None,
        clear_account_callback=None,
    ) -> None:
        self.session = session

        self.dialogs = DialogListState()
        self.messages = MessageListState()
        self.search = SearchState()
        self.status = StatusState()
        self.modal = ModalState()
        self.compose = ComposeState()

        self.me_name: str = ""
        self.me_id: int | None = None

        self.attachment_index: dict[str, AttachmentInfo] = {}
        self.is_connected: bool = False
        self.is_busy: bool = False
        self.poll_interval: float = max(1.0, float(poll_interval))
        self.poll_refresh_current_chat: bool = poll_refresh_current_chat
        self.notification_sounds: bool = bool(notification_sounds)
        self.save_session_callback = save_session_callback
        self.clear_session_callback = clear_session_callback
        self.clear_account_callback = clear_account_callback
        self.session_expired: bool = False

        self._poll_task: asyncio.Task[None] | None = None
        self._poll_stop = asyncio.Event()
        self._poll_events: asyncio.Queue[PollEvent | None] = asyncio.Queue()

    # -------------------------
    # high-level app lifecycle
    # -------------------------

    async def connect(self) -> None:
        self.is_busy = True
        try:
            await self.session.connect()
            me = await self.session.get_me()
            self.me_name = me.display_name
            self.me_id = me.user_id
            self.is_connected = True
            self.session_expired = False
            self.status.set(f"Вошёл как {self.me_name}", "success")
        except SessionCredentialsMissingError:
            self.is_connected = False
            self.status.set("Для работы нужно войти в систему", "warning")
            raise
        except SessionExpiredError:
            self.is_connected = False
            self.session_expired = True
            await self.session.close()
            self.session.clear_credentials()
            if callable(self.clear_session_callback):
                self.clear_session_callback()
            self.status.set("Эта сессия просрочена, войдите заново", "warning")
            raise
        except Exception as e:
            self.is_connected = False
            self.status.set(f"Ошибка подключения: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def close(self) -> None:
        self.is_busy = True
        try:
            await self.stop_polling()
            await self.session.close()
            self.is_connected = False
            self.status.set("Соединение закрыто", "info")
        finally:
            self.is_busy = False

    async def start_polling(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return

        self._poll_stop = asyncio.Event()
        while not self._poll_events.empty():
            try:
                self._poll_events.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        task = self._poll_task
        if task is None:
            return

        self._poll_stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._poll_task = None

        await self._poll_events.put(None)

    async def wait_for_poll_event(self) -> PollEvent | None:
        return await self._poll_events.get()

    def _dialogs_signature(self) -> tuple[tuple[int, int, str | None], ...]:
        return tuple(
            (chat.chat_id, chat.unread_count, chat.last_text)
            for chat in self.dialogs.items
        )

    async def _poll_loop(self) -> None:
        try:
            while not self._poll_stop.is_set():
                try:
                    if self.session.is_connected and not self.is_busy and not self.messages.is_loading:
                        event = await self.poll_once()
                        if event.has_updates:
                            await self._poll_events.put(event)
                    else:
                        self.is_connected = self.session.is_connected
                except asyncio.CancelledError:
                    raise
                except WebSocketNotConnectedError:
                    self.is_connected = False
                except Exception as e:
                    self.status.set(f"Ошибка фонового обновления: {e!r}", "error")
                    await self._poll_events.put(
                        PollEvent(
                            status_changed=True,
                            error_text=str(e),
                        )
                    )

                try:
                    await asyncio.wait_for(self._poll_stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def poll_once(self) -> PollEvent:
        event = PollEvent()
        previous_unread = {
            chat.chat_id: max(0, int(chat.unread_count or 0))
            for chat in self.dialogs.items
        }
        event.dialogs_changed = await self.refresh_dialogs(
            announce_status=False,
            track_busy=False,
        )

        unread_increased = any(
            max(0, int(chat.unread_count or 0)) > previous_unread.get(chat.chat_id, 0)
            for chat in self.dialogs.items
        )

        if self.poll_refresh_current_chat and self.current_chat_id is not None:
            event.new_message_count = await self.refresh_current_chat(
                announce_status=False,
                track_busy=False,
            )
            event.messages_changed = event.new_message_count > 0

        if self.notification_sounds and (unread_increased or event.new_message_count > 0):
            event.play_sound = True

        return event

    # -------------------------
    # dialogs
    # -------------------------

    def _current_chat_unread_count(self) -> int:
        chat_id = self.current_chat_id
        if chat_id is None:
            return 0

        for chat in self.dialogs.items:
            if chat.chat_id == chat_id:
                return max(0, int(chat.unread_count or 0))
        return 0

    def _sync_current_chat_unread_flags(self) -> bool:
        if not self.messages.items:
            return False

        remaining = self._current_chat_unread_count()
        changed = False

        for msg in self.messages.items:
            if msg.is_unread:
                msg.is_unread = False
                changed = True

        if remaining <= 0:
            return changed

        for msg in reversed(self.messages.items):
            if msg.is_outgoing:
                continue
            if not msg.is_unread:
                msg.is_unread = True
                changed = True
            remaining -= 1
            if remaining <= 0:
                break

        return changed

    async def refresh_dialogs(
        self,
        *,
        announce_status: bool = True,
        track_busy: bool = True,
    ) -> bool:
        old_signature = self._dialogs_signature()
        if track_busy:
            self.is_busy = True
        try:
            dialogs = await self.session.list_dialogs()
            self.dialogs.set_items(dialogs)
            self._sync_current_chat_unread_flags()
            if announce_status:
                self.status.set(f"Загружено диалогов: {len(dialogs)}", "info")
            return old_signature != self._dialogs_signature()
        except Exception as e:
            if announce_status:
                self.status.set(f"Ошибка загрузки диалогов: {e!r}", "error")
            raise
        finally:
            if track_busy:
                self.is_busy = False

    def get_selected_dialog(self) -> ChatInfo | None:
        return self.dialogs.selected

    async def open_selected_dialog(self, history_limit: int = 20) -> None:
        selected = self.dialogs.selected
        if selected is None:
            self.status.set("Нет выбранного чата", "warning")
            return
        await self.open_dialog(selected.chat_id, history_limit=history_limit)

    async def open_dialog(self, chat_id: int, history_limit: int = 20) -> None:
        self.is_busy = True
        try:
            if self.search.active and self.search.chat_id != chat_id:
                self.search.clear()
            chat = next((c for c in self.dialogs.items if c.chat_id == chat_id), None)
            if chat is None:
                chat = await self.session.get_chat_info(chat_id)

            if chat is None:
                self.status.set(f"Чат {chat_id} не найден", "warning")
                return

            self.dialogs.select_by_chat_id(chat.chat_id)
            self.messages.set_chat(chat.chat_id, chat.name)
            self.attachment_index.clear()

            history = await self.session.get_history(chat.chat_id, backward=history_limit)
            self.messages.replace_items(history)
            self._rebuild_attachment_index_from_messages(self.messages.items)
            self._sync_current_chat_unread_flags()

            if len(history) < history_limit:
                self.messages.has_more_history = False

            self.status.set(f"Открыт чат: {chat.name}", "success")
        except Exception as e:
            self.status.set(f"Ошибка открытия чата: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def refresh_current_chat(
        self,
        *,
        announce_status: bool = False,
        track_busy: bool = True,
        forward_limit: int = 20,
    ) -> int:
        if self.messages.chat_id is None:
            return 0

        if track_busy:
            self.is_busy = True
        try:
            if self.messages.newest_time is None:
                history = await self.session.get_history(
                    self.messages.chat_id,
                    backward=forward_limit,
                )
                if not history:
                    return 0
                if self.messages.items:
                    added = self.messages.append_items(history)
                else:
                    self.messages.replace_items(history)
                    added = len(self.messages.items)
            else:
                history = await self.session.get_history(
                    self.messages.chat_id,
                    from_time=self.messages.newest_time,
                    forward=forward_limit,
                    backward=0,
                )
                added = self.messages.append_items(history)

            if added > 0:
                self._rebuild_attachment_index_from_messages(self.messages.items)
                self._sync_current_chat_unread_flags()
                if announce_status:
                    self.status.set(f"Новых сообщений: {added}", "info")
            else:
                self._sync_current_chat_unread_flags()
            return added
        finally:
            if track_busy:
                self.is_busy = False

    # -------------------------
    # messages / history
    # -------------------------

    def get_selected_message(self) -> MessageInfo | None:
        return self.messages.selected

    def find_message_index(self, message_id: int) -> int | None:
        for i, msg in enumerate(self.messages.items):
            if msg.message_id == message_id:
                return i
        return None

    async def jump_to_reply_source(self, *, batch_size: int = 50, max_batches: int = 20) -> bool:
        selected = self.messages.selected
        if selected is None:
            self.status.set("Нет выбранного сообщения", "warning")
            return False

        if selected.reply_to is None:
            self.status.set("У сообщения нет ссылки на ответ", "warning")
            return False

        target_id = selected.reply_to
        try:
            jumped = await self.jump_to_message(
                target_id,
                batch_size=batch_size,
                max_batches=max_batches,
            )
        except Exception as e:
            self.status.set(f"Ошибка перехода к ответу: {e!r}", "error")
            raise

        if jumped:
            self.status.set(f"Переход к сообщению {target_id}", "info")
            return True

        self.status.set(f"Сообщение ответа {target_id} не найдено", "warning")
        return False

    async def load_older_messages(self, count: int = 20) -> int:
        if self.messages.chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return 0

        if not self.messages.has_more_history:
            self.status.set("Старых сообщений больше нет", "info")
            return 0

        self.is_busy = True
        self.messages.is_loading = True
        try:
            history = await self.session.get_history(
                self.messages.chat_id,
                from_time=self.messages.oldest_time,
                backward=count,
            )

            if not history:
                self.messages.has_more_history = False
                self.status.set("Старых сообщений больше нет", "info")
                return 0

            added = self.messages.prepend_items(history)
            self._rebuild_attachment_index_from_messages(self.messages.items)
            self._sync_current_chat_unread_flags()

            if added == 0:
                self.messages.has_more_history = False
                self.status.set("Старых сообщений больше нет", "info")
                return 0

            if len(history) < count:
                self.messages.has_more_history = False

            self.status.set(f"Подгружено сообщений: {added}", "info")
            return added
        except Exception as e:
            self.status.set(f"Ошибка подгрузки истории: {e!r}", "error")
            raise
        finally:
            self.is_busy = False
            self.messages.is_loading = False

    def move_dialog_selection_up(self, step: int = 1) -> None:
        self.dialogs.move_up(step)

    def move_dialog_selection_down(self, step: int = 1) -> None:
        self.dialogs.move_down(step)

    async def move_message_selection_up(self, step: int = 1, autoload_older: bool = True) -> None:
        was_at_top = self.messages.is_at_top()
        self.messages.move_up(step)

        if autoload_older and was_at_top and self.messages.is_at_top():
            await self.load_older_messages()

    def move_message_selection_down(self, step: int = 1) -> None:
        self.messages.move_down(step)

    # -------------------------
    # search
    # -------------------------

    @staticmethod
    def _normalize_search_query(query: str) -> str:
        return re.sub(r"\s+", " ", (query or "").strip())

    @staticmethod
    def _message_search_text(message: MessageInfo) -> str:
        text = (message.text or "").strip()
        if not text and message.attachments:
            text = " ".join(
                f"[{att.kind}:{att.attach_id}{att.ext or ''}]"
                if att.kind == "file"
                else f"[{att.kind}:{att.attach_id}]"
                for att in message.attachments
            )
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _build_search_preview(text: str, query: str, *, radius_before: int = 24, radius_after: int = 42) -> str:
        if not text:
            return "[empty]"

        lower_text = text.lower()
        lower_query = query.lower()
        pos = lower_text.find(lower_query)
        if pos < 0:
            return text[:80] + ("..." if len(text) > 80 else "")

        start = max(0, pos - radius_before)
        end = min(len(text), pos + len(query) + radius_after)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet or "[empty]"

    def _search_in_messages(self, messages: list[MessageInfo], query: str) -> tuple[list[SearchResult], set[int]]:
        results: list[SearchResult] = []
        scanned_ids: set[int] = set()
        query_lower = query.lower()

        for message in reversed(messages):
            if message.message_id in self.search.scanned_message_ids or message.message_id in scanned_ids:
                continue
            scanned_ids.add(message.message_id)

            haystack = self._message_search_text(message)
            if not haystack or query_lower not in haystack.lower():
                continue

            results.append(
                SearchResult(
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    sender_name=message.sender_name,
                    timestamp_ms=message.timestamp_ms,
                    preview_text=self._build_search_preview(haystack, query),
                )
            )

        return results, scanned_ids

    def begin_search(self) -> bool:
        if self.current_chat_id is None:
            self.status.set("Сначала открой чат для поиска", "warning")
            return False

        self.search.active = True
        self.search.input_mode = True
        self.search.chat_id = self.current_chat_id
        self.search.selected_index = 0
        self.status.clear()
        return True

    def cancel_search(self) -> None:
        if not self.search.active:
            return
        self.search.clear()
        self.status.set("Поиск закрыт", "info")

    async def execute_search(self, query: str, *, batch_size: int = 100) -> int:
        if self.current_chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return 0

        clean_query = self._normalize_search_query(query)
        if len(clean_query) < 3:
            self.status.set("Для поиска введите минимум 3 символа", "warning")
            return 0

        self.is_busy = True
        try:
            history = await self.session.get_history(
                self.current_chat_id,
                backward=batch_size,
            )
            results, scanned_ids = self._search_in_messages(history, clean_query)
            oldest_time = min(
                (msg.timestamp_ms for msg in history if msg.timestamp_ms is not None),
                default=None,
            )
            self.search.set_results(
                chat_id=self.current_chat_id,
                query=clean_query,
                results=results,
                oldest_time=oldest_time,
                has_more_history=len(history) >= batch_size,
                scanned_ids=scanned_ids,
            )
            self.status.set(f"Найдено: {len(results)}", "info")
            return len(results)
        except Exception as e:
            self.status.set(f"Ошибка поиска: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    def requires_startup_login(self) -> bool:
        return not self.session.has_credentials

    def login_prefill_phone(self) -> str:
        phone = (self.session.phone or "").strip()
        return "" if phone == "+70000000000" else phone

    def login_prefill_device_type(self) -> str:
        device_type = (self.session.device_type or "DESKTOP").strip().upper()
        return device_type if device_type in {"DESKTOP", "ANDROID", "IOS"} else "DESKTOP"

    async def request_login_code(self, phone: str, device_type: str) -> None:
        self.is_busy = True
        try:
            challenge = await self.session.request_login_code(phone, device_type)
            self.status.set(
                f"Код отправлен на {challenge.phone}. Клиент: {challenge.device_type}",
                "info",
            )
        except Exception as e:
            self.status.set(f"Ошибка запроса кода: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def complete_login(self, code: str) -> AuthResult:
        self.is_busy = True
        try:
            result = await self.session.complete_login_code(code)
            self.session_expired = False
            if callable(self.save_session_callback):
                self.save_session_callback(**self.session.build_session_payload())
            self.status.set("Вход выполнен, подключение...", "success")
            return result
        except Exception as e:
            self.status.set(f"Ошибка входа: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def list_active_sessions(self) -> list[ActiveSessionInfo]:
        self.is_busy = True
        try:
            sessions = await self.session.list_active_sessions()
            self.status.set(f"Активных сессий: {len(sessions)}", "info")
            return sessions
        except Exception as e:
            self.status.set(f"Ошибка списка сессий: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def close_other_sessions(self) -> list[ActiveSessionInfo]:
        self.is_busy = True
        try:
            await self.session.close_other_sessions()
            sessions = await self.session.list_active_sessions()
            self.status.set("Другие сессии закрыты", "success")
            return sessions
        except Exception as e:
            self.status.set(f"Ошибка закрытия других сессий: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def logout_account(self) -> None:
        self.is_busy = True
        logout_error: Exception | None = None
        try:
            await self.stop_polling()
            self.search.clear()
            try:
                await self.session.logout_current_session()
            except Exception as e:
                logout_error = e

            self.dialogs.clear()
            self.messages.clear()
            self.attachment_index.clear()
            self.me_name = ""
            self.me_id = None
            self.is_connected = False
            self.session_expired = False
            self.session.clear_account_identity()
            if callable(self.clear_account_callback):
                self.clear_account_callback()

            if logout_error is None:
                self.status.set("Вы вышли из аккаунта", "success")
            else:
                self.status.set(f"Локальный выход выполнен, но сервер ответил ошибкой: {logout_error!r}", "warning")
        finally:
            self.is_busy = False

    async def load_more_search_results(self, *, batch_size: int = 100) -> int:
        if not self.search.active or self.search.chat_id is None:
            self.status.set("Поиск не активен", "warning")
            return 0

        if not self.search.has_more_history:
            self.status.set("Старых сообщений для поиска больше нет", "info")
            return 0

        self.is_busy = True
        try:
            history = await self.session.get_history(
                self.search.chat_id,
                from_time=self.search.oldest_time,
                backward=batch_size,
            )
            if not history:
                self.search.has_more_history = False
                self.status.set("Старых сообщений для поиска больше нет", "info")
                return 0

            results, scanned_ids = self._search_in_messages(history, self.search.query)
            oldest_time = min(
                (msg.timestamp_ms for msg in history if msg.timestamp_ms is not None),
                default=self.search.oldest_time,
            )
            self.search.append_results(
                results,
                oldest_time=oldest_time,
                has_more_history=len(history) >= batch_size,
                scanned_ids=scanned_ids,
            )
            self.status.set(f"Найдено: {len(self.search.results)}", "info")
            return len(results)
        except Exception as e:
            self.status.set(f"Ошибка поиска: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def jump_to_message(self, target_id: int, *, batch_size: int = 100, max_batches: int = 20) -> bool:
        existing_index = self.find_message_index(target_id)
        if existing_index is not None:
            self.messages.selected_index = existing_index
            self.messages.scroll_to_bottom_on_refresh = False
            self.messages.center_selection_on_refresh = True
            return True

        if self.messages.chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return False

        self.is_busy = True
        self.messages.is_loading = True
        try:
            for _ in range(max_batches):
                if not self.messages.has_more_history:
                    break

                history = await self.session.get_history(
                    self.messages.chat_id,
                    from_time=self.messages.oldest_time,
                    backward=batch_size,
                )

                if not history:
                    self.messages.has_more_history = False
                    break

                added = self.messages.prepend_items(history)
                self._rebuild_attachment_index_from_messages(self.messages.items)
                self._sync_current_chat_unread_flags()

                if len(history) < batch_size or added == 0:
                    self.messages.has_more_history = False

                existing_index = self.find_message_index(target_id)
                if existing_index is not None:
                    self.messages.selected_index = existing_index
                    self.messages.scroll_to_bottom_on_refresh = False
                    self.messages.center_selection_on_refresh = True
                    return True

            return False
        finally:
            self.is_busy = False
            self.messages.is_loading = False

    async def activate_search_selection(self) -> str | None:
        if not self.search.active:
            return None

        if self.search.is_load_more_selected:
            await self.load_more_search_results()
            return "load_more"

        selected = self.search.selected_result
        if selected is None:
            self.status.set("Ничего не найдено", "info")
            return None

        jumped = await self.jump_to_message(selected.message_id)
        if jumped:
            self.status.set(f"Переход к сообщению {selected.message_id}", "info")
            return "jump"

        self.status.set(f"Сообщение {selected.message_id} не найдено", "warning")
        return None

    # -------------------------
    # send / reply
    # -------------------------

    @staticmethod
    def _normalize_local_path(path: str) -> str:
        clean = (path or "").strip()
        if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in {"'", '"'}:
            clean = clean[1:-1].strip()
        if not clean:
            return ""

        path_obj = Path(clean).expanduser()
        if not path_obj.is_absolute():
            path_obj = Path.cwd() / path_obj
        return str(path_obj)

    async def _mark_current_chat_read_after_send(self, message_id: int) -> int | None:
        chat_id = self.messages.chat_id
        if chat_id is None:
            return None

        try:
            await self.session.mark_read(chat_id, message_id)
        except Exception as e:
            self.status.set(
                f"Сообщение отправлено, но отметка прочитанного не выполнена: {e!r}",
                "warning",
            )
            return None

        self.dialogs.set_unread_count(chat_id, 0)
        self._sync_current_chat_unread_flags()
        return 0

    async def send_text(self, text: str, *, reply_to: int | None = None) -> MessageInfo | None:
        if self.messages.chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return None

        clean = text.strip()
        if not clean:
            self.status.set("Пустое сообщение", "warning")
            return None

        self.is_busy = True
        try:
            msg = await self.session.send_text(
                self.messages.chat_id,
                clean,
                reply_to=reply_to,
            )
            if msg:
                self.messages.add_sent_message(msg)
                self._rebuild_attachment_index_from_messages(self.messages.items)
                unread = await self._mark_current_chat_read_after_send(msg.message_id)
                if unread is None:
                    pass
                elif unread > 0:
                    self.status.set(
                        f"Сообщение отправлено. Отмечено прочитанным до вашего сообщения, осталось непрочитанных: {unread}",
                        "success",
                    )
                else:
                    self.status.set("Сообщение отправлено. Чат отмечен прочитанным", "success")
            return msg
        except Exception as e:
            self.status.set(f"Ошибка отправки: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def send_attachment(
        self,
        path: str,
        *,
        kind: str,
        text: str = "",
        reply_to: int | None = None,
    ) -> MessageInfo | None:
        if kind == "photo":
            return await self.send_photo(path, text=text, reply_to=reply_to)
        if kind == "file":
            return await self.send_file(path, text=text, reply_to=reply_to)
        raise ValueError(f"Unknown attachment kind: {kind!r}")

    async def send_photo(self, path: str, *, text: str = "", reply_to: int | None = None) -> MessageInfo | None:
        if self.messages.chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return None

        self.is_busy = True
        try:
            clean_path = self._normalize_local_path(path)
            if not clean_path:
                self.status.set("Не указан путь к фото", "warning")
                return None
            msg = await self.session.send_photo(
                self.messages.chat_id,
                clean_path,
                text=text.strip(),
                reply_to=reply_to,
            )
            if msg:
                self.messages.add_sent_message(msg)
                self._rebuild_attachment_index_from_messages(self.messages.items)
                self.status.set("Фото отправлено", "success")
            return msg
        except Exception as e:
            self.status.set(f"Ошибка отправки фото: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def send_file(self, path: str, *, text: str = "", reply_to: int | None = None) -> MessageInfo | None:
        if self.messages.chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return None

        self.is_busy = True
        try:
            clean_path = self._normalize_local_path(path)
            if not clean_path:
                self.status.set("Не указан путь к файлу", "warning")
                return None
            msg = await self.session.send_file(
                self.messages.chat_id,
                clean_path,
                text=text.strip(),
                reply_to=reply_to,
            )
            if msg:
                self.messages.add_sent_message(msg)
                self._rebuild_attachment_index_from_messages(self.messages.items)
                self.status.set("Файл отправлен", "success")
            return msg
        except Exception as e:
            self.status.set(f"Ошибка отправки файла: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def mark_selected_as_read(self) -> int | None:
        if self.messages.chat_id is None:
            self.status.set("Чат не открыт", "warning")
            return None

        selected = self.messages.selected
        if selected is None:
            self.status.set("Нет выбранного сообщения", "warning")
            return None

        self.is_busy = True
        try:
            unread = await self.session.mark_read(
                self.messages.chat_id,
                selected.message_id,
            )
            self.dialogs.set_unread_count(self.messages.chat_id, unread)
            self._sync_current_chat_unread_flags()
            if unread > 0:
                self.status.set(
                    f"Прочитано до сообщения {selected.message_id}. Осталось непрочитанных: {unread}",
                    "success",
                )
            else:
                self.status.set(f"Прочитано до сообщения {selected.message_id}", "success")
            return unread
        except Exception as e:
            self.status.set(f"Ошибка отметки прочитанного: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    def start_reply_to_selected(self) -> None:
        selected = self.messages.selected
        if selected is None:
            self.status.set("Нет выбранного сообщения", "warning")
            return
        self.compose.reply_to_message_id = selected.message_id
        self.status.set(f"Ответ на сообщение {selected.message_id}", "info")

    def cancel_reply(self) -> None:
        self.compose.reply_to_message_id = None
        self.status.set("Ответ отменён", "info")

    # -------------------------
    # attachments
    # -------------------------

    def _rebuild_attachment_index_from_messages(self, messages: list[MessageInfo]) -> None:
        self.attachment_index.clear()
        for msg in messages:
            for att in msg.attachments:
                self.attachment_index[str(att.attach_id)] = att

    def find_attachment(self, attach_id: str | int) -> AttachmentInfo | None:
        return self.attachment_index.get(str(attach_id))

    def selected_message_attachments(self) -> list[AttachmentInfo]:
        selected = self.messages.selected
        if selected is None:
            return []
        return selected.attachments

    @staticmethod
    def is_downloadable_attachment(attachment: AttachmentInfo | None) -> bool:
        return attachment is not None and attachment.kind in {"photo", "file"}

    def get_first_downloadable_attachment(self) -> AttachmentInfo | None:
        for attachment in self.selected_message_attachments():
            if self.is_downloadable_attachment(attachment):
                return attachment
        return None

    def get_downloadable_attachments(self) -> list[AttachmentInfo]:
        return [
            attachment
            for attachment in self.selected_message_attachments()
            if self.is_downloadable_attachment(attachment)
        ]

    def build_download_filename(self, attachment: AttachmentInfo) -> str:
        if attachment.name:
            return attachment.name
        if attachment.kind == "photo":
            return f"photo_{attachment.attach_id}.jpg"
        if attachment.kind == "file":
            suffix = attachment.ext or ""
            return f"file_{attachment.attach_id}{suffix}"
        return f"attachment_{attachment.attach_id}"

    def build_download_directory(self) -> str:
        base_dir = Path(self.session.download_dir)
        if not base_dir.is_absolute():
            base_dir = Path.cwd() / base_dir
        return str(base_dir)

    def build_download_path(self, attachment: AttachmentInfo) -> str:
        return str(Path(self.build_download_directory()) / self.build_download_filename(attachment))

    def normalize_download_path(self, save_path: str | None) -> str | None:
        clean = (save_path or "").strip()
        if not clean:
            return None
        return self._normalize_local_path(clean)

    async def download_attachment(self, attach_id: str | int, save_path: str | None = None) -> str:
        attachment = self.find_attachment(attach_id)
        if attachment is None:
            raise KeyError(f"Attachment {attach_id} not found")

        self.is_busy = True
        try:
            path = await self.session.download_attachment(
                attachment,
                save_path=self.normalize_download_path(save_path),
            )
            self.status.set(f"Скачано: {path}", "success")
            return path
        except Exception as e:
            self.status.set(f"Ошибка скачивания: {e!r}", "error")
            raise
        finally:
            self.is_busy = False

    async def download_attachments(
        self,
        attach_ids: list[str | int],
        *,
        save_dir: str | None = None,
    ) -> list[str]:
        if not attach_ids:
            self.status.set("Нет вложений для скачивания", "warning")
            return []

        clean_dir = self.normalize_download_path(save_dir) if save_dir else None
        if save_dir is not None and clean_dir is None:
            self.status.set("Не указана папка для скачивания", "warning")
            return []

        attachments: list[AttachmentInfo] = []
        for attach_id in attach_ids:
            attachment = self.find_attachment(attach_id)
            if attachment is not None and self.is_downloadable_attachment(attachment):
                attachments.append(attachment)

        if not attachments:
            self.status.set("Нет скачиваемых вложений", "warning")
            return []

        saved_paths: list[str] = []
        errors: list[str] = []
        self.is_busy = True
        try:
            for attachment in attachments:
                target_path = None
                if clean_dir is not None:
                    target_path = str(Path(clean_dir) / self.build_download_filename(attachment))
                try:
                    saved = await self.session.download_attachment(
                        attachment,
                        save_path=target_path,
                    )
                    saved_paths.append(saved)
                except Exception as e:
                    errors.append(f"{attachment.attach_id}: {e!r}")

            if errors:
                if saved_paths:
                    self.status.set(
                        f"Скачано {len(saved_paths)} из {len(attachments)}. Ошибки: {len(errors)}",
                        "warning",
                    )
                else:
                    self.status.set(f"Ошибка пакетного скачивания: {errors[0]}", "error")
                raise RuntimeError("; ".join(errors))

            if clean_dir is not None:
                self.status.set(f"Скачано файлов: {len(saved_paths)} в {clean_dir}", "success")
            else:
                self.status.set(f"Скачано файлов: {len(saved_paths)}", "success")
            return saved_paths
        finally:
            self.is_busy = False

    # -------------------------
    # convenience / ui helpers
    # -------------------------

    @property
    def current_chat_id(self) -> int | None:
        return self.messages.chat_id

    @property
    def current_chat_name(self) -> str | None:
        return self.messages.chat_name

    @property
    def current_message_count(self) -> int:
        return len(self.messages.items)

    @property
    def current_dialog_count(self) -> int:
        return len(self.dialogs.items)

    def current_function_hints(self) -> list[str]:
        """
        Подсказки для нижней функциональной строки.
        Пока без жесткой привязки к TUI-библиотеке.
        """
        hints = ["F1 Help", "F2 Refresh", "F9 Setup", "F10 Quit"]

        if self.current_chat_id is not None:
            hints.extend(["F3 Send", "F4 Reply", "F6 Upload"])

        if self.messages.selected is not None:
            hints.append("F7 Read")

        if self.get_first_downloadable_attachment() is not None:
            hints.append("F5 Download")

        return hints

# tui.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import re
from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, ListItem, ListView, Static, Input, Button

from .session import (
    MaxSession,
    ChatInfo,
    MessageInfo,
    SessionCredentialsMissingError,
    SessionExpiredError,
)
from .state import AppState, SearchResult


# =========================
# HELPERS
# =========================

def short_text(text: str | None, limit: int = 60) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def describe_attachment(kind: str, attach_id: str, ext: str | None = None, name: str | None = None) -> str:
    if kind == "photo":
        label = f"[photo:{attach_id}]"
    elif kind == "file":
        suffix = ext or ""
        label = f"[file:{attach_id}{suffix}]"
    else:
        label = f"[attach:{attach_id}]"

    if name:
        return f"{label} {name}"
    return label


def format_unread_count(count: int) -> str:
    if count <= 0:
        return ""
    if count > 99:
        return "99+"
    return str(count)


def format_chat_title_lines(chat: ChatInfo, *, max_inline: int) -> str:
    prefix = ">" if chat.is_group else "#"

    if chat.first_name and chat.last_name:
        inline = f"{prefix}{chat.first_name} {chat.last_name} [dim]({chat.chat_id})[/]"
        if len(f"{chat.first_name} {chat.last_name}") <= max_inline:
            return inline
        return f"{prefix}{chat.first_name} [dim]({chat.chat_id})[/]\n{chat.last_name}"
    return f"{prefix}{short_text(chat.name, max_inline)} [dim]({chat.chat_id})[/]"


def format_search_time(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return "{??/??/????-??:??}"
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("{%d/%m/%Y-%H:%M}")


def highlight_search_text(text: str, query: str) -> str:
    if not query:
        return f"[white]{escape(text)}[/]"

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    parts: list[str] = []
    pos = 0

    for match in pattern.finditer(text):
        if match.start() > pos:
            parts.append(f"[white]{escape(text[pos:match.start()])}[/]")
        parts.append(f"[green]{escape(match.group(0))}[/]")
        pos = match.end()

    if pos < len(text):
        parts.append(f"[white]{escape(text[pos:])}[/]")

    return "".join(parts) or f"[white]{escape(text)}[/]"


def build_message_body_lines(message: MessageInfo) -> list[str]:
    body_lines: list[str] = []

    if message.text:
        raw_lines = message.text.splitlines()
        if raw_lines:
            body_lines.extend(line.rstrip() for line in raw_lines)

    attachment_parts: list[str] = []
    for att in message.attachments:
        if att.kind == "photo":
            attachment_parts.append(f"[photo:{att.attach_id}]")
        elif att.kind == "file":
            ext = att.ext or ""
            attachment_parts.append(f"[file:{att.attach_id}{ext}]")
        elif att.kind == "video":
            attachment_parts.append(f"[video:{att.attach_id}]")
        else:
            attachment_parts.append(f"[attach:{att.attach_id}]")

    if attachment_parts:
        attachment_text = " ".join(attachment_parts)
        if body_lines:
            if body_lines[-1].strip():
                body_lines[-1] = f"{body_lines[-1]} {attachment_text}".strip()
            else:
                body_lines[-1] = attachment_text
        else:
            body_lines.append(attachment_text)

    if not body_lines:
        body_lines = ["[empty]"]

    return body_lines


def wrap_text_lines(lines_in: list[str], width: int) -> list[str]:
    wrapped: list[str] = []

    for body_line in lines_in:
        current = body_line or ""
        if not current:
            wrapped.append("")
            continue

        while len(current) > width:
            split_at = current.rfind(" ", 0, width)
            if split_at <= 0:
                split_at = width
            wrapped.append(current[:split_at].rstrip())
            current = current[split_at:].lstrip()

        wrapped.append(current)

    return wrapped


def wrap_message_lines(message: MessageInfo, width_hint: int = 72) -> list[str]:
    """
    Пока простой форматтер для TUI.
    Потом можно заменить на formatting.py.
    """
    marker = "[red]│[/]" if message.is_unread else " "
    body_lines = build_message_body_lines(message)

    if message.reply_to is not None and message.reply_sender_name:
        time_prefix = f"{message.timestamp_str} "
        reply_prefix = f"{time_prefix}╚═> "
        reply_preview = short_text(message.reply_preview_text or "[empty]", limit=42)
        reply_line = f"{message.reply_sender_name} - {reply_preview}"

        body_prefix = f"{' ' * len(time_prefix)}{message.sender_name} - "
        max_reply_width = max(20, width_hint - len(reply_prefix))
        max_body_width = max(20, width_hint - len(body_prefix))
        wrapped_body = wrap_text_lines(body_lines, max_body_width)

        out = [f"{marker}{reply_prefix}{short_text(reply_line, limit=max_reply_width)}"]
        out.append(f"{marker}{body_prefix}{wrapped_body[0]}")
        body_indent = f"{marker}{' ' * len(body_prefix)}"
        for line in wrapped_body[1:]:
            out.append(body_indent + line)
        return out

    prefix = f"{message.timestamp_str} {message.sender_name} - "
    max_body = max(20, width_hint - len(prefix))
    lines = wrap_text_lines(body_lines, max_body)

    if not lines:
        return [f"{marker}{prefix}"]

    out = [f"{marker}{prefix}{lines[0]}"]
    indent = f"{marker}{' ' * len(prefix)}"
    for line in lines[1:]:
        out.append(indent + line)
    return out


# =========================
# UI WIDGETS
# =========================

class ChatListItem(ListItem):
    def __init__(self, chat: ChatInfo) -> None:
        self.chat = chat
        label = format_chat_title_lines(chat, max_inline=18)
        unread = format_unread_count(chat.unread_count)
        super().__init__(
            Horizontal(
                Static(label, classes="chat-main"),
                Static(unread, classes="chat-unread"),
                classes="chat-row",
            )
        )


class SearchResultListItem(ListItem):
    def __init__(self, result: SearchResult, query: str) -> None:
        self.result = result
        header = f"[dim]{format_search_time(result.timestamp_ms)} {escape(result.sender_name)}[/]"
        preview = highlight_search_text(result.preview_text, query)
        super().__init__(Static(f"{header}\n{preview}"))


class SearchLoadMoreListItem(ListItem):
    def __init__(self) -> None:
        super().__init__(Static("[yellow]Загрузить еще 100 сообщений из истории?[/]"))


class SearchEmptyListItem(ListItem):
    def __init__(self) -> None:
        super().__init__(Static("[white]Ничего не найдено[/]"))


class SearchHintListItem(ListItem):
    def __init__(self) -> None:
        super().__init__(Static("[white]Введите минимум 3 символа и нажмите Enter[/]"))


class MessageListItem(ListItem):
    def __init__(self, message: MessageInfo) -> None:
        self.message = message
        text = "\n".join(wrap_message_lines(message))
        super().__init__(Static(text))


class StatusBar(Static):
    def set_status(self, text: str) -> None:
        self.update(text or "")


class CenterMessage(Static):
    DEFAULT_CSS = """
    CenterMessage {
        width: 100%;
        height: 100%;
        content-align: center middle;
    }
    """


# =========================
# OPTIONAL SIMPLE MODAL
# =========================

class InfoModal(ModalScreen[None]):
    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[b]{self._title}[/]\n\n{self._text}", id="info_modal_body"),
            id="info_modal",
        )

    def on_key(self, event) -> None:
        self.dismiss()

    DEFAULT_CSS = """
    #info_modal {
        width: 60;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        content-align: center middle;
        margin: 8 20;
    }
    #info_modal_body {
        width: 100%;
    }
    """

class ComposeMessageModal(ModalScreen[str | None]):
    def __init__(self, title: str = "Новое сообщение", initial_text: str = "") -> None:
        super().__init__()
        self._title = title
        self._initial_text = initial_text

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[b]{self._title}[/]", id="compose_title"),
            Input(value=self._initial_text, placeholder="Введите сообщение...", id="compose_input"),
            Static("[Enter - отправить]   [Esc - отмена]", id="compose_hint"),
            id="compose_modal",
        )

    def on_mount(self) -> None:
        self.query_one("#compose_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)

    DEFAULT_CSS = """
    #compose_modal {
        width: 70;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 8 10;
    }

    #compose_title {
        margin-bottom: 1;
    }

    #compose_input {
        margin-bottom: 1;
    }

    #compose_hint {
        color: $text-muted;
    }
    """


@dataclass(slots=True)
class UploadRequest:
    path: str
    text: str = ""
    kind: str = "file"


@dataclass(slots=True)
class DownloadRequest:
    attach_ids: list[str]
    path: str = ""
    is_directory: bool = False


@dataclass(slots=True)
class LoginRequest:
    phone: str
    device_type: str


class StartupLoginModal(ModalScreen[LoginRequest]):
    BINDINGS = [Binding("f10", "quit_app", "Quit")]
    DEVICE_TYPES = ("DESKTOP", "ANDROID", "IOS")

    def __init__(
        self,
        *,
        message: str = "",
        initial_phone: str = "",
        initial_device_type: str = "DESKTOP",
    ) -> None:
        super().__init__()
        self._message = message
        self._initial_phone = initial_phone
        normalized_type = (initial_device_type or "DESKTOP").strip().upper()
        self._device_index = self.DEVICE_TYPES.index(normalized_type) if normalized_type in self.DEVICE_TYPES else 0

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Вход в MAX[/]", id="login_title"),
            Static(self._message or "Введите номер телефона и выберите тип клиента.", id="login_text"),
            Static("Номер телефона:", classes="login_label"),
            Input(value=self._initial_phone, placeholder="+79991234567", id="login_phone"),
            Static("Тип клиента (DESKTOP / ANDROID / IOS):", classes="login_label"),
            Static("", id="login_device_type"),
            Static("[Tab - поля]   [Enter - далее]   [F10 - выход]", id="login_hint"),
            id="login_modal",
        )

    def on_mount(self) -> None:
        self._refresh_device_type_label()
        labels = [widget for widget in self.query(".login_label") if isinstance(widget, Static)]
        if len(labels) > 1:
            labels[1].update("Тип клиента:")
        self.query_one("#login_text", Static).update(self._message or "Введите номер телефона и выберите тип клиента стрелками.")
        self.query_one("#login_hint", Static).update("[Left/Right - клиент]   [Enter - далее]   [F10 - выход]")
        self.query_one("#login_phone", Input).focus()

    @property
    def _selected_device_type(self) -> str:
        return self.DEVICE_TYPES[self._device_index]

    def _refresh_device_type_label(self) -> None:
        parts: list[str] = []
        for index, device_type in enumerate(self.DEVICE_TYPES):
            if index == self._device_index:
                parts.append(f"[b yellow]> {device_type} <[/]")
            else:
                parts.append(device_type)
        self.query_one("#login_device_type", Static).update("   ".join(parts))

    def _submit(self) -> None:
        phone = self.query_one("#login_phone", Input).value.strip()
        self.dismiss(LoginRequest(phone=phone, device_type=self._selected_device_type))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            return
        if event.key == "left":
            event.stop()
            self._device_index = (self._device_index - 1) % len(self.DEVICE_TYPES)
            self._refresh_device_type_label()
            return
        if event.key == "right":
            event.stop()
            self._device_index = (self._device_index + 1) % len(self.DEVICE_TYPES)
            self._refresh_device_type_label()
            return
        if event.key == "enter":
            event.stop()
            self._submit()

    def action_quit_app(self) -> None:
        self.app.exit()

    DEFAULT_CSS = """
    #login_modal {
        width: 68;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 8 10;
    }

    #login_title, #login_text, .login_label {
        margin-bottom: 1;
    }

    #login_phone, #login_device_type {
        margin-bottom: 1;
    }

    #login_hint {
        color: $text-muted;
    }
    """


class SmsCodeModal(ModalScreen[str]):
    BINDINGS = [Binding("f10", "quit_app", "Quit")]

    def __init__(self, *, message: str = "") -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Код из SMS[/]", id="sms_title"),
            Static(self._message or "Введите код из SMS.", id="sms_text"),
            Static("Код:", classes="sms_label"),
            Input(placeholder="123456", id="sms_code"),
            Static("[Enter - вход]   [F10 - выход]", id="sms_hint"),
            id="sms_modal",
        )

    def on_mount(self) -> None:
        self.query_one("#sms_code", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(self.query_one("#sms_code", Input).value.strip())

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()

    def action_quit_app(self) -> None:
        self.app.exit()

    DEFAULT_CSS = """
    #sms_modal {
        width: 60;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 10 12;
    }

    #sms_title, #sms_text, .sms_label {
        margin-bottom: 1;
    }

    #sms_code {
        margin-bottom: 1;
    }

    #sms_hint {
        color: $text-muted;
    }
    """


class UploadModal(ModalScreen[UploadRequest | None]):
    def __init__(self, title: str = "Отправка вложения", initial_kind: str = "photo") -> None:
        super().__init__()
        self._title = title
        self._kind = initial_kind if initial_kind in {"photo", "file"} else "photo"

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[b]{self._title}[/]", id="upload_title"),
            Static("", id="upload_kind"),
            Input(placeholder="Путь к файлу...", id="upload_path"),
            Input(placeholder="Текст к сообщению (необязательно)...", id="upload_text"),
            Static(
                "[F4 - фото]   [F5 - файл]   [Tab - поля]   [Enter - отправить]   [Esc - отмена]",
                id="upload_hint",
            ),
            id="upload_modal",
        )

    def on_mount(self) -> None:
        self._refresh_kind_label()
        self.query_one("#upload_path", Input).focus()

    def _refresh_kind_label(self) -> None:
        label = (
            "Режим: [b yellow]> Фото <[/]   Файл"
            if self._kind == "photo"
            else "Режим: Фото   [b yellow]> Файл <[/]"
        )
        self.query_one("#upload_kind", Static).update(label)

    def _set_kind(self, kind: str) -> None:
        if kind not in {"photo", "file"}:
            return
        self._kind = kind
        self._refresh_kind_label()

    def _build_result(self) -> UploadRequest:
        path = self.query_one("#upload_path", Input).value.strip()
        text = self.query_one("#upload_text", Input).value
        return UploadRequest(path=path, text=text, kind=self._kind)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(self._build_result())

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
            return

        if event.key == "f4":
            event.stop()
            self._set_kind("photo")
            return

        if event.key == "f5":
            event.stop()
            self._set_kind("file")
            return

    DEFAULT_CSS = """
    #upload_modal {
        width: 76;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 7 8;
    }

    #upload_title {
        margin-bottom: 1;
    }

    #upload_kind {
        margin-bottom: 1;
        color: $text;
    }

    #upload_path, #upload_text {
        margin-bottom: 1;
    }

    #upload_hint {
        color: $text-muted;
    }
    """


class DownloadModal(ModalScreen[DownloadRequest | None]):
    def __init__(
        self,
        title: str,
        attachment_text: str,
        initial_path: str = "",
        attach_ids: list[str] | None = None,
        is_directory: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._attachment_text = attachment_text
        self._initial_path = initial_path
        self._attach_ids = list(attach_ids or [])
        self._is_directory = is_directory

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[b]{self._title}[/]", id="download_title"),
            Static(self._attachment_text, id="download_attachment"),
            Static(
                "Папка назначения:" if self._is_directory else "Путь сохранения:",
                id="download_target_label",
            ),
            Input(
                value=self._initial_path,
                placeholder="Куда сохранить..." if not self._is_directory else "Куда сохранить все файлы...",
                id="download_path",
            ),
            Static("[Enter - скачать]   [Esc - отмена]", id="download_hint"),
            id="download_modal",
        )

    def on_mount(self) -> None:
        self.query_one("#download_path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(
            DownloadRequest(
                attach_ids=list(self._attach_ids),
                path=event.value.strip(),
                is_directory=self._is_directory,
            )
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    DEFAULT_CSS = """
    #download_modal {
        width: 76;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 8 8;
    }

    #download_title {
        margin-bottom: 1;
    }

    #download_attachment {
        margin-bottom: 1;
    }

    #download_target_label {
        margin-bottom: 1;
    }

    #download_path {
        margin-bottom: 1;
    }

    #download_hint {
        color: $text-muted;
    }
    """


class ConfirmModal(ModalScreen[bool | None]):
    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[b]{self._title}[/]", id="confirm_title"),
            Static(self._text, id="confirm_text"),
            Horizontal(
                Button("Да", id="confirm_yes"),
                Button("Нет", id="confirm_no"),
                id="confirm_buttons",
            ),
            Static("[Enter - выбрать]   [Esc - отмена]", id="confirm_hint"),
            id="confirm_modal",
        )

    def on_mount(self) -> None:
        self.query_one("#confirm_no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm_yes")

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
        elif event.key.lower() in {"y", "д"}:
            event.stop()
            self.dismiss(True)
        elif event.key.lower() in {"n", "т"}:
            event.stop()
            self.dismiss(False)

    DEFAULT_CSS = """
    #confirm_modal {
        width: 56;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 10 12;
    }

    #confirm_title, #confirm_text {
        margin-bottom: 1;
    }

    #confirm_buttons {
        height: auto;
        margin-bottom: 1;
    }

    #confirm_yes, #confirm_no {
        margin-right: 1;
        min-width: 18;
    }

    #confirm_hint {
        color: $text-muted;
    }
    """


class SettingsModal(ModalScreen[str | None]):
    def __init__(self, account_name: str, account_id: int | None) -> None:
        super().__init__()
        self._account_name = account_name or "Неизвестно"
        self._account_id = account_id

    def compose(self) -> ComposeResult:
        account_id = self._account_id if self._account_id is not None else "?"
        yield Container(
            Static("[b]Настройки[/]", id="settings_title"),
            Static(f"Аккаунт: {self._account_name}", id="settings_name"),
            Static(f"ID: {account_id}", id="settings_id"),
            Static("Системные настройки появятся позже.", id="settings_system"),
            Horizontal(
                Button("Выйти из аккаунта", id="settings_logout"),
                Button("Закрыть", id="settings_close"),
                id="settings_buttons",
            ),
            Static("[Tab - кнопки]   [Enter - выбрать]   [Esc - закрыть]", id="settings_hint"),
            id="settings_modal",
        )

    def on_mount(self) -> None:
        self.query_one("#settings_title", Static).update("[b]Настройки[/]")
        self.query_one("#settings_system", Static).update("Управление аккаунтом и сессиями.")
        self.query_one("#settings_logout", Button).label = "Текущие сессии"
        self.query_one("#settings_hint", Static).update("[Tab - кнопки]   [Enter - выбрать]   [Esc - закрыть]")
        self.query_one("#settings_close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "settings_logout":
            self.dismiss("sessions")
            return
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    DEFAULT_CSS = """
    #settings_modal {
        width: 64;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 8 12;
    }

    #settings_title, #settings_name, #settings_id, #settings_system {
        margin-bottom: 1;
    }

    #settings_buttons {
        height: auto;
        margin-bottom: 1;
    }

    #settings_logout, #settings_close {
        margin-right: 1;
        min-width: 20;
    }

    #settings_hint {
        color: $text-muted;
    }
    """


class SessionsModal(ModalScreen[str | None]):
    def __init__(self, sessions_text: str) -> None:
        super().__init__()
        self._sessions_text = sessions_text

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Сессии MAX[/]", id="sessions_title"),
            Static(self._sessions_text, id="sessions_body"),
            Horizontal(
                Button("Закрыть все кроме текущей", id="sessions_close_others"),
                Button("Выйти из аккаунта", id="sessions_logout"),
                Button("Назад", id="sessions_back"),
                id="sessions_buttons",
            ),
            Static("[Tab - кнопки]   [Enter - выбрать]   [Esc - назад]", id="sessions_hint"),
            id="sessions_modal",
        )

    def on_mount(self) -> None:
        self.query_one("#sessions_back", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "sessions_close_others":
            self.dismiss("close_others")
            return
        if event.button.id == "sessions_logout":
            self.dismiss("logout")
            return
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    DEFAULT_CSS = """
    #sessions_modal {
        width: 86;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
        margin: 6 8;
    }

    #sessions_title, #sessions_body {
        margin-bottom: 1;
    }

    #sessions_body {
        max-height: 18;
    }

    #sessions_buttons {
        height: auto;
        margin-bottom: 1;
    }

    #sessions_close_others, #sessions_logout, #sessions_back {
        margin-right: 1;
        min-width: 22;
    }

    #sessions_hint {
        color: $text-muted;
    }
    """

# =========================
# MAIN APP
# =========================

class MaxCommanderApp(App):
    TITLE = "Max Commander"
    SUB_TITLE = "TUI client for MAX"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        height: 1fr;
    }

    #left_panel {
        width: 34;
        border: heavy #5f87ff;
    }

    #right_panel {
        width: 1fr;
        border: heavy #5f87ff;
    }

    #dialogs_header, #messages_header {
        height: auto;
        background: #1c1c7a;
        color: white;
        content-align: center middle;
        text-style: bold;
    }

    #dialogs_list, #messages_list {
        height: 1fr;
    }

    .chat-row {
        width: 100%;
        height: auto;
    }

    .chat-main {
        width: 1fr;
        height: auto;
    }

    .chat-unread {
        width: 4;
        color: red;
        content-align: right middle;
        text-style: bold;
    }

    #status_bar {
        height: 1;
        background: #1c1c7a;
        color: white;
    }

    ListView {
        scrollbar-size: 1 1;
        border: round #808080;
    }

    ListView:focus {
        border: round #ff8700;
    }
    """

    BINDINGS = [
        Binding("tab", "toggle_focus", "Switch panel"),
        Binding("enter", "activate", "Open"),
        Binding("ctrl+f", "search", show=False),
        Binding("f1", "help", "Help"),
        Binding("f2", "refresh", "Refresh"),
        Binding("f3", "send", "Send"),
        Binding("f4", "reply", "Reply"),
        Binding("f5", "download", "Download"),
        Binding("f6", "upload", "Upload"),
        Binding("f7", "mark_read", "Read"),
        Binding("f8", "search", "Search"),
        Binding("f9", "settings", "Settings"),
        Binding("f10", "quit", "Quit"),
    ]

    focus_mode = reactive("dialogs")  # dialogs | messages

    async def _handle_compose_result(self, text: str | None, reply_to: int | None) -> None:
        if text is None:
            self.state.status.set("Отправка отменена", "info")
            self.refresh_status()
            return

        text = text.strip()
        if not text:
            self.state.status.set("Пустое сообщение", "warning")
            self.refresh_status()
            return

        try:
            await self.state.send_text(text, reply_to=reply_to)
            self.refresh_dialogs_view()
            self.refresh_messages_view()
            self.refresh_status()
            self.focus_messages()
        except Exception as e:
            self.state.status.set(f"Ошибка отправки: {e!r}", "error")
            self.refresh_status()
    
    async def _handle_upload_result(self, result: UploadRequest | None) -> None:
        if result is None:
            self.state.status.set("Отправка отменена", "info")
            self.refresh_status()
            self.focus_messages()
            return

        if self.state.current_chat_id is None:
            self.state.status.set("Сначала открой чат", "warning")
            self.refresh_status()
            self.focus_messages()
            return

        if not result.path:
            self.state.status.set("Не указан путь к файлу", "warning")
            self.refresh_status()
            self.focus_messages()
            return

        try:
            sent = await self.state.send_attachment(
                result.path,
                kind=result.kind,
                text=result.text,
            )
            if sent is not None:
                self.refresh_messages_view()
            self.refresh_status()
            self.focus_messages()
        except Exception:
            self.refresh_status()
            self.focus_messages()

    async def _handle_download_result(self, result: DownloadRequest | None) -> None:
        if result is None:
            self.state.status.set("Скачивание отменено", "info")
            self.refresh_status()
            self.focus_messages()
            return

        try:
            if result.is_directory or len(result.attach_ids) > 1:
                await self.state.download_attachments(
                    result.attach_ids,
                    save_dir=result.path,
                )
            elif result.attach_ids:
                await self.state.download_attachment(
                    result.attach_ids[0],
                    save_path=result.path,
                )
            self.refresh_status()
            self.focus_messages()
        except Exception:
            self.refresh_status()
            self.focus_messages()

    async def _handle_settings_result(self, result: str | None) -> None:
        if result != "logout":
            self.refresh_status()
            if self.focus_mode == "messages":
                self.focus_messages()
            else:
                self.focus_dialogs()
            return

        def _on_confirm(confirmed: bool | None) -> None:
            self.call_later(self._handle_logout_confirmation, confirmed)

        self.push_screen(
            ConfirmModal(
                title="Подтверждение",
                text="Вы уверенны, что хотите выйти из аккаунта?",
            ),
            callback=_on_confirm,
        )

    async def _handle_logout_confirmation(self, confirmed: bool | None) -> None:
        if confirmed:
            self.state.status.set("Выход из аккаунта пока не реализован", "warning")
        else:
            self.state.status.set("Выход из аккаунта отменён", "info")
        self.refresh_status()
        if self.focus_mode == "messages":
            self.focus_messages()
        else:
            self.focus_dialogs()

    def _restore_focus_after_modal(self) -> None:
        self.refresh_status()
        if self.focus_mode == "messages":
            self.focus_messages()
        else:
            self.focus_dialogs()

    @staticmethod
    def _pick_session_info(client: str, info: str) -> str:
        clean_info = (info or "").strip()
        if not clean_info:
            return "-"

        parts = [part.strip() for part in clean_info.split(",") if part.strip()]
        if not parts:
            return clean_info
        if len(parts) == 1:
            return parts[0]

        client_lower = (client or "").lower()

        preferred_by_client = {
            "windows": ("windows",),
            "ubuntu": ("ubuntu",),
            "fedora": ("fedora",),
            "android": ("android",),
            "ios": ("ios", "iphone", "ipad"),
            "mac": ("macos", "mac os", "os x"),
            "web": ("chrome", "firefox", "safari", "edge", "opera", "browser"),
        }

        for marker, keywords in preferred_by_client.items():
            if marker in client_lower:
                for part in parts:
                    part_lower = part.lower()
                    if any(keyword in part_lower for keyword in keywords):
                        return part

        return parts[-1]

    @staticmethod
    def _format_sessions_text(sessions) -> str:
        if not sessions:
            return "Активных сессий не найдено."

        lines: list[str] = []
        for index, session in enumerate(sessions, start=1):
            stamp = "?"
            if getattr(session, "time", 0):
                try:
                    stamp = datetime.fromtimestamp(session.time / 1000).strftime("%d/%m/%Y %H:%M")
                except Exception:
                    stamp = str(session.time)
            marker = "*" if getattr(session, "current", False) else " "
            client = getattr(session, "client", "") or "Unknown"
            info = MaxCommanderApp._pick_session_info(client, getattr(session, "info", "") or "")
            location = getattr(session, "location", "") or "-"
            lines.append(f"{marker}{index}. {client}")
            lines.append(f"    {info}")
            lines.append(f"    {location}")
            lines.append(f"    {stamp}")
        lines.append("")
        lines.append("* Текущая сессия")
        lines.append("Отдельное закрытие одной чужой сессии MAX сейчас не поддерживает.")
        return "\n".join(lines)

    def _show_sessions_modal(self, sessions) -> None:
        def _on_close(result: str | None) -> None:
            self.call_later(self._handle_sessions_result, result)

        self.push_screen(
            SessionsModal(self._format_sessions_text(sessions)),
            callback=_on_close,
        )

    async def _handle_settings_result(self, result: str | None) -> None:
        if result != "sessions":
            self._restore_focus_after_modal()
            return

        try:
            sessions = await self.state.list_active_sessions()
        except Exception:
            self.refresh_status()
            self._restore_focus_after_modal()
            return

        self.refresh_status()
        self._show_sessions_modal(sessions)

    async def _handle_sessions_result(self, result: str | None) -> None:
        if result is None:
            self._restore_focus_after_modal()
            return

        if result == "close_others":
            try:
                sessions = await self.state.close_other_sessions()
            except Exception:
                self.refresh_status()
                self._restore_focus_after_modal()
                return

            self.refresh_status()
            self._show_sessions_modal(sessions)
            return

        if result == "logout":
            def _on_confirm(confirmed: bool | None) -> None:
                self.call_later(self._handle_logout_confirmation, confirmed)

            self.push_screen(
                ConfirmModal(
                    title="Подтверждение",
                    text="Вы уверенны, что хотите выйти из аккаунта?",
                ),
                callback=_on_confirm,
            )
            return

        self._restore_focus_after_modal()

    async def _handle_logout_confirmation(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.state.status.set("Выход из аккаунта отменён", "info")
            self.refresh_status()
            try:
                sessions = await self.state.list_active_sessions()
            except Exception:
                self._restore_focus_after_modal()
                return
            self.refresh_status()
            self._show_sessions_modal(sessions)
            return

        await self.state.logout_account()
        self.refresh_dialogs_view()
        self.refresh_messages_view()
        self.refresh_status()
        self._show_startup_login(expired=False)

    def _show_startup_login(self, *, expired: bool = False, error_text: str = "") -> None:
        message = error_text.strip()
        if not message:
            if expired:
                message = "Эта сессия просрочена, войдите заново."
            else:
                message = "Для работы нужно войти в систему."

        def _on_close(result: LoginRequest) -> None:
            self.call_later(self._handle_startup_login_request, result)

        self.push_screen(
            StartupLoginModal(
                message=message,
                initial_phone=self.state.login_prefill_phone(),
                initial_device_type=self.state.login_prefill_device_type(),
            ),
            callback=_on_close,
        )

    def _show_sms_code_modal(self, *, message: str = "") -> None:
        def _on_close(result: str) -> None:
            self.call_later(self._handle_sms_code_submit, result)

        self.push_screen(
            SmsCodeModal(message=message),
            callback=_on_close,
        )

    async def _handle_startup_login_request(self, result: LoginRequest) -> None:
        try:
            await self.state.request_login_code(result.phone, result.device_type)
        except Exception:
            self.refresh_status()
            self._show_startup_login(error_text=self.state.status.text)
            return

        self.refresh_status()
        self._show_sms_code_modal(message=f"Код отправлен на {result.phone}.")

    async def _handle_sms_code_submit(self, code: str) -> None:
        try:
            await self.state.complete_login(code)
        except Exception:
            self.refresh_status()
            self._show_sms_code_modal(message=self.state.status.text or "Введите код из SMS.")
            return

        await self._start_connected_flow()

    async def _start_connected_flow(self) -> None:
        self.set_status("Подключение...")
        try:
            await self.state.connect()
            await self.state.refresh_dialogs()
        except SessionCredentialsMissingError:
            self.refresh_status()
            self._show_startup_login(expired=False)
            return
        except SessionExpiredError:
            self.refresh_status()
            self._show_startup_login(expired=True)
            return
        except Exception:
            self.refresh_status()
            self.focus_dialogs()
            return

        self.refresh_dialogs_view()
        self.refresh_messages_view()
        self.refresh_status()
        await self.state.start_polling()
        if self._poll_consumer_task is None or self._poll_consumer_task.done():
            self._poll_consumer_task = asyncio.create_task(self._poll_events_loop())
        self.focus_dialogs()

    async def _poll_events_loop(self) -> None:
        while True:
            event = await self.state.wait_for_poll_event()
            if event is None:
                return

            if event.play_sound:
                self._play_notification_sound()
            if event.dialogs_changed:
                self.refresh_dialogs_view()
            if event.messages_changed:
                self.refresh_messages_view()
            if event.dialogs_changed or event.messages_changed or event.status_changed:
                self.refresh_status()

    def __init__(self, session: MaxSession, state: AppState) -> None:
        super().__init__()
        self.session = session
        self.state = state

        self._dialogs_list: ListView | None = None
        self._messages_list: ListView | None = None
        self._status_bar: StatusBar | None = None
        self._dialogs_header: Static | None = None
        self._messages_header: Static | None = None
        self._poll_consumer_task: asyncio.Task[None] | None = None
        self._messages_view_syncing: bool = False

    def watch_focus_mode(self, old_value: str, new_value: str) -> None:
        self.refresh_status()


    def on_focus(self, event: events.Focus) -> None:
        if self._dialogs_list is not None and event.control is self._dialogs_list:
            self.focus_mode = "dialogs"
        elif self._messages_list is not None and event.control is self._messages_list:
            self.focus_mode = "messages"

    # -------------------------
    # compose
    # -------------------------

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main"):
            with Vertical(id="left_panel"):
                yield Static("Chats", id="dialogs_header")
                yield ListView(id="dialogs_list")

            with Vertical(id="right_panel"):
                yield Static("Messages", id="messages_header")
                yield ListView(id="messages_list")

        yield StatusBar("", id="status_bar")
        yield Footer()

    # -------------------------
    # lifecycle
    # -------------------------

    async def on_mount(self) -> None:
        self._dialogs_list = self.query_one("#dialogs_list", ListView)
        self._messages_list = self.query_one("#messages_list", ListView)
        self._status_bar = self.query_one("#status_bar", StatusBar)
        self._dialogs_header = self.query_one("#dialogs_header", Static)
        self._messages_header = self.query_one("#messages_header", Static)
        self.refresh_dialogs_view()
        self.refresh_messages_view()
        self.refresh_status()
        if self.state.requires_startup_login():
            self._show_startup_login(expired=False)
            return
        await self._start_connected_flow()

    async def on_unmount(self) -> None:
        if self._poll_consumer_task is not None:
            self._poll_consumer_task.cancel()
            try:
                await self._poll_consumer_task
            except asyncio.CancelledError:
                pass
            self._poll_consumer_task = None
        await self.state.close()

    # -------------------------
    # render helpers
    # -------------------------

    def set_status(self, text: str) -> None:
        if self._status_bar:
            self._status_bar.set_status(text)

    def refresh_status(self) -> None:
        if self.state.search.active:
            tail = "_" if self.state.search.input_mode else ""
            text = f"Search: {self.state.search.query}{tail}"
        else:
            text = self.state.status.text or ""
        if self._status_bar:
            self._status_bar.set_status(text)

        if self._messages_header:
            if self.state.current_chat_id is None:
                self._messages_header.update("Messages")
            else:
                current_chat = next(
                    (chat for chat in self.state.dialogs.items if chat.chat_id == self.state.current_chat_id),
                    None,
                )
                if current_chat is not None:
                    self._messages_header.update(format_chat_title_lines(current_chat, max_inline=40))
                else:
                    prefix = ">" if self.state.current_chat_id < 0 else "#"
                    self._messages_header.update(
                        f"{prefix}{self.state.current_chat_name} ({self.state.current_chat_id})"
                    )

    def refresh_dialogs_view(self) -> None:
        assert self._dialogs_list is not None
        self._dialogs_list.clear()

        if self._dialogs_header is not None:
            self._dialogs_header.update("Search" if self.state.search.active else "Chats")

        if self.state.search.active:
            for result in self.state.search.results:
                self._dialogs_list.append(SearchResultListItem(result, self.state.search.query))

            if self.state.search.has_more_history:
                self._dialogs_list.append(SearchLoadMoreListItem())

            if not self.state.search.results and not self.state.search.has_more_history:
                if self.state.search.input_mode:
                    self._dialogs_list.append(SearchHintListItem())
                else:
                    self._dialogs_list.append(SearchEmptyListItem())

            if self._dialogs_list.children:
                max_index = max(0, len(self._dialogs_list.children) - 1)
                self._dialogs_list.index = min(self.state.search.selected_index, max_index)
            return

        for chat in self.state.dialogs.items:
            self._dialogs_list.append(ChatListItem(chat))

        if not self.state.dialogs.is_empty:
            self._dialogs_list.index = self.state.dialogs.selected_index

    def refresh_messages_view(self) -> None:
        assert self._messages_list is not None
        self._messages_view_syncing = True
        self._messages_list.clear()

        if not self.state.messages.items:
            self._messages_list.append(ListItem(CenterMessage("No messages loaded")))
            self.call_after_refresh(self._finish_messages_view_sync)
        else:
            for msg in self.state.messages.items:
                self._messages_list.append(MessageListItem(msg))
            self.call_after_refresh(self._sync_messages_selection_visual)

    def sync_dialog_selection_from_widget(self) -> None:
        if self._dialogs_list is None:
            return
        idx = self._dialogs_list.index or 0
        if self.state.search.active:
            self.state.search.selected_index = idx
        else:
            self.state.dialogs.selected_index = idx

    def sync_message_selection_from_widget(self) -> None:
        if self._messages_list is None:
            return
        idx = self._messages_list.index or 0
        self.state.messages.selected_index = idx

    def focus_dialogs(self) -> None:
        self.focus_mode = "dialogs"
        if self._dialogs_list:
            self._dialogs_list.focus()

    def focus_messages(self) -> None:
        self.focus_mode = "messages"
        if self._messages_list:
            self._messages_list.focus()
            self.call_later(self._sync_messages_selection_visual)

    def _finish_messages_view_sync(self) -> None:
        self._messages_view_syncing = False

    def _sync_messages_selection_visual(self) -> None:
        if self._messages_list is None or not self.state.messages.items:
            self._messages_view_syncing = False
            return

        children = list(self._messages_list.children)
        if not children:
            self._messages_view_syncing = False
            return

        selected_index = max(0, min(self.state.messages.selected_index, len(children) - 1))
        self._messages_list.index = None

        for child in children:
            if isinstance(child, ListItem):
                child.highlighted = False

        self._messages_list.index = selected_index
        self.call_later(self._scroll_messages_to_selection)

    def _scroll_messages_to_selection(self) -> None:
        if self._messages_list is None or not self.state.messages.items:
            return

        children = list(self._messages_list.children)
        if not children:
            return

        selected_index = max(0, min(self.state.messages.selected_index, len(children) - 1))
        self._messages_list.index = selected_index
        target = children[selected_index]
        scroll_to_bottom = self.state.messages.scroll_to_bottom_on_refresh
        center_selection = self.state.messages.center_selection_on_refresh

        if selected_index >= len(children) - 1:
            self._messages_list.scroll_to_widget(
                target,
                animate=False,
                force=True,
                immediate=True,
                origin_visible=False,
            )
            self._messages_list.scroll_end(
                animate=False,
                force=True,
                immediate=True,
                x_axis=False,
                y_axis=True,
            )
            self.state.messages.scroll_to_bottom_on_refresh = False
            self.state.messages.center_selection_on_refresh = False
            self.call_later(self._finish_messages_view_sync)
            return

        self._messages_list.scroll_to_widget(
            target,
            animate=False,
            force=True,
            immediate=True,
            origin_visible=False,
            center=center_selection,
        )

        if scroll_to_bottom:
            self._messages_list.scroll_end(
                animate=False,
                force=True,
                immediate=True,
                x_axis=False,
                y_axis=True,
            )
        self.state.messages.scroll_to_bottom_on_refresh = False
        self.state.messages.center_selection_on_refresh = False
        self.call_later(self._finish_messages_view_sync)

    @staticmethod
    def _play_notification_sound() -> None:
        print("\a", end="", flush=True)

    async def _open_selected_dialog_safe(self, *, focus_messages: bool = True) -> None:
        try:
            await self.state.open_selected_dialog(history_limit=20)
        except Exception:
            self.refresh_status()
            return

        self.refresh_messages_view()
        self.refresh_status()
        if focus_messages:
            self.focus_messages()

    async def _refresh_safe(self, *, force_messages_bottom: bool = False) -> None:
        try:
            await self.state.refresh_dialogs()
            if self.state.current_chat_id is not None:
                await self.state.refresh_current_chat(announce_status=False)
        except Exception:
            self.refresh_status()
            return

        if force_messages_bottom and self.state.messages.items:
            self.state.messages.selected_index = max(0, len(self.state.messages.items) - 1)
            self.state.messages.scroll_to_bottom_on_refresh = True
            self.state.messages.center_selection_on_refresh = False

        self.refresh_dialogs_view()
        self.refresh_messages_view()
        self.refresh_status()

    async def _execute_search_safe(self) -> None:
        try:
            found = await self.state.execute_search(self.state.search.query)
        except Exception:
            self.refresh_status()
            return

        if found >= 0:
            self.refresh_dialogs_view()
            self.refresh_status()
            self.focus_dialogs()

    async def _activate_search_selection_safe(self) -> None:
        try:
            result = await self.state.activate_search_selection()
        except Exception:
            self.refresh_status()
            return

        if result == "load_more":
            self.refresh_dialogs_view()
            self.refresh_status()
            self.focus_dialogs()
            return

        if result == "jump":
            self.refresh_messages_view()
            self.refresh_status()
            self.focus_messages()

    async def _load_older_messages_safe(self, *, count: int = 20) -> int:
        try:
            return await self.state.load_older_messages(count=count)
        except Exception:
            self.refresh_status()
            return 0

    # -------------------------
    # actions
    # -------------------------

    async def action_toggle_focus(self) -> None:
        if self.focus_mode == "dialogs":
            self.focus_messages()
        else:
            self.focus_dialogs()

    async def action_activate(self) -> None:
        if self.focus_mode == "dialogs":
            self.sync_dialog_selection_from_widget()
            if self.state.search.active:
                await self._activate_search_selection_safe()
            else:
                await self._open_selected_dialog_safe()
        elif self.focus_mode == "messages":
            self.sync_message_selection_from_widget()
            try:
                jumped = await self.state.jump_to_reply_source()
                if jumped:
                    self.refresh_messages_view()
                self.refresh_status()
                self.focus_messages()
            except Exception:
                self.refresh_status()
                self.focus_messages()

    async def action_help(self) -> None:
        await self.push_screen(
            InfoModal(
                "Max Commander",
                "Tab - переключить панель\n"
                "Enter - открыть чат / перейти к источнику ответа\n"
                "Ctrl+F / F8 - поиск в текущем чате\n"
                "Esc - отмена / закрыть окно\n"
                "F2 - обновить список чатов\n"
                "F3 - написать сообщение\n"
                "F4 - ответить на выбранное сообщение\n"
                "F5 - скачать вложение\n"
                "F6 - отправить вложение\n"
                "F7 - отметить прочитанным до выбранного\n"
                "F9 - настройки\n"
                "Стрелки - навигация\n"
                "F10 - выход",
            )
        )

    async def action_refresh(self) -> None:
        self.set_status("Обновление...")
        await self._refresh_safe(force_messages_bottom=True)

    async def action_send(self) -> None:
        if self.state.current_chat_id is None:
            self.state.status.set("Сначала открой чат", "warning")
            self.refresh_status()
            return

        def _on_close(result: str | None) -> None:
            self.call_later(self._handle_compose_result, result, None)

        self.push_screen(
            ComposeMessageModal(
                title="Новое сообщение",
                initial_text="",
            ),
            callback=_on_close,
        )

    async def action_reply(self) -> None:
        if self.state.current_chat_id is None:
            self.state.status.set("Сначала открой чат", "warning")
            self.refresh_status()
            return

        selected = self.state.get_selected_message()
        if selected is None:
            self.state.status.set("Выберите сообщение для ответа", "warning")
            self.refresh_status()
            self.focus_messages()
            return

        reply_to = selected.message_id

        def _on_close(result: str | None) -> None:
            self.call_later(self._handle_compose_result, result, reply_to)

        self.push_screen(
            ComposeMessageModal(
                title="Ответ",
                initial_text="",
            ),
            callback=_on_close,
        )

    async def action_download(self) -> None:
        attachments = self.state.get_downloadable_attachments()
        if not attachments:
            self.state.status.set("У выбранного сообщения нет скачиваемых вложений", "warning")
            self.refresh_status()
            self.focus_messages()
            return

        def _on_close(result: DownloadRequest | None) -> None:
            self.call_later(self._handle_download_result, result)

        is_batch = len(attachments) > 1
        if is_batch:
            attachment_text = (
                f"Пакетное скачивание: {len(attachments)} вложений\n"
                f"Сообщение: {', '.join(describe_attachment(att.kind, att.attach_id, ext=att.ext) for att in attachments[:3])}"
            )
            initial_path = self.state.build_download_directory()
        else:
            attachment = attachments[0]
            attachment_text = describe_attachment(
                attachment.kind,
                attachment.attach_id,
                ext=attachment.ext,
                name=attachment.name,
            )
            initial_path = self.state.build_download_path(attachment)

        self.push_screen(
            DownloadModal(
                title="Скачать вложения" if is_batch else "Скачать вложение",
                attachment_text=attachment_text,
                initial_path=initial_path,
                attach_ids=[str(att.attach_id) for att in attachments],
                is_directory=is_batch,
            ),
            callback=_on_close,
        )

    async def action_upload(self) -> None:
        if self.state.current_chat_id is None:
            self.state.status.set("Сначала открой чат", "warning")
            self.refresh_status()
            return

        def _on_close(result: UploadRequest | None) -> None:
            self.call_later(self._handle_upload_result, result)

        self.push_screen(
            UploadModal(
                title="Отправка вложения",
                initial_kind="photo",
            ),
            callback=_on_close,
        )

    async def action_mark_read(self) -> None:
        try:
            unread = await self.state.mark_selected_as_read()
            if unread is not None:
                self.refresh_dialogs_view()
                self.refresh_messages_view()
            self.refresh_status()
            self.focus_messages()
        except Exception:
            self.refresh_status()
            self.focus_messages()

    async def action_search(self) -> None:
        if not self.state.begin_search():
            self.refresh_status()
            return

        self.refresh_dialogs_view()
        self.refresh_status()
        self.focus_dialogs()

    async def action_settings(self) -> None:
        def _on_close(result: str | None) -> None:
            self.call_later(self._handle_settings_result, result)

        self.push_screen(
            SettingsModal(
                account_name=self.state.me_name,
                account_id=self.state.me_id,
            ),
            callback=_on_close,
        )

    # -------------------------
    # keyboard / selection events
    # -------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "dialogs_list":
            self.focus_mode = "dialogs"
            self.sync_dialog_selection_from_widget()
            if not self.state.search.active:
                await self._open_selected_dialog_safe()

        elif event.list_view.id == "messages_list":
            if self._messages_view_syncing:
                return
            self.focus_mode = "messages"
            self.sync_message_selection_from_widget()
            self.refresh_status()

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "dialogs_list":
            self.focus_mode = "dialogs"
            self.sync_dialog_selection_from_widget()
            self.refresh_status()
        elif event.list_view.id == "messages_list":
            if self._messages_view_syncing:
                return
            self.focus_mode = "messages"
            self.sync_message_selection_from_widget()
            self.refresh_status()

    async def on_key(self, event: events.Key) -> None:
        if self.state.search.active:
            if event.key == "escape":
                self.state.cancel_search()
                self.refresh_dialogs_view()
                self.refresh_status()
                self.focus_dialogs()
                event.stop()
                return

            if self.state.search.input_mode:
                if event.key == "enter":
                    await self._execute_search_safe()
                    event.stop()
                    return
                if event.key == "backspace":
                    self.state.search.query = self.state.search.query[:-1]
                    self.refresh_status()
                    event.stop()
                    return
                if event.key == "delete":
                    self.state.search.query = ""
                    self.refresh_status()
                    event.stop()
                    return
                if event.key == "space":
                    self.state.search.query += " "
                    self.refresh_status()
                    event.stop()
                    return
                if event.is_printable and event.character:
                    self.state.search.query += event.character
                    self.refresh_status()
                    event.stop()
                    return

        # Enter открывает чат из левой панели
        if event.key == "enter":
            if self.focus_mode == "dialogs":
                self.sync_dialog_selection_from_widget()
                if self.state.search.active:
                    await self._activate_search_selection_safe()
                else:
                    await self._open_selected_dialog_safe()
                event.stop()
                return
            if self.focus_mode == "messages":
                self.sync_message_selection_from_widget()
                try:
                    jumped = await self.state.jump_to_reply_source()
                    if jumped:
                        self.refresh_messages_view()
                    self.refresh_status()
                    self.focus_messages()
                except Exception:
                    self.refresh_status()
                    self.focus_messages()
                event.stop()
                return

        # Стрелка вверх в сообщениях -> подгрузка старых
        if self.focus_mode == "messages" and event.key == "up":
            if self._messages_list is not None:
                idx = self._messages_list.index or 0
                if idx <= 0:
                    added = await self._load_older_messages_safe(count=20)
                    if added > 0:
                        self.refresh_messages_view()
                        if self._messages_list is not None:
                            self._messages_list.index = added
                        self.refresh_status()


# =========================
# FACTORY
# =========================

def build_app(session: MaxSession, state: AppState) -> MaxCommanderApp:
    return MaxCommanderApp(session=session, state=state)

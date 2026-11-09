"""Settings page (P2): edit [COLLECT] / [NETWORK] / [SAFETY] and write them to the ini.

The form is generated from the Settings dataclasses, so new fields show up automatically.
Saved values apply to the next connect; a running session keeps the settings it started with.
"""
import copy
from dataclasses import fields
from typing import Dict, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TabbedContent, TabPane

from dobot_control.settings import (FIELD_HELP, FIELD_LABEL, ROBOT_TYPES, Settings, format_field, parse_field,
                                    validate)

SECTIONS = (("collect", "采集"), ("network", "网络"), ("safety", "安全"))


class SettingsScreen(ModalScreen):
    """Dismisses with the new Settings on save, or None on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存"),
    ]

    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    #dialog { width: 96; max-width: 100%; height: 90%; border: thick $accent; background: $surface; }
    #dialog > Static#note { padding: 0 1; color: $warning; height: auto; }
    #dialog TabbedContent { height: 1fr; }
    #dialog ContentSwitcher { height: 1fr; }
    #dialog TabPane { height: 1fr; padding: 0; }
    .row { height: auto; margin: 0 1; }
    .row Label.name { width: 20; padding: 1 0 0 0; }
    .row Label.key { color: $text-muted; }
    .row Input, .row Select { width: 30; }
    .row Switch { width: 30; }
    .row Label.help { width: 1fr; padding: 1 0 0 1; color: $text-muted; }
    .row.-invalid Label.name { color: $error; text-style: bold; }
    #errors { padding: 0 1; color: $error; height: auto; }
    #buttons { height: auto; padding: 0 1; }
    #buttons Button { margin-right: 1; }
    """

    def __init__(self, settings: Settings, session_active: bool = False, default_save_root: str = ""):
        super().__init__()
        self._settings = settings
        self._session_active = session_active
        self._default_save_root = default_save_root

    def _wid(self, sec: str, name: str) -> str:
        return f"f-{sec}-{name}"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            note = ("会话正在运行：现在保存，下次连接时生效。"
                    if self._session_active else "保存到 scripts/dobot_config/dobot_settings.ini")
            yield Static(note, id="note")
            with TabbedContent():
                for sec, title in SECTIONS:
                    with TabPane(title, id=f"tab-{sec}"):
                        with VerticalScroll():
                            obj = getattr(self._settings, sec)
                            for f in fields(obj):
                                yield self._row(sec, f.name, getattr(obj, f.name))
            yield Static("", id="errors")
            with Horizontal(id="buttons"):
                yield Button("保存", id="save", variant="primary")
                yield Button("取消", id="cancel")
                yield Button("恢复本页默认值", id="defaults")

    def _row(self, sec: str, name: str, value) -> Horizontal:
        wid = self._wid(sec, name)
        if isinstance(value, bool):
            widget = Switch(value=value, id=wid)
        elif (sec, name) == ("network", "robot_type"):
            widget = Select([(t, t) for t in ROBOT_TYPES], value=value, allow_blank=False, id=wid)
        else:
            placeholder = self._default_save_root if (sec, name) == ("collect", "save_root") else ""
            widget = Input(value=format_field(value), placeholder=placeholder, id=wid)
        help_text = Text(FIELD_HELP.get((sec, name), ""))
        help_text.append(f"  [{name}]", style="dim")
        return Horizontal(Label(FIELD_LABEL.get((sec, name), name), classes="name"), widget,
                          Label(help_text, classes="help"),
                          classes="row", id=f"row-{sec}-{name}")

    # ------------------------------------------------------------------ reading the form
    def _set_widget(self, sec: str, name: str, value) -> None:
        w = self.query_one(f"#{self._wid(sec, name)}")
        if isinstance(w, Switch):
            w.value = value
        elif isinstance(w, Select):
            w.value = value
        else:
            w.value = format_field(value)

    def collect_form(self) -> Tuple[Optional[Settings], Dict[tuple, str]]:
        """Parse and validate every field; returns (settings or None, {(sec, name): error})."""
        new = copy.deepcopy(self._settings)
        errors = {}
        for sec, _ in SECTIONS:
            obj = getattr(new, sec)
            for f in fields(obj):
                w = self.query_one(f"#{self._wid(sec, f.name)}")
                if isinstance(w, (Switch, Select)):
                    setattr(obj, f.name, w.value)
                    continue
                try:
                    setattr(obj, f.name, parse_field(sec, f.name, w.value))
                except ValueError as e:
                    errors[(sec, f.name)] = str(e) or "取值无效"
        if not errors:
            for sec, name, msg in validate(new):
                errors[(sec, name)] = msg
        for sec, _ in SECTIONS:
            for f in fields(getattr(new, sec)):
                self.query_one(f"#row-{sec}-{f.name}").set_class((sec, f.name) in errors, "-invalid")
        return (None if errors else new), errors

    # ------------------------------------------------------------------ actions
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "defaults":
            self._reset_tab()

    def _reset_tab(self) -> None:
        active = self.query_one(TabbedContent).active  # "tab-<sec>"
        sec = active.replace("tab-", "")
        defaults = getattr(Settings(), sec)
        for f in fields(defaults):
            self._set_widget(sec, f.name, getattr(defaults, f.name))
        self.notify(f"{dict(SECTIONS)[sec]}：已恢复默认值（尚未保存）")

    def action_save(self) -> None:
        new, errors = self.collect_form()
        self.query_one("#errors", Static).update(
            "\n".join(f"{FIELD_LABEL.get((sec, name), name)}（{name}）：{msg}"
                      for (sec, name), msg in errors.items()))
        if errors:
            self.query_one(TabbedContent).active = f"tab-{next(iter(errors))[0]}"
            return
        self.dismiss(new)

    def action_cancel(self) -> None:
        self.dismiss(None)

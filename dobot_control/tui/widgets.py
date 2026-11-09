"""Small shared TUI widgets."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmScreen(ModalScreen):
    """Yes/no dialog; dismisses with True or False."""

    BINDINGS = [Binding("escape", "no", "否"), Binding("n", "no", "否"), Binding("y", "yes", "是")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical { width: 70; max-width: 100%; height: auto; border: thick $warning;
                               background: $surface; padding: 1 2; }
    ConfirmScreen Static { height: auto; margin-bottom: 1; }
    ConfirmScreen Horizontal { height: auto; }
    ConfirmScreen Button { margin-right: 1; }
    """

    def __init__(self, message: str, yes_label: str = "是 (y)", no_label: str = "否 (n)"):
        super().__init__()
        self._message = message
        self._yes = yes_label
        self._no = no_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._message)
            with Horizontal():
                yield Button(self._yes, id="yes", variant="warning")
                yield Button(self._no, id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

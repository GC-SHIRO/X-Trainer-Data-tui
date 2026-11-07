"""Episode manager (P3): list recorded episodes per project, check them, move bad ones to trash."""
import os
from typing import Callable, List, Optional

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Select, Static

from dobot_control.episodes import (EpisodeInfo, human_size, list_episodes, list_projects, trash_dir_for,
                                    trash_episode, verify_episode)
from dobot_control.tui.widgets import ConfirmScreen


class EpisodesScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Refresh"),
        Binding("v", "verify", "Deep check"),
        Binding("d", "discard", "Move to trash"),
    ]
    DEFAULT_CSS = """
    EpisodesScreen #bar { height: auto; padding: 0 1; }
    EpisodesScreen #bar Label { padding: 1 1 0 0; }
    EpisodesScreen #bar Select { width: 40; }
    EpisodesScreen #summary { padding: 1 1 0 1; width: 1fr; color: $text-muted; }
    EpisodesScreen DataTable { height: 1fr; }
    EpisodesScreen #detail { height: auto; max-height: 10; border: round $accent; padding: 0 1; }
    """

    def __init__(self, save_root: str, project: str,
                 busy_episode: Callable[[], Optional[str]] = lambda: None):
        """busy_episode() returns the episode path currently being recorded (never trashed)."""
        super().__init__()
        self.save_root = save_root
        self.project = project
        self._busy_episode = busy_episode
        self.episodes: List[EpisodeInfo] = []

    @property
    def save_dir(self) -> str:
        return os.path.join(self.save_root, self.project, "collect_data")

    def compose(self) -> ComposeResult:
        yield Header()
        projects = list_projects(self.save_root)
        if self.project not in projects:
            projects = sorted(projects + [self.project])
        with Horizontal(id="bar"):
            yield Label("Project")
            yield Select([(p, p) for p in projects], value=self.project, allow_blank=False, id="project")
            yield Static("", id="summary")
        table = DataTable(id="table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Episode", "Started", "Frames", "Size", "Status")
        yield table
        detail = Static("", id="detail")
        detail.border_title = "Details"
        yield detail
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "episodes"
        self.query_one("#table", DataTable).focus()
        self.action_reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "project" and event.value != self.project:
            self.project = event.value
            self.action_reload()

    # ------------------------------------------------------------------ loading
    def action_reload(self) -> None:
        self.query_one("#summary", Static).update(f"scanning {self.save_dir} ...")
        self._load(self.save_dir)

    @work(thread=True, exclusive=True, group="scan")
    def _load(self, save_dir: str) -> None:
        eps = list_episodes(save_dir)
        self.app.call_from_thread(self._show, save_dir, eps)

    def _show(self, save_dir: str, eps: List[EpisodeInfo]) -> None:
        if save_dir != self.save_dir:
            return  # project changed while scanning
        self.episodes = eps
        table = self.query_one("#table", DataTable)
        table.clear()
        for ep in eps:
            table.add_row(*self._cells(ep), key=ep.path)
        n_bad = sum(not e.ok for e in eps)
        summary = (f"{len(eps)} episodes, {sum(e.frames for e in eps)} frames, "
                   f"{human_size(sum(e.size_bytes for e in eps))}")
        if n_bad:
            summary += f", {n_bad} with problems"
        self.query_one("#summary", Static).update(summary + f"   {save_dir}")
        self._update_detail()

    def _cells(self, ep: EpisodeInfo):
        started = ep.start_time.strftime("%Y-%m-%d %H:%M:%S") if ep.start_time else "-"
        if ep.issues:
            status = Text(ep.issues[0] + (f" (+{len(ep.issues) - 1})" if len(ep.issues) > 1 else ""), style="red")
        elif ep.verified:
            status = Text("ok (checked)", style="green")
        else:
            status = Text("ok", style="green")
        return ep.name, started, str(ep.frames), human_size(ep.size_bytes), status

    def _selected(self) -> Optional[EpisodeInfo]:
        table = self.query_one("#table", DataTable)
        if not self.episodes or table.cursor_row is None or table.cursor_row >= len(self.episodes):
            return None
        return self.episodes[table.cursor_row]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_detail()

    def _update_detail(self) -> None:
        ep = self._selected()
        if ep is None:
            self.query_one("#detail", Static).update("No episodes" if not self.episodes else "")
            return
        t = Text(f"{ep.path}\n")
        t.append("  ".join(f"{k}: {v}" for k, v in ep.counts.items()))
        if ep.first_idx is not None:
            t.append(f"   frame index {ep.first_idx}..{ep.last_idx}")
        for issue in ep.issues:
            t.append(f"\n- {issue}", style="red")
        self.query_one("#detail", Static).update(t)

    def _replace(self, ep: EpisodeInfo) -> None:
        table = self.query_one("#table", DataTable)
        for col, value in zip(table.columns, self._cells(ep)):
            table.update_cell(ep.path, col, value)
        self._update_detail()

    # ------------------------------------------------------------------ actions
    def action_verify(self) -> None:
        ep = self._selected()
        if ep:
            self.notify(f"Checking {ep.name} ...")
            self._verify(ep)

    @work(thread=True, group="verify")
    def _verify(self, ep: EpisodeInfo) -> None:
        verify_episode(ep)
        self.app.call_from_thread(self._replace, ep)
        self.app.call_from_thread(self.notify, f"{ep.name}: " + ("ok" if ep.verified else "problems found"),
                                  severity="information" if ep.verified else "error")

    def action_discard(self) -> None:
        ep = self._selected()
        if not ep:
            return
        busy = self._busy_episode()
        if busy and os.path.normpath(busy) == os.path.normpath(ep.path):
            self.notify("This episode is being recorded", severity="error")
            return
        trash = trash_dir_for(self.save_dir)

        def done(yes: bool) -> None:
            if not yes:
                return
            try:
                dest = trash_episode(ep, trash)
            except Exception as e:
                self.notify(f"Could not move: {e}", severity="error")
                return
            self.notify(f"Moved to {dest}")
            self.action_reload()

        self.app.push_screen(ConfirmScreen(
            f"Move episode {ep.name} ({ep.frames} frames) to\n{trash}?\n\n"
            "It is not deleted: move the folder back to restore it."), done)

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
        Binding("escape", "app.pop_screen", "返回"),
        Binding("r", "reload", "刷新"),
        Binding("v", "verify", "深度检查"),
        Binding("d", "discard", "移到回收站"),
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
            yield Label("项目")
            yield Select([(p, p) for p in projects], value=self.project, allow_blank=False, id="project")
            yield Static("", id="summary")
        table = DataTable(id="table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Episode", "开始时间", "帧数", "大小", "状态")
        yield table
        detail = Static("", id="detail")
        detail.border_title = "详情"
        yield detail
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "数据管理"
        self.query_one("#table", DataTable).focus()
        self.action_reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "project" and event.value != self.project:
            self.project = event.value
            self.action_reload()

    # ------------------------------------------------------------------ loading
    def action_reload(self) -> None:
        self.query_one("#summary", Static).update(f"正在扫描 {self.save_dir} ...")
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
        summary = (f"共 {len(eps)} 条，{sum(e.frames for e in eps)} 帧，"
                   f"{human_size(sum(e.size_bytes for e in eps))}")
        if n_bad:
            summary += f"，{n_bad} 条有问题"
        self.query_one("#summary", Static).update(summary + f"   {save_dir}")
        self._update_detail()

    def _cells(self, ep: EpisodeInfo):
        started = ep.start_time.strftime("%Y-%m-%d %H:%M:%S") if ep.start_time else "-"
        if ep.issues:
            status = Text(ep.issues[0] + (f"（另有 {len(ep.issues) - 1} 项）" if len(ep.issues) > 1 else ""), style="red")
        elif ep.verified:
            status = Text("正常（已深度检查）", style="green")
        else:
            status = Text("正常", style="green")
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
            self.query_one("#detail", Static).update("没有 episode" if not self.episodes else "")
            return
        t = Text(f"{ep.path}\n")
        t.append("  ".join(f"{k}: {v}" for k, v in ep.counts.items()))
        if ep.first_idx is not None:
            t.append(f"   帧号 {ep.first_idx}..{ep.last_idx}")
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
            self.notify(f"正在检查 {ep.name} ...")
            self._verify(ep)

    @work(thread=True, group="verify")
    def _verify(self, ep: EpisodeInfo) -> None:
        verify_episode(ep)
        self.app.call_from_thread(self._replace, ep)
        self.app.call_from_thread(self.notify, f"{ep.name}：" + ("正常" if ep.verified else "发现问题"),
                                  severity="information" if ep.verified else "error")

    def action_discard(self) -> None:
        ep = self._selected()
        if not ep:
            return
        busy = self._busy_episode()
        if busy and os.path.normpath(busy) == os.path.normpath(ep.path):
            self.notify("这条 episode 正在录制", severity="error")
            return
        trash = trash_dir_for(self.save_dir)

        def done(yes: bool) -> None:
            if not yes:
                return
            try:
                dest = trash_episode(ep, trash)
            except Exception as e:
                self.notify(f"移动失败：{e}", severity="error")
                return
            self.notify(f"已移到 {dest}")
            self.action_reload()

        self.app.push_screen(ConfirmScreen(
            f"把 episode {ep.name}（{ep.frames} 帧）移到\n{trash}？\n\n"
            "不会真正删除，把文件夹移回 collect_data 即可恢复。"), done)

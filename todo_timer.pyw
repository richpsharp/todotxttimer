from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import subprocess
import sys
import json
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from pathlib import Path
from datetime import date, datetime, timedelta
from urllib import error, request
import webbrowser

from todo_core import (
    AppConfig,
    ConfigStore,
    TodoFormatError,
    TodoItem,
    TodoStore,
    extract_first_url,
    format_duration,
    format_timestamp,
    is_date_string,
    parse_timestamp,
    parse_todo_line,
)

APP_TITLE = "TodoTimerTXT"
DEFAULT_IDLE_TIMEOUT_MINUTES = 10
REPORT_MODEL = "gpt-5-mini"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass(slots=True)
class IdleTimerEvent:
    item_id: str
    description: str
    detected_at: datetime
    last_activity_at: datetime
    running_seconds_at_detection: int
    running_seconds_at_last_activity: int
    idle_seconds: int


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str = "") -> None:
        self.widget = widget
        self.text = text
        self.tip_window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        widget.bind("<ButtonPress>", self.hide)

    def set_text(self, text: str) -> None:
        self.text = text
        if self.tip_window is not None:
            label = self.tip_window.winfo_children()[0]
            if isinstance(label, ttk.Label):
                label.configure(text=text)

    def show(self, event: tk.Event[tk.Widget]) -> None:
        if self.tip_window is not None or not self.text:
            return
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 10}")
        ttk.Label(
            self.tip_window,
            text=self.text,
            padding=(6, 4),
            relief="solid",
            borderwidth=1,
            wraplength=720,
        ).pack()

    def hide(self, event: tk.Event[tk.Widget] | None = None) -> None:
        if self.tip_window is None:
            return
        self.tip_window.destroy()
        self.tip_window = None
@dataclass(slots=True)
class ReportTask:
    item: TodoItem
    status: str
    source: str
    activity_date: str


class IdleTimerDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, event: IdleTimerEvent):
        super().__init__(master)
        self.title("Timer stopped after inactivity")
        self.resizable(False, False)
        self.transient(master)
        self.result = "close"

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text="The running timer was stopped because no keyboard or mouse activity was detected.",
            wraplength=520,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(
            body,
            text=f"Task: {event.description}",
            wraplength=520,
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(
            body,
            text=(
                f"Timer duration when stopped: "
                f"{format_duration(event.running_seconds_at_detection)}"
            ),
        ).grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Label(
            body,
            text=(
                f"Time at last activity: "
                f"{format_duration(event.running_seconds_at_last_activity)}"
            ),
        ).grid(row=3, column=0, sticky="w", pady=(0, 4))
        ttk.Label(
            body,
            text=(
                f"Idle time detected: {format_duration(event.idle_seconds)} "
                f"since {event.last_activity_at:%Y-%m-%d %H:%M:%S}"
            ),
        ).grid(row=4, column=0, sticky="w", pady=(0, 12))

        button_row = ttk.Frame(body)
        button_row.grid(row=5, column=0, sticky="e")
        ttk.Button(
            button_row,
            text="Close",
            command=lambda: self._finish("close"),
        ).pack(side="right")
        ttk.Button(
            button_row,
            text="Reset to last activity",
            command=lambda: self._finish("reset"),
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            button_row,
            text="Restart timer",
            command=lambda: self._finish("restart"),
        ).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("close"))
        self.bind("<Escape>", lambda event_: self._finish("close"))
        self.grab_set()
        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())

    def _finish(self, result: str) -> None:
        self.result = result
        self.destroy()


class RunningTimerRecoveryDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        item: TodoItem,
        closed_at: datetime,
        opened_at: datetime,
    ):
        super().__init__(master)
        self.title("Timer left running")
        self.resizable(False, False)
        self.transient(master)
        self.result = "continue"

        closed_seconds = max(0, int((opened_at - closed_at).total_seconds()))

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text=(
                "The timer was left running on this task while the app was "
                "closed."
            ),
            wraplength=540,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(
            body,
            text=f"Task: {item.description}",
            wraplength=540,
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(
            body,
            text=f"App closed at: {closed_at:%Y-%m-%d %H:%M:%S}",
        ).grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Label(
            body,
            text=f"Time closed: {format_duration(closed_seconds)}",
        ).grid(row=3, column=0, sticky="w", pady=(0, 12))

        button_row = ttk.Frame(body)
        button_row.grid(row=4, column=0, sticky="e")
        ttk.Button(
            button_row,
            text="Continue timer",
            command=lambda: self._finish("continue"),
        ).pack(side="right")
        ttk.Button(
            button_row,
            text="Stop at app close",
            command=lambda: self._finish("stop_at_close"),
        ).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("continue"))
        self.bind("<Escape>", lambda event_: self._finish("continue"))
        self.grab_set()
        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())

    def _finish(self, result: str) -> None:
        self.result = result
        self.destroy()


class ReportDateDialog(tk.Toplevel):
    MONTHS = [
        ("January", 1),
        ("February", 2),
        ("March", 3),
        ("April", 4),
        ("May", 5),
        ("June", 6),
        ("July", 7),
        ("August", 8),
        ("September", 9),
        ("October", 10),
        ("November", 11),
        ("December", 12),
    ]

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.title("Generate report")
        self.resizable(False, False)
        self.transient(master)
        self.result: tuple[str, str] | None = None

        today = date.today()
        self.today = today
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.month_var = tk.StringVar(value=today.strftime("%B"))
        self.year_var = tk.IntVar(value=today.year)

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)

        preset_frame = ttk.LabelFrame(body, text="Quick ranges", padding=10)
        preset_frame.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(0, 12),
        )
        ttk.Button(
            preset_frame,
            text="Current month to date",
            command=self.apply_current_month,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(
            preset_frame,
            text="Last month",
            command=self.apply_last_month,
        ).grid(row=0, column=1, sticky="ew")

        month_frame = ttk.LabelFrame(body, text="Pick a month", padding=10)
        month_frame.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(0, 12),
        )
        ttk.Label(month_frame, text="Month").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.month_combo = ttk.Combobox(
            month_frame,
            textvariable=self.month_var,
            values=[month for month, _ in self.MONTHS],
            width=14,
            state="readonly",
        )
        self.month_combo.grid(row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Label(month_frame, text="Year").grid(
            row=0, column=2, sticky="w", padx=(0, 8)
        )
        ttk.Spinbox(
            month_frame,
            textvariable=self.year_var,
            from_=1970,
            to=9999,
            width=7,
        ).grid(row=0, column=3, sticky="w")
        ttk.Button(
            month_frame,
            text="Use full month",
            command=self.apply_selected_month,
        ).grid(row=1, column=1, sticky="ew", pady=(10, 0), padx=(0, 8))
        ttk.Button(
            month_frame,
            text="Use month to date",
            command=self.apply_selected_month_to_date,
        ).grid(row=1, column=2, columnspan=2, sticky="ew", pady=(10, 0))

        ttk.Label(body, text="Start date").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 8)
        )
        ttk.Entry(body, textvariable=self.start_var, width=18).grid(
            row=2, column=1, sticky="w", pady=(0, 8)
        )
        ttk.Label(body, text="End date").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 8)
        )
        ttk.Entry(body, textvariable=self.end_var, width=18).grid(
            row=3, column=1, sticky="w", pady=(0, 8)
        )
        ttk.Label(body, text="Use YYYY-MM-DD").grid(
            row=4, column=1, sticky="w", pady=(0, 10)
        )

        button_row = ttk.Frame(body)
        button_row.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(
            side="right"
        )
        ttk.Button(button_row, text="Generate", command=self._on_generate).pack(
            side="right", padx=(0, 8)
        )

        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Return>", lambda event: self._on_generate())
        self.apply_current_month()
        self.grab_set()
        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())

    def apply_current_month(self) -> None:
        self.set_range(date(self.today.year, self.today.month, 1), self.today)

    def apply_last_month(self) -> None:
        year = self.today.year
        month = self.today.month - 1
        if month == 0:
            year -= 1
            month = 12
        self.set_month_range(year, month)
        self.month_var.set(date(year, month, 1).strftime("%B"))
        self.year_var.set(year)

    def apply_selected_month(self) -> None:
        year, month = self.selected_year_month()
        if year is None or month is None:
            return
        self.set_month_range(year, month)

    def apply_selected_month_to_date(self) -> None:
        year, month = self.selected_year_month()
        if year is None or month is None:
            return
        start = date(year, month, 1)
        self.set_range(start, self.today)

    def selected_year_month(self) -> tuple[int | None, int | None]:
        month_lookup = dict(self.MONTHS)
        month = month_lookup.get(self.month_var.get())
        try:
            year = int(self.year_var.get())
        except (TypeError, tk.TclError, ValueError):
            year = None
        if month is None or year is None or year < 1:
            messagebox.showerror(
                APP_TITLE,
                "Choose a valid month and year.",
                parent=self,
            )
            return None, None
        return year, month

    def set_month_range(self, year: int, month: int) -> None:
        self.month_var.set(date(year, month, 1).strftime("%B"))
        self.year_var.set(year)
        self.set_range(date(year, month, 1), self.last_day_of_month(year, month))

    def set_range(self, start_date: date, end_date: date) -> None:
        self.start_var.set(start_date.strftime("%Y-%m-%d"))
        self.end_var.set(end_date.strftime("%Y-%m-%d"))

    @staticmethod
    def last_day_of_month(year: int, month: int) -> date:
        if month == 12:
            return date(year, 12, 31)
        return date(year, month + 1, 1) - timedelta(days=1)

    def _on_generate(self) -> None:
        start_date = self.start_var.get().strip()
        end_date = self.end_var.get().strip()
        if not is_date_string(start_date) or not is_date_string(end_date):
            messagebox.showerror(
                APP_TITLE,
                "Start and end dates must use YYYY-MM-DD.",
                parent=self,
            )
            return
        if start_date > end_date:
            messagebox.showerror(
                APP_TITLE,
                "Start date must be before or equal to end date.",
                parent=self,
            )
            return
        self.result = (start_date, end_date)
        self.destroy()


class OpenAIKeyDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, current_key: str):
        super().__init__(master)
        self.title("OpenAI key")
        self.resizable(True, False)
        self.transient(master)
        self.result: str | None = None
        self.key_var = tk.StringVar(value=current_key)

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text="OpenAI API key").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        entry = ttk.Entry(body, textvariable=self.key_var, width=72, show="*")
        entry.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        button_row = ttk.Frame(body)
        button_row.grid(row=2, column=0, sticky="e")
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(
            side="right"
        )
        ttk.Button(button_row, text="Save", command=self._on_save).pack(
            side="right", padx=(0, 8)
        )

        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Return>", lambda event: self._on_save())
        self.grab_set()
        entry.focus_set()
        self.update_idletasks()
        self.minsize(max(520, self.winfo_width()), self.winfo_height())

    def _on_save(self) -> None:
        self.result = self.key_var.get().strip()
        self.destroy()


class ReportResultDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, title: str, report_text: str):
        super().__init__(master)
        self.title(title)
        self.resizable(True, True)
        self.transient(master)

        body = ttk.Frame(self, padding=10)
        body.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        text = tk.Text(body, width=100, height=34, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        text.insert("1.0", report_text)
        text.configure(state="normal")

        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)

        button_row = ttk.Frame(body)
        button_row.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(
            button_row,
            text="Close",
            command=self.destroy,
        ).pack(side="right")
        ttk.Button(
            button_row,
            text="Copy",
            command=lambda: self._copy_report(report_text),
        ).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda event: self.destroy())
        self.update_idletasks()
        self.minsize(760, 460)

    def _copy_report(self, report_text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(report_text)


class LastInputInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


def get_system_idle_seconds() -> int | None:
    try:
        last_input = LastInputInfo()
        last_input.cbSize = ctypes.sizeof(last_input)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input)):
            return None
        tick_count = ctypes.windll.kernel32.GetTickCount()
        elapsed_ms = (tick_count - last_input.dwTime) & 0xFFFFFFFF
        return max(0, int(elapsed_ms / 1000))
    except Exception:
        return None


class TaskDialog(tk.Toplevel):
    def __init__(
        self, master: tk.Misc, title: str, item: TodoItem | None = None
    ):
        super().__init__(master)
        self.title(title)
        self.resizable(True, True)
        self.transient(master)
        self.result: dict[str, object] | None = None

        self.priority_var = tk.StringVar(value=item.priority or "")
        self.created_var = tk.StringVar(value=item.creation_date or "")
        self.completed_var = tk.BooleanVar(
            value=item.completed if item else False
        )
        self.completed_date_var = tk.StringVar(value=item.completion_date or "")
        self.time_spent_var = tk.StringVar(
            value=format_duration(item.time_spent_seconds if item else 0)
        )
        self.last_worked_var = tk.StringVar(
            value=(
                item.last_worked_at.strftime("%Y-%m-%d-%H-%M-%S")
                if item and item.last_worked_at
                else ""
            )
        )

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        ttk.Label(body, text="Description").grid(
            row=0, column=0, sticky="nw", padx=(0, 8), pady=(0, 8)
        )
        self.description_text = tk.Text(body, height=5, width=90, wrap="word")
        self.description_text.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        if item:
            self.description_text.insert("1.0", item.description)

        ttk.Label(body, text="Priority").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.priority_combo = ttk.Combobox(
            body,
            textvariable=self.priority_var,
            values=[""] + [chr(code) for code in range(ord("A"), ord("Z") + 1)],
            width=8,
            state="readonly",
        )
        self.priority_combo.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(body, text="Created").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4
        )
        ttk.Entry(body, textvariable=self.created_var, width=18).grid(
            row=2, column=1, sticky="w", pady=4
        )

        ttk.Checkbutton(
            body,
            text="Completed",
            variable=self.completed_var,
            command=self._toggle_completion,
        ).grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(body, text="Completed date").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.completed_entry = ttk.Entry(
            body, textvariable=self.completed_date_var, width=18
        )
        self.completed_entry.grid(row=4, column=1, sticky="w", pady=4)

        ttk.Separator(body, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=8
        )

        ttk.Label(body, text="Tracked time").grid(
            row=6, column=0, sticky="w", padx=(0, 8), pady=4
        )
        ttk.Entry(
            body, textvariable=self.time_spent_var, width=18, state="readonly"
        ).grid(row=6, column=1, sticky="w", pady=4)

        ttk.Label(body, text="Last worked").grid(
            row=7, column=0, sticky="w", padx=(0, 8), pady=4
        )
        ttk.Entry(
            body, textvariable=self.last_worked_var, width=22, state="readonly"
        ).grid(row=7, column=1, sticky="w", pady=4)

        button_row = ttk.Frame(body)
        button_row.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(
            side="right"
        )
        ttk.Button(button_row, text="Save", command=self._on_save).pack(
            side="right", padx=(0, 8)
        )

        self._toggle_completion()
        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Control-Return>", lambda event: self._on_save())
        self.description_text.focus_set()
        self.grab_set()
        self.update_idletasks()
        self.minsize(max(700, self.winfo_width()), self.winfo_height())

    def _toggle_completion(self) -> None:
        state = "normal" if self.completed_var.get() else "disabled"
        self.completed_entry.configure(state=state)
        if not self.completed_var.get():
            self.completed_date_var.set("")

    def _on_save(self) -> None:
        description = " ".join(self.description_text.get("1.0", "end").split())
        if not description:
            messagebox.showerror(
                APP_TITLE, "Description cannot be empty.", parent=self
            )
            return
        created = self.created_var.get().strip()
        completed = self.completed_var.get()
        completed_date = self.completed_date_var.get().strip()

        if created and not is_date_string(created):
            messagebox.showerror(
                APP_TITLE, "Created date must be YYYY-MM-DD.", parent=self
            )
            return
        if completed and completed_date and not is_date_string(completed_date):
            messagebox.showerror(
                APP_TITLE, "Completed date must be YYYY-MM-DD.", parent=self
            )
            return
        if not completed:
            completed_date = ""

        self.result = {
            "description": description,
            "priority": self.priority_var.get().strip() or None,
            "creation_date": created or None,
            "completed": completed,
            "completion_date": completed_date or None,
        }
        self.destroy()


class TodoTimerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1180x720")
        self.root.minsize(920, 560)

        self.store = TodoStore()
        self.config_store = ConfigStore(APP_TITLE)
        self.config = self.config_store.load()
        self.sort_mode = self.config.sort_mode or "priority"
        self.idle_timeout_minutes = self._normalized_idle_timeout(
            self.config.idle_timeout_minutes
        )
        self.config.idle_timeout_minutes = self.idle_timeout_minutes
        self.last_app_activity_at = datetime.now()
        self.idle_dialog_open = False
        self.show_completed_var = tk.BooleanVar(
            value=self.config.show_completed
        )
        self.path_var = tk.StringVar(value="")
        self.archive_path_var = tk.StringVar(
            value=self.config.archive_file.strip()
        )
        self.status_var = tk.StringVar(value="Open a todo.txt file to begin.")
        self.config_status_var = tk.StringVar(value="")
        self.todo_status_var = tk.StringVar(value="")
        self.archive_status_var = tk.StringVar(value="")
        self.idle_status_var = tk.StringVar(value="")
        self.running_var = tk.StringVar(value="")

        self._build_styles()
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()
        self._bind_activity_tracking()

        if self.config.window_geometry:
            try:
                self.root.geometry(self.config.window_geometry)
            except Exception:
                pass

        last_file = self.config.last_file.strip()
        if last_file:
            try:
                self.open_file(last_file)
            except Exception as exc:
                self.status_var.set(f"Could not open saved file: {exc}")
        self.update_connection_status()
        self.recover_left_running_timer()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)

        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(
            label="Open todo.txt...",
            accelerator="Ctrl+O",
            command=self.choose_file,
        )
        file_menu.add_command(
            label="Create new todo.txt...", command=self.create_new_file
        )
        file_menu.add_command(
            label="Reload", accelerator="F5", command=self.reload_file
        )
        file_menu.add_command(
            label="Save", accelerator="Ctrl+S", command=self.save_file
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Open archive.txt...",
            command=self.choose_archive_file,
        )
        file_menu.add_command(
            label="Create new archive.txt...",
            command=self.create_new_archive_file,
        )
        file_menu.add_command(
            label="Archive completed tasks",
            accelerator="Ctrl+Shift+A",
            command=self.archive_completed_tasks,
        )
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)

        task_menu = tk.Menu(menu, tearoff=False)
        task_menu.add_command(
            label="Edit task...", accelerator="F2", command=self.edit_selected
        )
        task_menu.add_command(
            label="Toggle complete",
            accelerator="X",
            command=self.toggle_complete_selected,
        )
        task_menu.add_command(
            label="Delete task", accelerator="Del", command=self.delete_selected
        )
        task_menu.add_separator()
        task_menu.add_command(
            label="Start / stop timer",
            accelerator="Ctrl+T",
            command=self.toggle_timer_selected,
        )
        task_menu.add_separator()
        task_menu.add_command(
            label="Increase priority",
            accelerator="Alt+Up",
            command=self.increase_priority,
        )
        task_menu.add_command(
            label="Decrease priority",
            accelerator="Alt+Down",
            command=self.decrease_priority,
        )
        task_menu.add_command(
            label="Clear priority",
            accelerator="Alt+Left / Alt+Right",
            command=self.clear_priority,
        )
        task_menu.add_separator()
        task_menu.add_command(
            label="Open first link",
            accelerator="Ctrl+L",
            command=self.open_first_link,
        )
        menu.add_cascade(label="Task", menu=task_menu)

        sort_menu = tk.Menu(menu, tearoff=False)
        self.sort_var = tk.StringVar(value=self.sort_mode)
        for label, value in [
            ("Priority", "priority"),
            ("Created date", "created"),
            ("Tracked time", "worked"),
            ("Description", "description"),
            ("File order", "file"),
        ]:
            sort_menu.add_radiobutton(
                label=label,
                value=value,
                variable=self.sort_var,
                command=self.on_sort_changed,
            )
        menu.add_cascade(label="Sort", menu=sort_menu)

        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_checkbutton(
            label="Show completed tasks",
            variable=self.show_completed_var,
            command=self.refresh_tree,
        )
        view_menu.add_separator()
        view_menu.add_command(
            label="Idle timeout...",
            command=self.configure_idle_timeout,
        )
        menu.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(
            label="Generate report...",
            command=self.generate_report,
        )
        tools_menu.add_command(
            label="OpenAI key...",
            command=self.configure_openai_key,
        )
        menu.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menu)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        add_frame = ttk.Frame(outer)
        add_frame.grid(row=0, column=0, sticky="ew", pady=(0, 0))
        add_frame.columnconfigure(0, weight=1)

        ttk.Label(
            add_frame,
            text="<Ctrl+n> to add new task | <Ctrl+Enter> when done | <Esc> to cancel",
        ).grid(row=0, column=0, sticky="ew", padx=0, pady=0)

        self.quick_add_var = tk.StringVar()
        self.quick_add_entry = ttk.Entry(
            add_frame, textvariable=self.quick_add_var
        )
        self.quick_add_entry.grid(
            row=1, column=0, sticky="ew", padx=0, pady=(0, 5)
        )
        self.quick_add_entry.bind(
            "<Control-Return>", lambda event: self.quick_add() or "break"
        )
        self.quick_add_entry.bind(
            "<Escape>",
            lambda event: self.tree.focus_set() or "break",
        )

        table_frame = ttk.Frame(outer)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        shortcut_frame = ttk.Frame(outer)
        shortcut_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(
            shortcut_frame,
            text="[Ctrl+t] start/stop timer | [F2] edit entry | [Ctrl+l] open first link | [x] mark complete | [Del] delete task",
        ).grid(row=0, column=0, sticky="w")

        columns = ("done", "priority", "created", "lastworked", "spent", "task")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("done", text="✔️")
        self.tree.heading("priority", text="⚑")
        self.tree.heading("created", text="🌱")
        self.tree.heading("lastworked", text="⚒")
        self.tree.heading("spent", text="⏱️")
        self.tree.heading("task", text="Task")
        self.tree.column("done", width=20, anchor="center", stretch=False)
        self.tree.column("priority", width=20, anchor="center", stretch=False)
        self.tree.column("created", width=80, anchor="center", stretch=False)
        self.tree.column("lastworked", width=80, anchor="center", stretch=False)
        self.tree.column("spent", width=70, anchor="center", stretch=False)
        self.tree.column("task", width=600, anchor="w", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda event: self.edit_selected())

        y_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=y_scroll.set)

        self.tree.tag_configure("completed", foreground="#7a7a7a")
        self.tree.tag_configure("running", font=("Segoe UI", 9, "bold"))

        idle_status_frame = ttk.Frame(outer)
        idle_status_frame.grid(row=3, column=0, sticky="ew", pady=(3, 0))
        idle_status_frame.columnconfigure(0, weight=1)
        ttk.Label(
            idle_status_frame,
            textvariable=self.idle_status_var,
        ).grid(row=0, column=0, sticky="w")

        status_frame = ttk.Frame(outer)
        status_frame.grid(row=4, column=0, sticky="ew", pady=(3, 0))
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )

        connection_frame = ttk.Frame(outer)
        connection_frame.grid(row=5, column=0, sticky="ew", pady=(3, 0))
        connection_frame.columnconfigure(3, weight=1)
        self.config_status_label = ttk.Label(
            connection_frame,
            textvariable=self.config_status_var,
            cursor="hand2",
        )
        self.config_status_label.grid(row=0, column=0, sticky="w")
        self.config_status_label.bind(
            "<Button-1>",
            lambda event: self.open_status_file(
                self.config_store.path,
                "config.json",
            ),
        )
        self.config_status_tooltip = ToolTip(self.config_status_label)
        self.todo_status_label = ttk.Label(
            connection_frame,
            textvariable=self.todo_status_var,
            cursor="hand2",
        )
        self.todo_status_label.grid(row=0, column=1, sticky="w", padx=(3, 0))
        self.todo_status_label.bind(
            "<Button-1>",
            lambda event: self.open_status_file(
                self.path_var.get().strip(),
                "todo.txt",
            ),
        )
        self.todo_status_tooltip = ToolTip(self.todo_status_label)
        self.archive_status_label = ttk.Label(
            connection_frame,
            textvariable=self.archive_status_var,
            cursor="hand2",
        )
        self.archive_status_label.grid(row=0, column=2, sticky="w", padx=(3, 0))
        self.archive_status_label.bind(
            "<Button-1>",
            lambda event: self.open_status_file(
                self.archive_path_var.get().strip(),
                "archive.txt",
            ),
        )
        self.archive_status_tooltip = ToolTip(self.archive_status_label)

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-o>", lambda event: self.choose_file())
        self.root.bind_all("<Control-s>", lambda event: self.save_file())
        for sequence in ("<Control-Shift-a>", "<Control-Shift-A>"):
            self.root.bind_all(
                sequence,
                lambda event: self.archive_completed_tasks() or "break",
            )
        self.root.bind_all("<F5>", lambda event: self.reload_file())
        self.root.bind_all(
            "<Control-n>",
            lambda event: self.quick_add_entry.focus_set() or "break",
        )
        self.tree.bind("<F2>", lambda event: self.edit_selected() or "break")
        self.tree.bind(
            "<Delete>", lambda event: self.delete_selected() or "break"
        )
        self.tree.bind(
            "<Alt-Up>", lambda event: self.increase_priority() or "break"
        )
        self.tree.bind(
            "<Alt-Down>", lambda event: self.decrease_priority() or "break"
        )
        self.root.bind_all("<Alt-Left>", lambda event: self.clear_priority())
        self.root.bind_all("<Alt-Right>", lambda event: self.clear_priority())
        self.tree.bind(
            "x",
            lambda event: (
                self.toggle_complete_selected()
                if self._tree_has_focus()
                else None
            )
            or "break",
        )
        self.tree.bind(
            "<Control-t>", lambda event: self.toggle_timer_selected() or "break"
        )
        self.root.bind_all("<Control-l>", lambda event: self.open_first_link())

    def _bind_activity_tracking(self) -> None:
        for sequence in (
            "<KeyPress>",
            "<ButtonPress>",
            "<ButtonRelease>",
            "<Motion>",
            "<MouseWheel>",
        ):
            self.root.bind_all(
                sequence,
                lambda event: self._record_app_activity(),
                add="+",
            )

    def _record_app_activity(self) -> None:
        self.last_app_activity_at = datetime.now()

    def _tree_has_focus(self) -> bool:
        focus = self.root.focus_get()
        return focus is self.tree

    def show_about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            (
                "TodoTimerTXT\n\n"
                "A fresh Windows-friendly todo.txt app with built-in task timing.\n\n"
                "Timer metadata is stored directly in each line using:\n"
                "  spent:HH:MM:SS\n"
                "  lastworked:YYYY-MM-DD-HH-MM-SS\n"
                "  active:YYYY-MM-DD-HH-MM-SS\n\n"
                "Shortcuts:\n"
                "  Ctrl+O open file\n"
                "  F2 edit\n"
                "  Alt+Up / Alt+Down change priority\n"
                "  Ctrl+T start/stop timer\n"
                "  Ctrl+L open first link\n"
                "  Ctrl+Shift+A archive completed tasks\n"
                "  Tools > Generate report creates an OpenAI summary from archive.txt\n"
            ),
            parent=self.root,
        )

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open todo.txt",
            filetypes=[
                ("todo.txt files", "*.txt"),
                ("Text files", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.open_file(path)

    def create_new_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Create new todo.txt",
            defaultextension=".txt",
            initialfile="todo.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        Path(path).write_text("", encoding="utf-8")
        self.open_file(path)

    def choose_archive_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open archive.txt",
            filetypes=[
                ("archive.txt files", "*.txt"),
                ("Text files", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.set_archive_file(path)

    def create_new_archive_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Create new archive.txt",
            defaultextension=".txt",
            initialfile="archive.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        Path(path).write_text("", encoding="utf-8")
        self.set_archive_file(path)

    def set_archive_file(self, path: str) -> None:
        archive_path = str(Path(path))
        self.archive_path_var.set(archive_path)
        self.config.archive_file = archive_path
        try:
            self.config_store.save(self.config)
        except Exception as exc:
            self.update_connection_status()
            messagebox.showerror(
                APP_TITLE,
                f"Could not save archive file setting:\n{exc}",
                parent=self.root,
            )
            return
        self.update_connection_status()
        self.status_var.set(f"Archive file set to {archive_path}")

    def open_file(self, path: str) -> None:
        self.store.load(path)
        self.path_var.set(str(Path(path)))
        self.config.last_file = str(Path(path))
        config_save_error = None
        try:
            self.config_store.save(self.config)
        except Exception as exc:
            config_save_error = exc
        self.update_connection_status()
        self.refresh_tree()
        if config_save_error:
            self.status_var.set(
                f"Loaded file, but could not save config: {config_save_error}"
            )
        else:
            self.status_var.set(
                f"Loaded {len(self.store.items)} task(s) from {path}"
            )

    def recover_left_running_timer(self) -> None:
        if self.store.path is None:
            return

        try:
            closed_at = parse_timestamp(self.config.last_closed_at.strip())
        except TodoFormatError:
            self.clear_last_closed_at()
            return
        if closed_at is None:
            return

        running = self.store.running_items()
        if not running:
            self.clear_last_closed_at()
            return

        item = running[0]
        if item.timer_started_at is None or closed_at < item.timer_started_at:
            self.clear_last_closed_at()
            return

        opened_at = datetime.now()
        if closed_at > opened_at:
            closed_at = opened_at

        dialog = RunningTimerRecoveryDialog(
            self.root,
            item,
            closed_at,
            opened_at,
        )
        self.root.wait_window(dialog)

        try:
            if dialog.result == "stop_at_close":
                self.store.stop_timer(item.id, now=closed_at)
                self.store.save()
                self.refresh_tree(select_item_id=item.id)
                self.status_var.set(
                    "Timer was stopped at the previous app close time."
                )
            else:
                self.status_var.set("Timer left running from previous session.")
            self.clear_last_closed_at()
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not recover the running timer:\n{exc}",
                parent=self.root,
            )

    def clear_last_closed_at(self) -> None:
        self.config.last_closed_at = ""
        try:
            self.config_store.save(self.config)
        except Exception:
            pass

    def reload_file(self) -> None:
        if not self.store.path:
            self.choose_file()
            return
        self.open_file(str(self.store.path))

    def save_file(self) -> None:
        if not self.store.path:
            self.choose_file()
            return
        try:
            self.store.save()
            self.status_var.set(
                f"Saved {len(self.store.items)} task(s) to {self.store.path}"
            )
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Could not save file:\n{exc}", parent=self.root
            )

    def archive_completed_tasks(self) -> None:
        try:
            self.ensure_file_loaded()
            archive_path = self.ensure_archive_file_loaded()
            completed_count = len(
                [item for item in self.store.items if item.completed]
            )
            if completed_count == 0:
                messagebox.showinfo(
                    APP_TITLE,
                    "There are no completed tasks to archive.",
                    parent=self.root,
                )
                return
            if not messagebox.askyesno(
                APP_TITLE,
                (
                    f"Archive {completed_count} completed task(s) to:\n\n"
                    f"{archive_path}"
                ),
                parent=self.root,
            ):
                return
            archived_count = self.store.archive_completed(archive_path)
            self.refresh_tree()
            self.update_connection_status()
            self.status_var.set(
                f"Archived {archived_count} completed task(s) to {archive_path}"
            )
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not archive completed tasks:\n{exc}",
                parent=self.root,
            )

    def configure_openai_key(self) -> None:
        dialog = OpenAIKeyDialog(self.root, self.config.openai_api_key)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self.config.openai_api_key = dialog.result
        try:
            self.config_store.save(self.config)
            self.update_connection_status()
            self.status_var.set("OpenAI key saved.")
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not save OpenAI key:\n{exc}",
                parent=self.root,
            )

    def generate_report(self) -> None:
        if not self.config.openai_api_key.strip():
            messagebox.showinfo(
                APP_TITLE,
                "Set your OpenAI key before generating a report.",
                parent=self.root,
            )
            self.configure_openai_key()
            if not self.config.openai_api_key.strip():
                return

        archive_path = self.archive_path_var.get().strip()
        if not archive_path:
            messagebox.showinfo(
                APP_TITLE,
                "Choose or create an archive.txt file before generating a report.",
                parent=self.root,
            )
            try:
                archive_path = self.ensure_archive_file_loaded()
            except Exception:
                return
        archive_file = Path(archive_path)
        if not archive_file.exists():
            messagebox.showerror(
                APP_TITLE,
                f"Archive file does not exist:\n{archive_file}",
                parent=self.root,
            )
            self.update_connection_status()
            return

        dialog = ReportDateDialog(self.root)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        start_date, end_date = dialog.result

        try:
            tasks = self.report_tasks_in_range(
                archive_file, start_date, end_date
            )
            if not tasks:
                messagebox.showinfo(
                    APP_TITLE,
                    (
                        "No archived or active todo.txt tasks were found between "
                        f"{start_date} and {end_date}."
                    ),
                    parent=self.root,
                )
                return
            self.status_var.set("Generating report with OpenAI...")
            self.root.update_idletasks()
            report = self.generate_openai_report(tasks, start_date, end_date)
            ReportResultDialog(
                self.root,
                f"Report {start_date} to {end_date}",
                report,
            )
            self.status_var.set(
                f"Generated report for {len(tasks)} task(s)."
            )
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not generate report:\n{exc}",
                parent=self.root,
            )
            self.status_var.set("Report generation failed.")

    def report_tasks_in_range(
        self,
        archive_path: Path,
        start_date: str,
        end_date: str,
    ) -> list[ReportTask]:
        tasks = self.completed_archive_tasks_in_range(
            archive_path, start_date, end_date
        )
        tasks.extend(self.active_todo_report_tasks_in_range(start_date, end_date))
        return sorted(
            tasks,
            key=lambda task: (
                task.activity_date,
                task.status,
                task.item.description.casefold(),
            ),
        )

    def completed_archive_tasks_in_range(
        self,
        archive_path: Path,
        start_date: str,
        end_date: str,
    ) -> list[ReportTask]:
        tasks: list[ReportTask] = []
        for index, line in enumerate(
            archive_path.read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip():
                continue
            item = parse_todo_line(line, line_index=index)
            if (
                item.completed
                and item.completion_date
                and start_date <= item.completion_date <= end_date
            ):
                tasks.append(
                    ReportTask(
                        item=item,
                        status="completed",
                        source="archive.txt",
                        activity_date=item.completion_date,
                    )
                )
        return tasks

    def active_todo_report_tasks_in_range(
        self,
        start_date: str,
        end_date: str,
    ) -> list[ReportTask]:
        tasks: list[ReportTask] = []
        if self.store.path is None:
            return tasks

        for item in self.store.items:
            if (
                item.completed
                and item.completion_date
                and start_date <= item.completion_date <= end_date
            ):
                tasks.append(
                    ReportTask(
                        item=item,
                        status="completed",
                        source="todo.txt",
                        activity_date=item.completion_date,
                    )
                )
                continue

            activity_date = self.in_progress_activity_date(
                item, start_date, end_date
            )
            if activity_date is not None:
                tasks.append(
                    ReportTask(
                        item=item,
                        status="in progress",
                        source="todo.txt",
                        activity_date=activity_date,
                    )
                )
        return tasks

    def in_progress_activity_date(
        self,
        item: TodoItem,
        start_date: str,
        end_date: str,
    ) -> str | None:
        if item.completed:
            return None

        candidates: list[str] = []
        if item.last_worked_at is not None:
            candidates.append(item.last_worked_at.strftime("%Y-%m-%d"))
        if item.timer_started_at is not None:
            candidates.append(item.timer_started_at.strftime("%Y-%m-%d"))
            today = self.today_string()
            if start_date <= today <= end_date:
                candidates.append(today)

        in_range = [
            candidate
            for candidate in candidates
            if start_date <= candidate <= end_date
        ]
        return max(in_range) if in_range else None

    def generate_openai_report(
        self,
        tasks: list[ReportTask],
        start_date: str,
        end_date: str,
    ) -> str:
        completed_lines = self.format_report_task_lines(
            [task for task in tasks if task.status == "completed"]
        )
        in_progress_lines = self.format_report_task_lines(
            [task for task in tasks if task.status == "in progress"]
        )
        prompt = (
            "Generate a concise work report from these todo.txt tasks.\n\n"
            f"Date range: {start_date} through {end_date}\n"
            f"Completed tasks:\n{completed_lines or '- None'}\n\n"
            f"In-progress tasks worked during the date range:\n"
            f"{in_progress_lines or '- None'}\n\n"
            "Write a polished report with these sections:\n"
            "1. Summary\n"
            "2. Completed Work\n"
            "3. In Progress\n"
            "4. Themes and Progress\n"
            "5. Follow-ups or Risks, if any\n"
            "Keep it practical and grounded only in the task list."
        )
        payload = {
            "model": REPORT_MODEL,
            "instructions": (
                "You write clear status reports from completed and in-progress "
                "task lists. Do not invent facts that are not implied by the "
                "tasks."
            ),
            "input": prompt,
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            OPENAI_RESPONSES_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key.strip()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Could not reach OpenAI API: {exc.reason}") from exc

        output_text = response_data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        parts: list[str] = []
        for item in response_data.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        report = "\n".join(parts).strip()
        if not report:
            raise RuntimeError("OpenAI response did not include report text.")
        return report

    def format_report_task_lines(self, tasks: list[ReportTask]) -> str:
        return "\n".join(
            f"- {task.activity_date}: {task.item.description} "
            f"[{task.source}; {task.status}; "
            f"tracked {format_duration(task.item.total_elapsed_seconds())}]"
            for task in tasks
        )

    def quick_add(self) -> None:
        text = self.quick_add_var.get().strip()
        if not text:
            return
        try:
            self.ensure_file_loaded()
            item = self.store.add_from_text(text)
            if not item.creation_date and not item.completed:
                item.creation_date = self.today_string()
            self.store.save()
            self.quick_add_var.set("")
            self.refresh_tree(select_item_id=item.id)
            self.tree.focus_set()
            self.status_var.set("Task added.")
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Could not add task:\n{exc}", parent=self.root
            )

    def selected_item(self) -> TodoItem | None:
        selection = self.tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        try:
            return self.store.get_by_id(item_id)
        except Exception:
            return None

    def edit_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        dialog = TaskDialog(self.root, "Edit task", item=item)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        try:
            self.store.update_item(
                item.id,
                description=dialog.result["description"],
                priority=dialog.result.get("priority") or None,
                creation_date=dialog.result.get("creation_date") or None,
                completed=bool(dialog.result.get("completed")),
                completion_date=dialog.result.get("completion_date") or None,
            )
            if item.completed:
                item.priority = None
                if not item.completion_date:
                    item.completion_date = self.today_string()
            else:
                item.completion_date = None
            self.store.save()
            self.refresh_tree(select_item_id=item.id)
            self.status_var.set("Task updated.")
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Could not update task:\n{exc}", parent=self.root
            )

    def toggle_complete_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        try:
            self.store.toggle_complete(item.id, today=self.today_string())
            self.store.save()
            self.refresh_tree(select_item_id=item.id)
            self.status_var.set("Completion toggled.")
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not toggle completion:\n{exc}",
                parent=self.root,
            )

    def delete_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Delete this task?\n\n{item.description}",
            parent=self.root,
        ):
            return
        try:
            self.store.delete_item(item.id)
            self.store.save()
            self.refresh_tree()
            self.status_var.set("Task deleted.")
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Could not delete task:\n{exc}", parent=self.root
            )

    def toggle_timer_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        try:
            self.ensure_file_loaded()
            if item.completed:
                messagebox.showinfo(
                    APP_TITLE,
                    "Reopen the task before timing it.",
                    parent=self.root,
                )
                return
            if item.timer_started_at is None:
                stopped = self.store.stop_all_timers(except_item_id=item.id)
                self.store.start_timer(item.id)
                self.store.save()
                if stopped:
                    self.status_var.set(
                        "Started timer and stopped another running timer."
                    )
                else:
                    self.status_var.set("Timer started.")
            else:
                self.store.stop_timer(item.id)
                self.store.save()
                self.status_var.set("Timer stopped.")
            self.refresh_tree(select_item_id=item.id)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Could not toggle timer:\n{exc}", parent=self.root
            )

    def increase_priority(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        try:
            self.store.adjust_priority(item.id, -1)
            self.store.save()
            self.refresh_tree(select_item_id=item.id)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not change priority:\n{exc}",
                parent=self.root,
            )

    def decrease_priority(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        try:
            self.store.adjust_priority(item.id, 1)
            self.store.save()
            self.refresh_tree(select_item_id=item.id)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not change priority:\n{exc}",
                parent=self.root,
            )

    def clear_priority(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        try:
            self.store.clear_priority(item.id)
            self.store.save()
            self.refresh_tree(select_item_id=item.id)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Could not clear priority:\n{exc}", parent=self.root
            )

    def open_first_link(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        url = extract_first_url(item.description)
        if not url:
            messagebox.showinfo(
                APP_TITLE,
                "No URL found in the selected task.",
                parent=self.root,
            )
            return
        webbrowser.open(url)
        self.status_var.set(f"Opened {url}")

    def open_status_file(self, path: str | Path, label: str) -> None:
        if not path:
            messagebox.showinfo(
                APP_TITLE,
                f"No {label} file is configured.",
                parent=self.root,
            )
            return
        file_path = Path(path)
        if not file_path.exists():
            messagebox.showinfo(
                APP_TITLE,
                f"{label} does not exist yet:\n{file_path}",
                parent=self.root,
            )
            self.update_connection_status()
            return
        try:
            self.open_file_in_default_app(file_path)
            self.status_var.set(f"Opened {label}: {file_path}")
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not open {label}:\n{exc}",
                parent=self.root,
            )

    @staticmethod
    def open_file_in_default_app(path: Path) -> None:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def configure_idle_timeout(self) -> None:
        minutes = simpledialog.askinteger(
            "Idle timeout",
            "Stop a running timer after how many idle minutes?",
            parent=self.root,
            initialvalue=self.idle_timeout_minutes,
            minvalue=1,
            maxvalue=1440,
        )
        if minutes is None:
            return
        self.idle_timeout_minutes = self._normalized_idle_timeout(minutes)
        self.config.idle_timeout_minutes = self.idle_timeout_minutes
        try:
            self.config_store.save(self.config)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not save idle timeout:\n{exc}",
                parent=self.root,
            )
            return
        self.status_var.set(
            f"Idle timeout set to {self.idle_timeout_minutes} minute(s)."
        )

    def on_sort_changed(self) -> None:
        self.sort_mode = self.sort_var.get()
        self.refresh_tree()

    def refresh_tree(self, select_item_id: str | None = None) -> None:
        current_selection = select_item_id or (
            self.tree.selection()[0] if self.tree.selection() else None
        )
        for child in self.tree.get_children():
            self.tree.delete(child)

        show_completed = self.show_completed_var.get()
        items = list(
            self.store.iter_sorted(
                self.sort_mode, show_completed=show_completed
            )
        )
        for item in items:
            tags: tuple[str, ...] = tuple(
                tag
                for tag, active in [
                    ("completed", item.completed),
                    ("running", item.timer_started_at is not None),
                ]
                if active
            )
            last_worked = (
                item.last_worked_at.strftime("%Y-%m-%d")
                if item.last_worked_at
                else "not started"
            )
            spent = format_duration(item.total_elapsed_seconds())
            task_text = item.description
            if item.projects:
                task_text = f"{task_text}"
            self.tree.insert(
                "",
                "end",
                iid=item.id,
                values=(
                    "x" if item.completed else "",
                    item.priority or "",
                    item.creation_date or "",
                    last_worked,
                    spent + (" ▶" if item.timer_started_at else ""),
                    task_text,
                ),
                tags=tags,
            )

        if current_selection and self.tree.exists(current_selection):
            self.tree.selection_set(current_selection)
            self.tree.focus(current_selection)
            self.tree.see(current_selection)

        total = len(self.store.items)
        complete = len([item for item in self.store.items if item.completed])
        incomplete = total - complete
        self.status_var.set(
            f"Tasks: {total} total | {incomplete} incomplete | {complete} complete | Sort: {self.sort_mode}"
        )
        self._update_running_status()

    def _update_running_status(self) -> None:
        running = self.store.running_items()
        if not running:
            self.running_var.set("")
            return
        item = running[0]
        self.running_var.set(
            f"Running: {format_duration(item.total_elapsed_seconds())} - {item.description[:70]}"
        )

    def update_connection_status(self) -> None:
        config_ok = self.config_store.loaded
        self.config_status_var.set(
            f"{chr(0x2713) if config_ok else '!'} Config: "
            f"{self.config_store.path.name}"
        )
        self.config_status_tooltip.set_text(
            f"Config: {self.config_store.path}\n{self.config_store.load_message}"
        )
        self.config_status_label.configure(
            foreground="#107c10" if config_ok else "#8a6d00"
        )

        todo_path = self.path_var.get().strip()
        todo_ok = bool(todo_path) and Path(todo_path).exists()
        todo_name = Path(todo_path).name if todo_path else "not loaded"
        self.todo_status_var.set(
            f"{chr(0x2713) if todo_ok else '!'} todo.txt: {todo_name}"
        )
        self.todo_status_tooltip.set_text(
            f"todo.txt: {todo_path if todo_path else 'not loaded'}"
        )
        self.todo_status_label.configure(
            foreground="#107c10" if todo_ok else "#8a6d00"
        )

        archive_path = self.archive_path_var.get().strip()
        archive_ok = bool(archive_path) and Path(archive_path).exists()
        archive_name = Path(archive_path).name if archive_path else "not set"
        self.archive_status_var.set(
            f"{chr(0x2713) if archive_ok else '!'} archive.txt: {archive_name}"
        )
        self.archive_status_tooltip.set_text(
            f"archive.txt: {archive_path if archive_path else 'not set'}"
        )
        self.archive_status_label.configure(
            foreground="#107c10" if archive_ok else "#8a6d00"
        )

    def _update_idle_status(self) -> None:
        if not self.store.running_items():
            self.idle_status_var.set("")
            return
        idle_seconds = self._current_idle_seconds()
        threshold_seconds = self.idle_timeout_minutes * 60
        remaining_seconds = max(0, threshold_seconds - idle_seconds)
        self.idle_status_var.set(
            "Idle: "
            f"{format_duration(idle_seconds)} | "
            f"Pauses in: {format_duration(remaining_seconds)}"
        )

    def _current_idle_seconds(self) -> int:
        system_idle_seconds = get_system_idle_seconds()
        if system_idle_seconds is not None:
            return system_idle_seconds
        return max(0, int((datetime.now() - self.last_app_activity_at).total_seconds()))

    def _check_idle_timer(self) -> None:
        if self.idle_dialog_open:
            return
        running = self.store.running_items()
        if not running:
            return

        idle_seconds = self._current_idle_seconds()
        threshold_seconds = self.idle_timeout_minutes * 60
        if idle_seconds < threshold_seconds:
            return

        item = running[0]
        if item.timer_started_at is None:
            return

        detected_at = datetime.now()
        last_activity_at = detected_at - timedelta(seconds=idle_seconds)
        elapsed_to_last_activity = max(
            0,
            int((last_activity_at - item.timer_started_at).total_seconds()),
        )
        event = IdleTimerEvent(
            item_id=item.id,
            description=item.description,
            detected_at=detected_at,
            last_activity_at=last_activity_at,
            running_seconds_at_detection=item.total_elapsed_seconds(detected_at),
            running_seconds_at_last_activity=(
                item.time_spent_seconds + elapsed_to_last_activity
            ),
            idle_seconds=idle_seconds,
        )

        self.store.stop_timer(item.id, now=detected_at)
        self.store.save()
        self.refresh_tree(select_item_id=item.id)
        self.status_var.set("Timer stopped after keyboard/mouse inactivity.")
        self._show_idle_timer_dialog(event)

    def _show_idle_timer_dialog(self, event: IdleTimerEvent) -> None:
        self.idle_dialog_open = True
        try:
            dialog = IdleTimerDialog(self.root, event)
            self.root.wait_window(dialog)
            self._handle_idle_timer_choice(event, dialog.result)
        finally:
            self.idle_dialog_open = False
            self._record_app_activity()

    def _handle_idle_timer_choice(
        self, event: IdleTimerEvent, choice: str
    ) -> None:
        try:
            item = self.store.get_by_id(event.item_id)
        except KeyError:
            self.status_var.set("Idle timer task no longer exists.")
            return

        try:
            if choice == "reset":
                item.time_spent_seconds = event.running_seconds_at_last_activity
                item.last_worked_at = event.last_activity_at
                item.timer_started_at = None
                self.status_var.set("Timer reset to the last activity time.")
            elif choice == "restart":
                if item.completed:
                    messagebox.showinfo(
                        APP_TITLE,
                        "Reopen the task before timing it.",
                        parent=self.root,
                    )
                    return
                self.store.start_timer(item.id)
                self.status_var.set("Timer restarted.")
            else:
                self.status_var.set("Timer left stopped after inactivity.")
            self.store.save()
            self.refresh_tree(select_item_id=item.id)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Could not update idle timer action:\n{exc}",
                parent=self.root,
            )

    def _tick(self) -> None:
        self._check_idle_timer()
        if self.store.running_items():
            selected = (
                self.tree.selection()[0] if self.tree.selection() else None
            )
            self.refresh_tree(select_item_id=selected)
        else:
            self._update_running_status()
        self.update_connection_status()
        self._update_idle_status()
        self.root.after(1000, self._tick)

    def ensure_file_loaded(self) -> None:
        if not self.store.path:
            raise RuntimeError("Open or create a todo.txt file first.")

    def ensure_archive_file_loaded(self) -> str:
        archive_path = self.archive_path_var.get().strip()
        if archive_path:
            path = Path(archive_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
            self.update_connection_status()
            return str(path)

        path = filedialog.asksaveasfilename(
            title="Choose or create archive.txt",
            defaultextension=".txt",
            initialfile="archive.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            raise RuntimeError("Choose or create an archive.txt file first.")
        if not Path(path).exists():
            Path(path).write_text("", encoding="utf-8")
        self.set_archive_file(path)
        archive_path = self.archive_path_var.get().strip()
        if not archive_path:
            raise RuntimeError("Choose or create an archive.txt file first.")
        return archive_path

    @staticmethod
    def today_string() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _normalized_idle_timeout(value: object) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return DEFAULT_IDLE_TIMEOUT_MINUTES
        return max(1, minutes)

    def on_close(self) -> None:
        now = datetime.now()
        self.config.last_file = self.path_var.get().strip()
        self.config.archive_file = self.archive_path_var.get().strip()
        self.config.openai_api_key = self.config.openai_api_key.strip()
        self.config.last_closed_at = (
            format_timestamp(now) if self.store.running_items() else ""
        )
        self.config.window_geometry = self.root.geometry()
        self.config.sort_mode = self.sort_mode
        self.config.show_completed = self.show_completed_var.get()
        self.config.idle_timeout_minutes = self.idle_timeout_minutes
        try:
            self.config_store.save(self.config)
            self.config_store.loaded = True
            self.config_store.load_message = "Saved config."
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    try:
        TodoTimerApp().run()
    except TodoFormatError as exc:
        messagebox.showerror(APP_TITLE, str(exc))

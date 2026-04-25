from __future__ import annotations

import ctypes
from dataclasses import dataclass
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from pathlib import Path
from datetime import datetime, timedelta
import webbrowser

from todo_core import (
    AppConfig,
    ConfigStore,
    TodoFormatError,
    TodoItem,
    TodoStore,
    extract_first_url,
    format_duration,
    is_date_string,
)

APP_TITLE = "TodoTimerTXT"
DEFAULT_IDLE_TIMEOUT_MINUTES = 10


@dataclass(slots=True)
class IdleTimerEvent:
    item_id: str
    description: str
    detected_at: datetime
    last_activity_at: datetime
    running_seconds_at_detection: int
    running_seconds_at_last_activity: int
    idle_seconds: int


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
        self.status_var = tk.StringVar(value="Open a todo.txt file to begin.")
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
        shortcut_frame.grid(row=2, column=0, sticky="ew", pady=(8, 8))
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

        status_frame = ttk.Frame(outer)
        status_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(status_frame, textvariable=self.idle_status_var).grid(
            row=0, column=1, sticky="e"
        )
        ttk.Label(status_frame, textvariable=self.running_var).grid(
            row=0, column=2, sticky="e", padx=(12, 0)
        )

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-o>", lambda event: self.choose_file())
        self.root.bind_all("<Control-s>", lambda event: self.save_file())
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

    def open_file(self, path: str) -> None:
        self.store.load(path)
        self.path_var.set(str(Path(path)))
        self.config.last_file = str(Path(path))
        self.refresh_tree()
        self.status_var.set(
            f"Loaded {len(self.store.items)} task(s) from {path}"
        )

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
        self._update_idle_status()
        self.root.after(1000, self._tick)

    def ensure_file_loaded(self) -> None:
        if not self.store.path:
            raise RuntimeError("Open or create a todo.txt file first.")

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
        self.config.last_file = self.path_var.get().strip()
        self.config.window_geometry = self.root.geometry()
        self.config.sort_mode = self.sort_mode
        self.config.show_completed = self.show_completed_var.get()
        self.config.idle_timeout_minutes = self.idle_timeout_minutes
        try:
            self.config_store.save(self.config)
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

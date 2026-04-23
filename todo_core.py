from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Iterable, Optional

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
PRIORITY_RE = re.compile(r"^\(([A-Z])\)$")
URL_RE = re.compile(r"https?://\S+")

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d-%H-%M-%S"


class TodoFormatError(ValueError):
    pass


def is_date_string(value: str | None) -> bool:
    if not value:
        return False
    return bool(DATE_RE.match(value))


def parse_date_string(value: str | None) -> str | None:
    if not value:
        return None
    if not is_date_string(value):
        raise TodoFormatError(f"Invalid date: {value!r}. Expected YYYY-MM-DD.")
    datetime.strptime(value, DATE_FMT)
    return value


def format_timestamp(value: datetime) -> str:
    return value.strftime(DATETIME_FMT)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    if not DATETIME_RE.match(value):
        raise TodoFormatError(f"Invalid timestamp: {value!r}. Expected YYYY-MM-DD-HH-MM-SS.")
    return datetime.strptime(value, DATETIME_FMT)


def parse_duration(value: str | None) -> int:
    """Parses either HH:MM:SS or <seconds>s."""
    if not value:
        return 0
    if value.endswith("s") and value[:-1].isdigit():
        return max(0, int(value[:-1]))
    parts = value.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise TodoFormatError(
            f"Invalid duration: {value!r}. Expected HH:MM:SS or <seconds>s."
        )
    hours, minutes, seconds = map(int, parts)
    return max(0, hours * 3600 + minutes * 60 + seconds)


def format_duration(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@dataclass(slots=True)
class TodoItem:
    description: str = ""
    completed: bool = False
    priority: str | None = None
    creation_date: str | None = None
    completion_date: str | None = None
    time_spent_seconds: int = 0
    timer_started_at: datetime | None = None
    last_worked_at: datetime | None = None
    line_index: int = -1
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if self.priority is not None:
            self.priority = self.priority.upper()
            if len(self.priority) != 1 or not self.priority.isalpha():
                raise TodoFormatError(f"Invalid priority {self.priority!r}. Expected A-Z.")
        self.creation_date = parse_date_string(self.creation_date)
        self.completion_date = parse_date_string(self.completion_date)
        self.time_spent_seconds = max(0, int(self.time_spent_seconds))
        if isinstance(self.timer_started_at, str):
            self.timer_started_at = parse_timestamp(self.timer_started_at)
        if isinstance(self.last_worked_at, str):
            self.last_worked_at = parse_timestamp(self.last_worked_at)
        self.description = normalize_single_line(self.description)

    @property
    def projects(self) -> list[str]:
        return [token for token in self.description.split() if token.startswith("+") and len(token) > 1]

    @property
    def contexts(self) -> list[str]:
        return [token for token in self.description.split() if token.startswith("@") and len(token) > 1]

    def raw_line(self) -> str:
        return serialize_todo_line(self)

    def total_elapsed_seconds(self, now: datetime | None = None) -> int:
        total = self.time_spent_seconds
        if self.timer_started_at is not None:
            current = now or datetime.now()
            elapsed = int((current - self.timer_started_at).total_seconds())
            total += max(0, elapsed)
        return total

    def start_timer(self, now: datetime | None = None) -> None:
        if self.timer_started_at is None:
            self.timer_started_at = now or datetime.now()

    def stop_timer(self, now: datetime | None = None) -> int:
        if self.timer_started_at is None:
            return 0
        current = now or datetime.now()
        elapsed = max(0, int((current - self.timer_started_at).total_seconds()))
        self.time_spent_seconds += elapsed
        self.last_worked_at = current
        self.timer_started_at = None
        return elapsed


@dataclass(slots=True)
class AppConfig:
    last_file: str = ""
    window_geometry: str = ""
    sort_mode: str = "priority"
    show_completed: bool = True


class ConfigStore:
    def __init__(self, app_name: str = "TodoTimerTXT") -> None:
        self.app_name = app_name
        self.path = self._default_path(app_name)

    @staticmethod
    def _default_path(app_name: str) -> Path:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / ".config"
        folder = base / app_name
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "config.json"

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AppConfig(**data)
        except Exception:
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        self.path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


class TodoStore:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.items: list[TodoItem] = []

    def load(self, path: str | os.PathLike[str]) -> list[TodoItem]:
        file_path = Path(path)
        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("", encoding="utf-8")
        raw_lines = file_path.read_text(encoding="utf-8").splitlines()
        self.path = file_path
        self.items = [parse_todo_line(line, line_index=index) for index, line in enumerate(raw_lines) if line.strip()]
        return self.items

    def save(self) -> None:
        if self.path is None:
            raise RuntimeError("No todo.txt file is loaded.")
        serialized = "\n".join(serialize_todo_line(item) for item in self.items)
        if serialized:
            serialized += "\n"
        self._atomic_write(self.path, serialized)
        for index, item in enumerate(self.items):
            item.line_index = index

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
            handle.write(content)
            temp_name = handle.name
        Path(temp_name).replace(path)

    def add_from_text(self, text: str) -> TodoItem:
        item = parse_todo_line(text, line_index=len(self.items))
        self.items.append(item)
        return item

    def add_item(self, item: TodoItem) -> TodoItem:
        item.line_index = len(self.items)
        self.items.append(item)
        return item

    def get_by_id(self, item_id: str) -> TodoItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def delete_item(self, item_id: str) -> None:
        self.items = [item for item in self.items if item.id != item_id]
        for index, item in enumerate(self.items):
            item.line_index = index

    def toggle_complete(self, item_id: str, today: str | None = None) -> TodoItem:
        item = self.get_by_id(item_id)
        if item.completed:
            item.completed = False
            item.completion_date = None
        else:
            item.stop_timer()
            item.completed = True
            item.completion_date = today or datetime.now().strftime(DATE_FMT)
            item.priority = None
        return item

    def set_priority(self, item_id: str, priority: str | None) -> TodoItem:
        item = self.get_by_id(item_id)
        if priority is None or priority == "":
            item.priority = None
        else:
            p = priority.upper()
            if len(p) != 1 or not p.isalpha():
                raise TodoFormatError("Priority must be A-Z or blank.")
            item.priority = p
        return item

    def adjust_priority(self, item_id: str, direction: int) -> TodoItem:
        item = self.get_by_id(item_id)
        if direction == 0:
            return item
        if item.priority is None:
            item.priority = "A" if direction < 0 else "Z"
            return item
        code = ord(item.priority)
        if direction < 0:
            code = max(ord("A"), code - 1)
        else:
            code = min(ord("Z"), code + 1)
        item.priority = chr(code)
        return item

    def clear_priority(self, item_id: str) -> TodoItem:
        item = self.get_by_id(item_id)
        item.priority = None
        return item

    def update_item(self, item_id: str, **changes: object) -> TodoItem:
        item = self.get_by_id(item_id)
        for key, value in changes.items():
            if not hasattr(item, key):
                raise AttributeError(key)
            setattr(item, key, value)
        item.__post_init__()
        return item

    def stop_all_timers(self, except_item_id: str | None = None) -> list[TodoItem]:
        changed: list[TodoItem] = []
        for item in self.items:
            if item.id == except_item_id:
                continue
            if item.timer_started_at is not None:
                item.stop_timer()
                changed.append(item)
        return changed

    def start_timer(self, item_id: str) -> TodoItem:
        self.stop_all_timers(except_item_id=item_id)
        item = self.get_by_id(item_id)
        item.start_timer()
        return item

    def stop_timer(self, item_id: str) -> TodoItem:
        item = self.get_by_id(item_id)
        item.stop_timer()
        return item

    def running_items(self) -> list[TodoItem]:
        return [item for item in self.items if item.timer_started_at is not None]

    def iter_sorted(self, sort_mode: str = "priority", show_completed: bool = True) -> Iterable[TodoItem]:
        items = list(self.items)
        if not show_completed:
            items = [item for item in items if not item.completed]

        if sort_mode == "file":
            key = lambda item: item.line_index
        elif sort_mode == "created":
            key = lambda item: (
                item.completed,
                item.creation_date or "9999-99-99",
                item.priority or "Z{",
                item.line_index,
            )
        elif sort_mode == "description":
            key = lambda item: (
                item.completed,
                normalize_sort_text(item.description),
                item.priority or "Z{",
                item.line_index,
            )
        elif sort_mode == "worked":
            key = lambda item: (
                item.completed,
                -item.total_elapsed_seconds(),
                item.priority or "Z{",
                item.line_index,
            )
        else:  # priority
            key = lambda item: (
                item.completed,
                item.priority or "Z{",
                item.creation_date or "9999-99-99",
                item.line_index,
            )
        return sorted(items, key=key)


def normalize_single_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def normalize_sort_text(value: str) -> str:
    return normalize_single_line(value).casefold()


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    return match.group(0) if match else None


def parse_todo_line(line: str, line_index: int = -1) -> TodoItem:
    raw = normalize_single_line(line)
    if not raw:
        return TodoItem(description="", line_index=line_index)

    tokens = raw.split()
    index = 0
    completed = False
    priority = None
    creation_date = None
    completion_date = None

    if tokens and tokens[0] == "x":
        completed = True
        index += 1
        if index < len(tokens) and is_date_string(tokens[index]):
            completion_date = tokens[index]
            index += 1
        if index < len(tokens) and is_date_string(tokens[index]):
            creation_date = tokens[index]
            index += 1
    else:
        if index < len(tokens):
            match = PRIORITY_RE.match(tokens[index])
            if match:
                priority = match.group(1)
                index += 1
        if index < len(tokens) and is_date_string(tokens[index]):
            creation_date = tokens[index]
            index += 1

    description_tokens: list[str] = []
    time_spent_seconds = 0
    timer_started_at: datetime | None = None
    last_worked_at: datetime | None = None

    for token in tokens[index:]:
        if token.startswith("spent:"):
            candidate = token.split(":", 1)[1]
            try:
                time_spent_seconds = parse_duration(candidate)
                continue
            except TodoFormatError:
                pass
        if token.startswith("active:"):
            candidate = token.split(":", 1)[1]
            parsed = parse_timestamp(candidate)
            if parsed is not None:
                timer_started_at = parsed
                continue
        if token.startswith("lastworked:"):
            candidate = token.split(":", 1)[1]
            parsed = parse_timestamp(candidate)
            if parsed is not None:
                last_worked_at = parsed
                continue
        description_tokens.append(token)

    return TodoItem(
        description=" ".join(description_tokens),
        completed=completed,
        priority=priority,
        creation_date=creation_date,
        completion_date=completion_date,
        time_spent_seconds=time_spent_seconds,
        timer_started_at=timer_started_at,
        last_worked_at=last_worked_at,
        line_index=line_index,
    )


def serialize_todo_line(item: TodoItem) -> str:
    parts: list[str] = []
    if item.completed:
        parts.append("x")
        if item.completion_date:
            parts.append(item.completion_date)
        if item.creation_date:
            parts.append(item.creation_date)
    else:
        if item.priority:
            parts.append(f"({item.priority})")
        if item.creation_date:
            parts.append(item.creation_date)

    if item.description:
        parts.append(normalize_single_line(item.description))
    if item.time_spent_seconds > 0:
        parts.append(f"spent:{format_duration(item.time_spent_seconds)}")
    if item.last_worked_at is not None:
        parts.append(f"lastworked:{format_timestamp(item.last_worked_at)}")
    if item.timer_started_at is not None:
        parts.append(f"active:{format_timestamp(item.timer_started_at)}")
    return " ".join(part for part in parts if part)

from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping
import json
import os
import re
import tempfile
import time
import uuid

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
PRIORITY_RE = re.compile(r"^\(([A-Z])\1*\)$")
URL_RE = re.compile(r"https?://\S+")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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
        raise TodoFormatError(
            f"Invalid timestamp: {value!r}. Expected YYYY-MM-DD-HH-MM-SS."
        )
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


def normalize_priority(value: str | None) -> str | None:
    """Normalizes a todo priority token value.

    Args:
        value: Priority text without parentheses, such as ``"A"``,
            ``"AAAA"``, or ``"BB"``. Empty strings and ``None`` mean no
            priority.

    Returns:
        Uppercase repeated-letter priority text, or ``None`` when no priority
        was provided.

    Raises:
        TodoFormatError: If the priority is not one repeated A-Z letter.
    """
    if value is None:
        return None
    priority = value.strip().upper()
    if not priority:
        return None
    if not re.fullmatch(r"[A-Z]+", priority) or len(set(priority)) != 1:
        raise TodoFormatError(
            f"Invalid priority {priority!r}. "
            "Expected repeated A-Z letters like A, AA, or BBB."
        )
    return priority


def priority_sort_key(priority: str | None) -> tuple[str, int]:
    """Builds the sort key for todo priority ordering.

    Args:
        priority: Priority text without parentheses, such as ``"A"``,
            ``"AAAA"``, or ``"BB"``.

    Returns:
        A tuple of ``(letter_group, negative_length)``. Empty priorities sort
        after ``Z``. Longer repeated priorities sort first inside the same
        letter group, so ``AAAA`` comes before ``A``.
    """
    normalized = normalize_priority(priority)
    if normalized is None:
        return ("Z{", 0)
    return (normalized[0], -len(normalized))


def validate_task_id(value: str) -> str:
    """Validates a stable task id used for sync matching.

    Args:
        value: Task id text from a ``tid:<value>`` token or Firestore task
            document.

    Returns:
        The validated task id text.

    Raises:
        TodoFormatError: If the id contains characters that do not fit a
            portable todo.txt metadata token or Firestore document id.
    """
    if not TASK_ID_RE.fullmatch(value):
        raise TodoFormatError(
            f"Invalid task id {value!r}. Expected letters, numbers, "
            "underscores, or hyphens."
        )
    return value


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
    task_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        self.priority = normalize_priority(self.priority)
        if self.task_id is not None:
            self.task_id = validate_task_id(self.task_id)
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
        return [
            token
            for token in self.description.split()
            if token.startswith("+") and len(token) > 1
        ]

    @property
    def contexts(self) -> list[str]:
        return [
            token
            for token in self.description.split()
            if token.startswith("@") and len(token) > 1
        ]

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
    archive_file: str = ""
    openai_api_key: str = ""
    last_closed_at: str = ""
    project_filter: str = ""
    column_widths: dict[str, int] = field(default_factory=dict)
    window_geometry: str = ""
    sort_mode: str = "priority"
    column_sort_column: str = ""
    column_sort_direction: str = ""
    show_completed: bool = True
    idle_timeout_minutes: int = 10
    check_in_interval_minutes: int = 0
    worked_today_date: str = ""
    worked_today_seconds: dict[str, int] = field(default_factory=dict)
    worked_today_active_started_at: dict[str, str] = field(default_factory=dict)


class ConfigStore:
    def __init__(self, app_name: str = "TodoTimerTXT") -> None:
        self.app_name = app_name
        self.path = self._default_path(app_name)
        self.loaded = False
        self.load_message = "Config not loaded yet."

    @staticmethod
    def _default_path(app_name: str) -> Path:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / ".config"
        folder = base / app_name
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "config.json"

    def load(self) -> AppConfig:
        self.loaded = False
        if not self.path.exists():
            self.load_message = "Config file not found; using defaults."
            return AppConfig()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Config file must contain a JSON object.")
            valid_fields = {item.name for item in fields(AppConfig)}
            filtered = {
                key: value for key, value in data.items() if key in valid_fields
            }
            self.loaded = True
            ignored = len(data) - len(filtered)
            if ignored:
                self.load_message = (
                    f"Loaded config; ignored {ignored} unknown setting(s)."
                )
            else:
                self.load_message = "Loaded config."
            return AppConfig(**filtered)
        except Exception as exc:
            self.load_message = f"Could not load config: {exc}"
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        self.path.write_text(
            json.dumps(asdict(config), indent=2), encoding="utf-8"
        )
        self.loaded = True
        self.load_message = "Saved config."


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
        self.items = [
            parse_todo_line(line, line_index=index)
            for index, line in enumerate(raw_lines)
            if line.strip()
        ]
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

    def archive_completed(self, archive_path: str | os.PathLike[str]) -> int:
        if self.path is None:
            raise RuntimeError("No todo.txt file is loaded.")

        completed = [item for item in self.items if item.completed]
        if not completed:
            return 0

        file_path = Path(archive_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            file_path.read_text(encoding="utf-8")
            if file_path.exists()
            else ""
        )
        archive_lines = "\n".join(serialize_todo_line(item) for item in completed)
        separator = "" if not existing or existing.endswith("\n") else "\n"
        self._atomic_write(file_path, f"{existing}{separator}{archive_lines}\n")

        completed_ids = {item.id for item in completed}
        self.items = [item for item in self.items if item.id not in completed_ids]
        self.save()
        return len(completed)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                dir=path.parent,
                encoding="utf-8",
            ) as handle:
                handle.write(content)
                temp_name = handle.name

            for attempt in range(10):
                try:
                    Path(temp_name).replace(path)
                    return
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.2)
        finally:
            if temp_name and Path(temp_name).exists():
                Path(temp_name).unlink()

    def add_from_text(self, text: str) -> TodoItem:
        item = parse_todo_line(text, line_index=len(self.items))
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

    def toggle_complete(
        self, item_id: str, today: str | None = None
    ) -> TodoItem:
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

    def adjust_priority(self, item_id: str, direction: int) -> TodoItem:
        item = self.get_by_id(item_id)
        if direction == 0:
            return item
        if item.priority is None:
            item.priority = "A" if direction < 0 else "Z"
            return item
        if direction < 0:
            if item.priority[0] == "A":
                item.priority += "A"
                return item
            code = ord(item.priority[0]) - 1
        else:
            if len(item.priority) > 1:
                item.priority = item.priority[:-1]
                return item
            code = min(ord("Z"), ord(item.priority[0]) + 1)
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

    def stop_all_timers(
        self,
        except_item_id: str | None = None,
        now: datetime | None = None,
    ) -> list[TodoItem]:
        changed: list[TodoItem] = []
        for item in self.items:
            if item.id == except_item_id:
                continue
            if item.timer_started_at is not None:
                item.stop_timer(now=now)
                changed.append(item)
        return changed

    def start_timer(self, item_id: str, now: datetime | None = None) -> TodoItem:
        self.stop_all_timers(except_item_id=item_id)
        item = self.get_by_id(item_id)
        item.start_timer(now=now)
        return item

    def stop_timer(self, item_id: str, now: datetime | None = None) -> TodoItem:
        item = self.get_by_id(item_id)
        item.stop_timer(now=now)
        return item

    def running_items(self) -> list[TodoItem]:
        return [
            item for item in self.items if item.timer_started_at is not None
        ]

    def iter_sorted(
        self, sort_mode: str = "priority", show_completed: bool = True
    ) -> Iterable[TodoItem]:
        items = list(self.items)
        if not show_completed:
            items = [item for item in items if not item.completed]

        if sort_mode == "file":
            key = lambda item: item.line_index
        elif sort_mode == "created":
            key = lambda item: (
                item.completed,
                item.creation_date or "9999-99-99",
                priority_sort_key(item.priority),
                item.line_index,
            )
        elif sort_mode == "description":
            key = lambda item: (
                item.completed,
                normalize_sort_text(item.description),
                priority_sort_key(item.priority),
                item.line_index,
            )
        elif sort_mode == "worked":
            key = lambda item: (
                item.completed,
                -item.total_elapsed_seconds(),
                priority_sort_key(item.priority),
                item.line_index,
            )
        else:  # priority
            key = lambda item: (
                item.completed,
                priority_sort_key(item.priority),
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
                priority = tokens[index][1:-1]
                index += 1
        if index < len(tokens) and is_date_string(tokens[index]):
            creation_date = tokens[index]
            index += 1

    description_tokens: list[str] = []
    time_spent_seconds = 0
    timer_started_at: datetime | None = None
    last_worked_at: datetime | None = None
    task_id: str | None = None

    for token in tokens[index:]:
        if token.startswith("tid:"):
            candidate = token.split(":", 1)[1]
            try:
                task_id = validate_task_id(candidate)
                continue
            except TodoFormatError:
                pass
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
        task_id=task_id,
        id=task_id or uuid.uuid4().hex,
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
    if item.task_id:
        parts.append(f"tid:{item.task_id}")
    if item.time_spent_seconds > 0:
        parts.append(f"spent:{format_duration(item.time_spent_seconds)}")
    if item.last_worked_at is not None:
        parts.append(f"lastworked:{format_timestamp(item.last_worked_at)}")
    if item.timer_started_at is not None:
        parts.append(f"active:{format_timestamp(item.timer_started_at)}")
    return " ".join(part for part in parts if part)


FIRESTORE_TASK_SCHEMA_VERSION = 1


def ensure_task_id(item: TodoItem) -> str:
    """Ensures a task has a stable id for sync matching.

    Args:
        item: Parsed todo item. If ``item.task_id`` is empty, this function
            assigns the item's existing internal ``id`` as its stable task id.

    Returns:
        Stable task id suitable for a ``tid:<value>`` token and Firestore
        document id.
    """
    task_id = validate_task_id(item.task_id or item.id)
    item.task_id = task_id
    return task_id


def todo_item_to_firestore_document(
    item: TodoItem,
    source_id: str = "",
    *,
    assign_task_id: bool = False,
) -> dict[str, object]:
    """Converts a parsed todo item into a Firestore task document.

    Args:
        item: Todo item parsed from a todo.txt line.
        source_id: Source identifier for the local todo.txt file that owns the
            task. Empty string means the caller has not assigned a source yet.
        assign_task_id: When true, assign ``item.task_id`` if it is missing so
            the resulting document can be matched back to the todo.txt line on
            later syncs.

    Returns:
        Firestore-ready dictionary with these keys:
        ``schema_version`` (int), ``tid`` (str), ``source_id`` (str),
        ``description`` (str), ``completed`` (bool), ``priority`` (str),
        ``creation_date`` (str), ``completion_date`` (str),
        ``time_spent_seconds`` (int), ``timer_started_at`` (str),
        ``last_worked_at`` (str), and ``line_index`` (int).
    """
    task_id = ensure_task_id(item) if assign_task_id else item.task_id
    return {
        "schema_version": FIRESTORE_TASK_SCHEMA_VERSION,
        "tid": task_id or "",
        "source_id": source_id,
        "description": item.description,
        "completed": item.completed,
        "priority": item.priority or "",
        "creation_date": item.creation_date or "",
        "completion_date": item.completion_date or "",
        "time_spent_seconds": item.time_spent_seconds,
        "timer_started_at": (
            format_timestamp(item.timer_started_at)
            if item.timer_started_at is not None
            else ""
        ),
        "last_worked_at": (
            format_timestamp(item.last_worked_at)
            if item.last_worked_at is not None
            else ""
        ),
        "line_index": item.line_index,
    }


def firestore_document_to_todo_item(
    document: Mapping[str, object],
    *,
    line_index: int | None = None,
) -> TodoItem:
    """Converts a Firestore task document back into a todo item.

    Args:
        document: Firestore task document produced by
            ``todo_item_to_firestore_document``. Expected keys are
            ``tid``, ``description``, ``completed``, ``priority``,
            ``creation_date``, ``completion_date``, ``time_spent_seconds``,
            ``timer_started_at``, ``last_worked_at``, and ``line_index``.
        line_index: Optional replacement line index to use instead of the
            document's ``line_index`` value.

    Returns:
        Todo item that can be serialized with ``serialize_todo_line``.

    Raises:
        KeyError: If a required document key is missing.
        TodoFormatError: If priority, dates, timestamps, or task id values do
            not match the todo.txt metadata formats.
    """
    task_id = validate_task_id(document["tid"]) if document["tid"] else None
    item_line_index = (
        int(document["line_index"]) if line_index is None else line_index
    )
    return TodoItem(
        description=document["description"],
        completed=document["completed"],
        priority=document["priority"] or None,
        creation_date=document["creation_date"] or None,
        completion_date=document["completion_date"] or None,
        time_spent_seconds=int(document["time_spent_seconds"]),
        timer_started_at=parse_timestamp(document["timer_started_at"] or None),
        last_worked_at=parse_timestamp(document["last_worked_at"] or None),
        line_index=item_line_index,
        task_id=task_id,
        id=task_id or uuid.uuid4().hex,
    )


def todo_text_to_firestore_documents(
    todo_text: str,
    source_id: str = "",
    *,
    assign_task_ids: bool = False,
) -> list[dict[str, object]]:
    """Converts todo.txt content into Firestore task documents.

    Args:
        todo_text: Full todo.txt file contents.
        source_id: Source identifier to attach to every produced document.
        assign_task_ids: When true, assign missing stable task ids so exported
            documents can be matched back to local lines on later syncs.

    Returns:
        List of Firestore-ready task dictionaries, one per non-empty todo.txt
        line, preserving original line indexes.
    """
    documents: list[dict[str, object]] = []
    for index, line in enumerate(todo_text.splitlines()):
        if not line.strip():
            continue
        item = parse_todo_line(line, line_index=index)
        documents.append(
            todo_item_to_firestore_document(
                item,
                source_id,
                assign_task_id=assign_task_ids,
            )
        )
    return documents


def firestore_documents_to_todo_text(
    documents: Iterable[Mapping[str, object]],
) -> str:
    """Converts Firestore task documents into todo.txt content.

    Args:
        documents: Firestore task documents produced by
            ``todo_item_to_firestore_document`` or loaded from Firestore with
            the same schema. Documents are ordered by ``line_index`` before
            export.

    Returns:
        todo.txt content containing one serialized task per line.

    Raises:
        KeyError: If a required document key is missing.
        TodoFormatError: If a document contains invalid todo.txt metadata.
    """
    ordered_documents = sorted(
        documents,
        key=lambda document: (int(document["line_index"]), str(document["tid"])),
    )
    return "\n".join(
        serialize_todo_line(firestore_document_to_todo_item(document))
        for document in ordered_documents
    )

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable
import hashlib
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
        value: Task id text from a ``tid:<value>`` token.

    Returns:
        The validated task id text.

    Raises:
        TodoFormatError: If the id contains characters that do not fit a
            portable todo.txt metadata token.
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

    def set_todo_tid_if_missing(self) -> str:
        """Sets a stable todo.txt ``tid`` metadata value when one is missing.

        Returns:
            Stable task id suitable for serializing as ``tid:<value>``.
        """
        if self.task_id is None:
            self.task_id = validate_task_id(self.id)
        return self.task_id

    @property
    def projects(self) -> list[str]:
        return split_task_description_tags(self.description)[1]

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
            self.set_todo_tid_if_missing()
            self.timer_started_at = now or datetime.now()

    def stop_timer(self, now: datetime | None = None) -> int:
        if self.timer_started_at is None:
            return 0
        self.set_todo_tid_if_missing()
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


@dataclass(frozen=True, slots=True)
class TodoTaskLine:
    """Stores one parsed todo.txt line used in external diff analysis.

    Args:
        line_number: One-based line number from the source file.
        text: Original todo.txt line text.
        item: Parsed todo item for the line.
    """

    line_number: int
    text: str
    item: TodoItem


@dataclass(frozen=True, slots=True)
class TodoTaskLineChange:
    """Describes one task-level line change in todo.txt.

    Args:
        task_id: Stable ``tid`` value used to match the task.
        shadow_line_number: One-based line number in the baseline file, or
            None for a task that only exists on disk.
        disk_line_number: One-based line number in the current disk file, or
            None for a task that only exists in the baseline.
        shadow_text: Baseline todo.txt line text, or an empty string when the
            task is new on disk.
        disk_text: Current disk todo.txt line text, or an empty string when the
            task was removed from disk.
        shadow_description: Parsed baseline task description.
        disk_description: Parsed current disk task description.
        shadow_item: Parsed baseline task, or None when the task is new on
            disk.
        disk_item: Parsed current disk task, or None when the task was removed
            from disk.
    """

    task_id: str
    shadow_line_number: int | None
    disk_line_number: int | None
    shadow_text: str
    disk_text: str
    shadow_description: str
    disk_description: str
    shadow_item: TodoItem | None
    disk_item: TodoItem | None


@dataclass(frozen=True, slots=True)
class TodoTaskLineDiff:
    """Groups todo.txt external differences by task-level meaning.

    Args:
        modified_tasks: Tasks where the same ``tid`` exists in both files but
            the serialized line text changed.
        added_tasks: Tasks with a ``tid`` that exists only in the current disk
            file.
        removed_tasks: Tasks with a ``tid`` that exists only in the baseline
            file.
        unmatched_added_lines: Current disk lines that cannot be matched by
            ``tid``. This includes tasks without ``tid`` and unparsable lines.
        unmatched_removed_lines: Baseline lines that cannot be matched by
            ``tid``. This includes tasks without ``tid`` and unparsable lines.
    """

    modified_tasks: list[TodoTaskLineChange] = field(default_factory=list)
    added_tasks: list[TodoTaskLineChange] = field(default_factory=list)
    removed_tasks: list[TodoTaskLineChange] = field(default_factory=list)
    unmatched_added_lines: list[str] = field(default_factory=list)
    unmatched_removed_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TodoFileChange:
    """Describes todo.txt content that changed outside the app.

    Args:
        todo_path: Path to the configured todo.txt file.
        shadow_path: Path to the app-managed baseline copy.
        disk_content: Current content read from todo_path.
        shadow_content: Last known content stored in shadow_path.
        added_lines: Count of lines present on disk but not in the baseline.
        removed_lines: Count of baseline lines missing from disk.
        task_diff: Task-level classification of changed lines.
    """

    todo_path: Path
    shadow_path: Path
    disk_content: str
    shadow_content: str
    added_lines: int
    removed_lines: int
    task_diff: TodoTaskLineDiff = field(default_factory=TodoTaskLineDiff)


class TodoFileShadow:
    """Stores per-file todo.txt baselines for external change detection."""

    def __init__(self, folder: str | os.PathLike[str]) -> None:
        """Creates a shadow store rooted at folder.

        Args:
            folder: Directory where baseline copies are stored. Each todo.txt
                path maps to a stable hash-named text file in this directory.
        """
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def shadow_path_for(self, todo_path: str | os.PathLike[str]) -> Path:
        """Returns the baseline path for a todo.txt path.

        Args:
            todo_path: todo.txt file path to map to a baseline file.

        Returns:
            Path under this shadow store for the todo.txt path.
        """
        resolved = str(Path(todo_path).resolve())
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        return self.folder / f"todo_{digest}.txt"

    def detect_external_change(
        self, todo_path: str | os.PathLike[str]
    ) -> TodoFileChange | None:
        """Compares the current todo.txt file to the stored baseline.

        Args:
            todo_path: todo.txt file to compare against its baseline.

        Returns:
            TodoFileChange when a baseline exists and differs from disk,
            otherwise None.
        """
        file_path = Path(todo_path)
        shadow_path = self.shadow_path_for(file_path)
        if not file_path.exists() or not shadow_path.exists():
            return None

        disk_content = file_path.read_text(encoding="utf-8")
        shadow_content = shadow_path.read_text(encoding="utf-8")
        if disk_content == shadow_content:
            return None

        disk_lines = Counter(disk_content.splitlines())
        shadow_lines = Counter(shadow_content.splitlines())
        added_lines = sum((disk_lines - shadow_lines).values())
        removed_lines = sum((shadow_lines - disk_lines).values())
        task_diff = self.describe_task_line_changes(
            shadow_content,
            disk_content,
        )
        return TodoFileChange(
            todo_path=file_path,
            shadow_path=shadow_path,
            disk_content=disk_content,
            shadow_content=shadow_content,
            added_lines=added_lines,
            removed_lines=removed_lines,
            task_diff=task_diff,
        )

    def describe_task_line_changes(
        self,
        shadow_content: str,
        disk_content: str,
    ) -> TodoTaskLineDiff:
        """Classifies changed todo.txt lines by stable task id.

        Args:
            shadow_content: Baseline todo.txt content previously written by
                the app.
            disk_content: Current todo.txt content read from disk.

        Returns:
            TodoTaskLineDiff with modified, added, removed, and unmatched line
            changes. Duplicate ``tid`` values are treated as unmatched because
            they cannot be safely mapped to a single task.
        """
        shadow_tasks, shadow_unmatched = self.task_lines_by_tid(shadow_content)
        disk_tasks, disk_unmatched = self.task_lines_by_tid(disk_content)
        unmatched_added_lines = self.changed_unmatched_lines(
            disk_unmatched,
            shadow_unmatched,
        )
        unmatched_removed_lines = self.changed_unmatched_lines(
            shadow_unmatched,
            disk_unmatched,
        )

        modified_tasks: list[TodoTaskLineChange] = []
        added_tasks: list[TodoTaskLineChange] = []
        removed_tasks: list[TodoTaskLineChange] = []

        for task_id in sorted(shadow_tasks.keys() & disk_tasks.keys()):
            shadow_task = shadow_tasks[task_id]
            disk_task = disk_tasks[task_id]
            if shadow_task.text == disk_task.text:
                continue
            modified_tasks.append(
                TodoTaskLineChange(
                    task_id=task_id,
                    shadow_line_number=shadow_task.line_number,
                    disk_line_number=disk_task.line_number,
                    shadow_text=shadow_task.text,
                    disk_text=disk_task.text,
                    shadow_description=shadow_task.item.description,
                    disk_description=disk_task.item.description,
                    shadow_item=shadow_task.item,
                    disk_item=disk_task.item,
                )
            )

        for task_id in sorted(disk_tasks.keys() - shadow_tasks.keys()):
            disk_task = disk_tasks[task_id]
            added_tasks.append(
                TodoTaskLineChange(
                    task_id=task_id,
                    shadow_line_number=None,
                    disk_line_number=disk_task.line_number,
                    shadow_text="",
                    disk_text=disk_task.text,
                    shadow_description="",
                    disk_description=disk_task.item.description,
                    shadow_item=None,
                    disk_item=disk_task.item,
                )
            )

        for task_id in sorted(shadow_tasks.keys() - disk_tasks.keys()):
            shadow_task = shadow_tasks[task_id]
            removed_tasks.append(
                TodoTaskLineChange(
                    task_id=task_id,
                    shadow_line_number=shadow_task.line_number,
                    disk_line_number=None,
                    shadow_text=shadow_task.text,
                    disk_text="",
                    shadow_description=shadow_task.item.description,
                    disk_description="",
                    shadow_item=shadow_task.item,
                    disk_item=None,
                )
            )

        return TodoTaskLineDiff(
            modified_tasks=modified_tasks,
            added_tasks=added_tasks,
            removed_tasks=removed_tasks,
            unmatched_added_lines=unmatched_added_lines,
            unmatched_removed_lines=unmatched_removed_lines,
        )

    @staticmethod
    def diff_updates_only_time_metadata(
        task_diff: TodoTaskLineDiff,
    ) -> bool:
        """Returns whether a task diff is safe time metadata only.

        Args:
            task_diff: Task-level diff produced by describe_task_line_changes.

        Returns:
            True when every changed task is matched by ``tid`` and changes
            only ``spent:``, ``lastworked:``, or ``active:`` metadata. False
            when tasks were added, removed, unmatched, or changed in any
            non-time field.
        """
        if (
            not task_diff.modified_tasks
            or task_diff.added_tasks
            or task_diff.removed_tasks
            or task_diff.unmatched_added_lines
            or task_diff.unmatched_removed_lines
        ):
            return False
        return all(
            TodoFileShadow.task_change_updates_only_time_metadata(change)
            for change in task_diff.modified_tasks
        )

    @staticmethod
    def diff_can_auto_accept_without_merge_base(
        task_diff: TodoTaskLineDiff,
    ) -> bool:
        """Returns whether a diff is safe to reload without a merge base.

        Args:
            task_diff: Task-level diff produced by describe_task_line_changes.

        Returns:
            True when every changed task is matched by ``tid`` and only
            non-total time metadata changed. Changes to ``spent:`` or
            ``active:`` return False because they can replace work from another
            machine unless a common ancestor is available for summing deltas.
        """
        if not TodoFileShadow.diff_updates_only_time_metadata(task_diff):
            return False
        return all(
            TodoFileShadow.task_change_updates_only_non_total_time_metadata(
                change
            )
            for change in task_diff.modified_tasks
        )

    @staticmethod
    def task_change_updates_only_time_metadata(
        change: TodoTaskLineChange,
    ) -> bool:
        """Returns whether one matched task changed only time metadata.

        Args:
            change: Modified task line with both baseline and disk parsed
                TodoItem values.

        Returns:
            True when all non-time task fields are identical and at least one
            time metadata field changed.
        """
        if change.shadow_item is None or change.disk_item is None:
            return False

        shadow = change.shadow_item
        disk = change.disk_item
        non_time_fields_match = (
            shadow.task_id == disk.task_id
            and shadow.description == disk.description
            and shadow.completed == disk.completed
            and shadow.priority == disk.priority
            and shadow.creation_date == disk.creation_date
            and shadow.completion_date == disk.completion_date
        )
        time_fields_changed = (
            shadow.time_spent_seconds != disk.time_spent_seconds
            or shadow.timer_started_at != disk.timer_started_at
            or shadow.last_worked_at != disk.last_worked_at
        )
        return non_time_fields_match and time_fields_changed

    @staticmethod
    def task_change_updates_only_non_total_time_metadata(
        change: TodoTaskLineChange,
    ) -> bool:
        """Returns whether one task changed only non-total time metadata.

        Args:
            change: Modified task line with both baseline and disk parsed
                TodoItem values.

        Returns:
            True when ``lastworked:`` changed without changing ``spent:`` or
            ``active:``. These changes are safe to reload without a common
            ancestor because they do not replace accumulated work totals.
        """
        if not TodoFileShadow.task_change_updates_only_time_metadata(change):
            return False

        shadow = change.shadow_item
        disk = change.disk_item
        if shadow is None or disk is None:
            return False
        return (
            shadow.time_spent_seconds == disk.time_spent_seconds
            and shadow.timer_started_at == disk.timer_started_at
            and shadow.last_worked_at != disk.last_worked_at
        )

    @staticmethod
    def changed_unmatched_lines(
        changed_lines: list[str],
        baseline_lines: list[str],
    ) -> list[str]:
        """Returns unmatched lines present after removing unchanged matches.

        Args:
            changed_lines: Candidate changed lines from one side of a diff.
            baseline_lines: Lines from the other side of the diff.

        Returns:
            Lines from changed_lines that are not canceled out by equal lines
            in baseline_lines, preserving changed_lines order.
        """
        remaining_baseline = Counter(baseline_lines)
        result: list[str] = []
        for line in changed_lines:
            if remaining_baseline[line]:
                remaining_baseline[line] -= 1
            else:
                result.append(line)
        return result

    @staticmethod
    def task_lines_by_tid(
        content: str,
    ) -> tuple[dict[str, TodoTaskLine], list[str]]:
        """Maps todo.txt lines by stable ``tid`` metadata.

        Args:
            content: todo.txt file content.

        Returns:
            Tuple of ``(task_lines, unmatched_lines)`` where task_lines maps
            ``tid`` values to parsed task lines and unmatched_lines contains
            nonblank lines that could not be safely keyed by ``tid``.
        """
        task_lines: dict[str, TodoTaskLine] = {}
        unmatched_lines: list[str] = []
        duplicate_task_ids: set[str] = set()

        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = parse_todo_line(line, line_index=line_number - 1)
            except TodoFormatError:
                unmatched_lines.append(line)
                continue
            if item.task_id is None:
                unmatched_lines.append(line)
                continue
            if item.task_id in task_lines:
                duplicate_task_ids.add(item.task_id)
                unmatched_lines.append(task_lines[item.task_id].text)
                unmatched_lines.append(line)
                del task_lines[item.task_id]
                continue
            if item.task_id in duplicate_task_ids:
                unmatched_lines.append(line)
                continue
            task_lines[item.task_id] = TodoTaskLine(
                line_number=line_number,
                text=line,
                item=item,
            )

        return task_lines, unmatched_lines

    def write_baseline(
        self,
        todo_path: str | os.PathLike[str],
        content: str | None = None,
    ) -> None:
        """Writes the current baseline for a todo.txt file.

        Args:
            todo_path: todo.txt file represented by the baseline.
            content: Text to write as the baseline. When None, the current
                todo.txt content is read from disk.

        Raises:
            FileNotFoundError: If content is None and todo_path does not exist.
        """
        file_path = Path(todo_path)
        baseline = (
            file_path.read_text(encoding="utf-8")
            if content is None
            else content
        )
        self.folder.mkdir(parents=True, exist_ok=True)
        self.shadow_path_for(file_path).write_text(baseline, encoding="utf-8")


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
        self._atomic_write(self.path, self.serialize_content())
        for index, item in enumerate(self.items):
            item.line_index = index

    def serialize_content(self) -> str:
        """Serializes the store to the exact todo.txt save content.

        Returns:
            Text that TodoStore.save writes to disk, including the trailing
            newline when at least one task exists.
        """
        serialized = "\n".join(serialize_todo_line(item) for item in self.items)
        if serialized:
            serialized += "\n"
        return serialized

    def archive_completed(self, archive_path: str | os.PathLike[str]) -> int:
        if self.path is None:
            raise RuntimeError("No todo.txt file is loaded.")

        completed = [item for item in self.items if item.completed]
        if not completed:
            return 0
        for item in completed:
            item.set_todo_tid_if_missing()

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
        item.set_todo_tid_if_missing()
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
        item.set_todo_tid_if_missing()
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
        item.set_todo_tid_if_missing()
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
        item.set_todo_tid_if_missing()
        item.priority = None
        return item

    def update_item(self, item_id: str, **changes: object) -> TodoItem:
        item = self.get_by_id(item_id)
        item.set_todo_tid_if_missing()
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


def is_project_tag_token(token: str) -> bool:
    return token.startswith("+") and len(token) > 1


def split_task_description_tags(description: str) -> tuple[str, list[str]]:
    tokens = normalize_single_line(description).split()
    tag_tokens = [token for token in tokens if is_project_tag_token(token)]
    description_tokens = [
        token for token in tokens if not is_project_tag_token(token)
    ]
    return " ".join(description_tokens), tag_tokens


def normalize_project_tag_tokens(tags: str) -> list[str]:
    project_tags: list[str] = []
    for token in normalize_single_line(tags).split():
        tag = token if token.startswith("+") else f"+{token}"
        if is_project_tag_token(tag):
            project_tags.append(tag)
    return project_tags


def compose_task_description(description: str, tags: str) -> str:
    description_text, inline_tags = split_task_description_tags(description)
    project_tags = inline_tags + normalize_project_tag_tokens(tags)
    return normalize_single_line(" ".join([description_text, *project_tags]))


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

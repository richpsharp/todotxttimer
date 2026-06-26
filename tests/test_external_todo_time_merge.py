from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from todo_core import TodoFileShadow, TodoStore


class ExternalTodoTimeMergeTests(unittest.TestCase):
    def test_lastworked_only_change_can_auto_accept_without_merge_base(self) -> None:
        """Checks the narrow external metadata case that needs no merge base."""
        with TemporaryDirectory() as temp_dir:
            shadow = TodoFileShadow(Path(temp_dir) / "shadows")
            before = (
                "Task one tid:abc spent:01:00:00 "
                "lastworked:2026-06-16-10-00-00\n"
            )
            after = (
                "Task one tid:abc spent:01:00:00 "
                "lastworked:2026-06-16-11-00-00\n"
            )

            diff = shadow.describe_task_line_changes(before, after)

            self.assertTrue(TodoFileShadow.diff_updates_only_time_metadata(diff))
            self.assertTrue(
                TodoFileShadow.diff_can_auto_accept_without_merge_base(diff)
            )

    def test_divergent_spent_changes_require_a_merge_base(self) -> None:
        """Simulates two machines recording different time from one old file."""
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shadow = TodoFileShadow(root / "shadows")
            todo_path = root / "todo.txt"

            common_start = "Task one tid:abc spent:00:00:00\n"
            machine_a_after_day_one = "Task one tid:abc spent:01:00:00\n"
            machine_b_after_day_two = "Task one tid:abc spent:02:00:00\n"

            todo_path.write_text(machine_b_after_day_two, encoding="utf-8")
            store = TodoStore()
            store.load(todo_path)
            shadow.write_baseline(todo_path, machine_b_after_day_two)

            todo_path.write_text(machine_a_after_day_one, encoding="utf-8")
            change = shadow.detect_external_change(todo_path)

            self.assertIsNotNone(change)
            self.assertEqual(store.serialize_content(), machine_b_after_day_two)
            self.assertTrue(
                TodoFileShadow.diff_updates_only_time_metadata(
                    change.task_diff
                )
            )
            self.assertFalse(
                TodoFileShadow.diff_can_auto_accept_without_merge_base(
                    change.task_diff
                )
            )
            self.assertNotEqual(common_start, machine_a_after_day_one)
            self.assertNotEqual(common_start, machine_b_after_day_two)


if __name__ == "__main__":
    unittest.main()

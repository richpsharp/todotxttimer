import unittest

from todo_core import (
    TodoItem,
    compose_task_description,
    split_task_description_tags,
)


class TaskTagHelperTest(unittest.TestCase):
    def test_split_task_description_tags_keeps_non_project_tokens(self) -> None:
        description, tags = split_task_description_tags(
            "Write report +client @desk +urgent"
        )

        self.assertEqual(description, "Write report @desk")
        self.assertEqual(tags, ["+client", "+urgent"])

    def test_compose_task_description_normalizes_tags(self) -> None:
        self.assertEqual(
            compose_task_description("Write report", "client +urgent"),
            "Write report +client +urgent",
        )

    def test_compose_task_description_moves_inline_tags_to_end(self) -> None:
        self.assertEqual(
            compose_task_description("Write +client report", "+urgent"),
            "Write report +client +urgent",
        )

    def test_todo_item_projects_uses_split_task_description_tags(self) -> None:
        item = TodoItem(description="Task +one @ctx +two")

        self.assertEqual(item.projects, ["+one", "+two"])


if __name__ == "__main__":
    unittest.main()

import unittest

from orchestrator.commands import (
    format_natural_result, natural_task_envelope, parse_natural_command,
)


class NaturalCommandTest(unittest.TestCase):
    def test_parse_command_with_mention(self):
        command = parse_natural_command("@机器人 /glm 分析这份数据")
        self.assertEqual(command["worker_id"], "glm")
        self.assertEqual(command["instructions"], "分析这份数据")

    def test_parse_kimi_without_mention(self):
        command = parse_natural_command("/kimi 检查代码")
        self.assertEqual(command["worker_id"], "kimi_code")
        self.assertEqual(command["instructions"], "检查代码")

    def test_plain_chat_and_json_are_not_commands(self):
        self.assertIsNone(parse_natural_command("今天怎么样"))
        self.assertIsNone(parse_natural_command('{"kind":"task"}'))

    def test_natural_task_has_stable_identity(self):
        event = {"event_id": "evt-1", "message_id": "om-1"}
        command = parse_natural_command("/glm 任务")
        first = natural_task_envelope(event, command, "cloud-desktop-1")
        second = natural_task_envelope(event, command, "cloud-desktop-1")
        self.assertEqual(first, second)
        self.assertEqual(first["assigned_worker"], "glm")
        self.assertEqual(first["instructions"], "任务")

    def test_human_readable_result(self):
        text = format_natural_result({
            "status": "succeeded", "worker_id": "glm", "task_id": "TASK-1",
            "summary": "分析完成", "unknowns": [], "blocking_issues": []
        })
        self.assertIn("执行成功", text)
        self.assertIn("分析完成", text)
        self.assertNotIn("protocol", text)

    def test_failure_includes_actual_error(self):
        text = format_natural_result({
            "status": "failed", "worker_id": "glm", "task_id": "TASK-2",
            "summary": "Worker did not produce a valid result", "unknowns": [],
            "blocking_issues": [], "error": "JSONDecodeError: invalid response",
        })
        self.assertIn("错误详情", text)
        self.assertIn("JSONDecodeError: invalid response", text)


if __name__ == "__main__":
    unittest.main()

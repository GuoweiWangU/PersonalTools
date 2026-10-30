import json
import unittest
from unittest.mock import patch

from orchestrator.workers import OpenAICompatibleWorker


class FakeResponse:
    def __init__(self, envelope):
        self.body = json.dumps(envelope).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class OpenAICompatibleWorkerTest(unittest.TestCase):
    def test_glm_compatible_request(self):
        result = {
            "status": "succeeded", "summary": "GLM test completed",
            "artifacts": [], "unknowns": [], "blocking_issues": []
        }
        envelope = {"choices": [{"message": {"content": json.dumps(result)}}]}
        with patch.dict("os.environ", {"GLM_API_KEY": "test-secret"}):
            with patch("urllib.request.urlopen", return_value=FakeResponse(envelope)) as call:
                worker = OpenAICompatibleWorker(
                    "https://open.bigmodel.cn/api/paas/v4/", "glm-5.3", "GLM_API_KEY"
                )
                output = worker.run({"task_id": "TASK-glm-test"})
        request = call.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(body["model"], "glm-5.3")
        self.assertEqual(body["temperature"], 0.6)
        self.assertNotIn("response_format", body)
        self.assertEqual(output["summary"], "GLM test completed")

    def test_json_code_fence_is_accepted(self):
        result = {
            "status": "succeeded", "summary": "Fenced GLM result",
            "artifacts": [], "unknowns": [], "blocking_issues": []
        }
        content = "```json\n" + json.dumps(result) + "\n```"
        envelope = {"choices": [{"message": {"content": content}}]}
        with patch.dict("os.environ", {"GLM_API_KEY": "test-secret"}):
            with patch("urllib.request.urlopen", return_value=FakeResponse(envelope)):
                worker = OpenAICompatibleWorker(
                    "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "GLM_API_KEY"
                )
                output = worker.run({"task_id": "TASK-glm-fence"})
        self.assertEqual(output["summary"], "Fenced GLM result")


if __name__ == "__main__":
    unittest.main()

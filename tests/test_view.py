import base64
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coplot.server import AgentService


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        plots = root / "coplot/plots"
        plots.mkdir(parents=True)
        (plots / "a.png").write_bytes(PNG)
        (plots / "b.png").write_bytes(PNG)
        self.messages = []
        self.payloads = []
        settings = {"model": "test", "max_tokens": 512, "temperature": 0.2,
                    "timeout_seconds": 10, "reasoning_control": "none", "reasoning_enabled": False}
        self.service = AgentService(
            SimpleNamespace(root=root, plots_dir=plots, source_file=root / "coplot.py", language="python"),
            SimpleNamespace(append=self.append, annotate=lambda entry, **metadata: entry.update(metadata)),
            SimpleNamespace(payload=lambda **kwargs: {}),
            SimpleNamespace(execute=lambda *args, **kwargs: {"entry": {"ok": True}}),
            SimpleNamespace(),
            SimpleNamespace(read=lambda: settings, request_url=lambda: "http://example.invalid/v1/chat/completions"),
        )

    def append(self, role, content, **kwargs):
        entry = {"id": str(len(self.messages)), "role": role, "content": content, **kwargs}
        self.messages.append(entry)
        return entry

    def view(self, paths):
        return self.service._run_actions('```coplot-view\n' + json.dumps({"paths": paths}) + '\n```')

    def test_images_are_attached_only_to_next_turn(self):
        responses = iter([
            '```coplot-view\n{"paths": ["coplot/plots/a.png", "coplot/plots/b.png"]}\n```',
            '```coplot-run\nprint("continue")\n```',
            "Finished inspecting the plots.",
        ])

        def transport(request, **kwargs):
            self.payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": next(responses)},
                                                      "finish_reason": "stop"}]}).encode())

        with patch("coplot.server.urllib.request.urlopen", side_effect=transport):
            result = self.service.respond("Inspect the two plots.")
        self.assertEqual(len(self.payloads), 3)
        self.assertIsInstance(self.payloads[0]["messages"][1]["content"], str)
        parts = self.payloads[1]["messages"][1]["content"]
        self.assertEqual([part["type"] for part in parts], ["text", "image_url", "image_url"])
        for part in parts[1:]:
            self.assertEqual(base64.b64decode(part["image_url"]["url"].split(",")[1]), PNG)
        context = json.loads(self.payloads[1]["messages"][0]["content"].split("Context payload:\n")[1])
        feedback = json.loads(context["action_feedback"])
        self.assertEqual(feedback["paths"], ["coplot/plots/a.png", "coplot/plots/b.png"])
        self.assertIsInstance(self.payloads[2]["messages"][1]["content"], str)
        self.assertEqual(result["actions"][0]["status"], "attached")
        self.assertNotIn("data:image", json.dumps(result))

    def test_no_phrase_trigger(self):
        self.assertEqual(self.service._user_content("Look at the plot", attachments=[]), "Look at the plot")

    def test_invalid_requests_produce_repair_feedback(self):
        for content in ['not JSON', '{}', '{"paths": "a.png"}', '{"paths": []}', '{"paths": [null]}']:
            actions = self.service._run_actions('```coplot-view\n' + content + '\n```')
            self.assertEqual(actions[0]["status"], "failed")
            self.assertTrue(self.service._actions_need_followup(actions))
            self.assertIn("error", self.service._action_feedback(actions))

    def test_paths_and_png_validation(self):
        outside = self.service.project.root / "outside.png"
        outside.write_bytes(PNG)
        (self.service.project.plots_dir / "escape.png").symlink_to(outside)
        (self.service.project.plots_dir / "fake.png").write_text("not a PNG")
        for path in ["outside.png", "coplot/plots/escape.png", "coplot/plots/fake.png",
                     "coplot/plots/missing.png", "coplot/plots/../../outside.png"]:
            self.assertEqual(self.view([path])[0]["status"], "failed", path)
        self.assertEqual(self.view(["coplot/plots/a.png"] * 5)[0]["status"], "failed")

    def test_image_limit_across_blocks(self):
        block = '```coplot-view\n{"paths": ["coplot/plots/a.png"]}\n```\n'
        actions = self.service._run_actions(block * 5)
        self.assertEqual([a["status"] for a in actions], ["queued"] * 4 + ["failed"])

    def test_missing_image_before_followup(self):
        actions = self.view(["coplot/plots/a.png"])
        (self.service.project.plots_dir / "a.png").unlink()
        self.assertEqual(self.service._view_attachments(actions), [])
        self.assertEqual(actions[0]["status"], "failed")

    def test_oversized_image(self):
        with patch("coplot.server.MAX_CHAT_IMAGE_BYTES", 8):
            self.assertEqual(self.view(["coplot/plots/a.png"])[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

import io
import json
import urllib.error
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import coplot.server as server
import test_view


class AgentTests(unittest.TestCase):
    setUp = test_view.ViewTests.setUp
    append = test_view.ViewTests.append

    def transport(self, contents, finish="stop"):
        responses = iter(contents)

        def call(request, **kwargs):
            self.payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": next(responses)},
                                                      "finish_reason": finish}],
                                          "usage": {"completion_tokens": 12}}).encode())
        return call

    def test_default_budget_allows_more_than_four_turns(self):
        with patch("coplot.server.urllib.request.urlopen", side_effect=self.transport(
            ['```coplot-run\nprint(1)\n```'] * 6 + ["Done"]
        )):
            result = self.service.respond("Proceed")
        self.assertEqual(result["turns"], 7)
        self.assertEqual(result["halt_reason"], "")

    def test_budget_exhaustion_is_explicit(self):
        self.service.settings.read()["max_agent_turns"] = 2
        with patch("coplot.server.urllib.request.urlopen", side_effect=self.transport(
            ['```coplot-run\nprint(1)\n```'] * 2
        )):
            result = self.service.respond("Proceed")
        self.assertEqual(result["turns"], 2)
        self.assertIn("2-turn limit", result["halt_reason"])
        self.assertEqual(self.messages[-1]["role"], "system")

    def test_xml_and_malformed_fences_are_repaired_without_execution(self):
        invalid = ['<tool_call><function=run>print(1)</function></tool_call>',
                   '```coplot-run\nprint(1)']
        for content in invalid:
            actions = self.service._run_actions(content)
            self.assertEqual(actions[0]["type"], "protocol_error")
        with patch("coplot.server.urllib.request.urlopen", side_effect=self.transport(
            [invalid[0], '```coplot-run\nprint(1)\n```', "Done"]
        )):
            result = self.service.respond("Proceed")
        self.assertEqual(result["turns"], 3)
        self.assertIn("No actions executed", self.payloads[1]["messages"][0]["content"])

    def test_protocol_repair_is_bounded(self):
        with patch("coplot.server.urllib.request.urlopen", side_effect=self.transport(
            ['<tool_call><function=run>print(1)</function></tool_call>'] * 3
        )):
            result = self.service.respond("Proceed")
        self.assertEqual(result["turns"], 3)
        self.assertIn("two repair attempts", result["halt_reason"])

    def test_output_truncation_executes_no_actions(self):
        with patch("coplot.server.urllib.request.urlopen", side_effect=self.transport(
            ['```coplot-run\nprint(1)\n```'], finish="length"
        )):
            result = self.service.respond("Proceed")
        self.assertEqual(result["actions"], [])
        self.assertIn("No actions", self.messages[-1]["content"])
        self.assertEqual(result["message"]["diagnostics"]["finish_reason"], "length")

    def test_action_order_and_stop(self):
        calls = []
        self.service.shell.execute = lambda code, **kwargs: calls.append("shell") or {"entry": {"ok": True}}
        self.service.session.execute = lambda code, **kwargs: calls.append("session") or {"entry": {"ok": True}}
        self.service._run_actions('```coplot-shell\necho 1\n```\n```coplot-run\nprint(1)\n```')
        self.assertEqual(calls, ["shell", "session"])

        def stop(code, **kwargs):
            server.stop_requested = True
            return {"entry": {"ok": True}}
        self.service.shell.execute = stop
        try:
            actions = self.service._run_actions('```coplot-shell\necho 1\n```\n```coplot-run\nprint(1)\n```')
            self.assertEqual(len(actions), 1)
        finally:
            server.stop_requested = False

    def test_inline_backticks_do_not_close_an_action(self):
        received = []
        self.service.session.execute = lambda code, **kwargs: received.append(code) or {"entry": {"ok": True}}
        actions = self.service._run_actions('```coplot-run\r\nprint("```")\r\n```')
        self.assertEqual(actions[0]["status"], "completed")
        self.assertEqual(received, ['print("```")'])

    def test_failed_action_skips_dependent_later_actions(self):
        self.service.session.execute = lambda *args, **kwargs: {"entry": {"ok": False, "stderr": "failure"}}
        actions = self.service._run_actions('```coplot-run\nmissing\n```\n```coplot-view\n{"paths": ["coplot/plots/a.png"]}\n```')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "failed")

    def test_native_tool_call_receives_protocol_feedback(self):
        response = {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"function": {"name": "run", "arguments": "{}"}}]}}]}
        with patch("coplot.server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            result = self.service._request_and_apply("Proceed", action_feedback="")
        self.assertEqual(result["actions"][0]["type"], "protocol_error")

    def test_context_code_description_and_history_stability(self):
        project = self.service.project
        project.source_file.write_text("# source\n")
        project.summary_file = project.root / "summary.md"
        project.summary_file.write_text("summary")
        chat = server.ChatStore(project.root / "chat.jsonl")
        transcript = server.TranscriptStore(project.root / "transcript.jsonl")
        artifacts = server.ArtifactStore(project.root / "artifacts.jsonl", project.root)
        context = server.ContextBuilder(project, chat, transcript, artifacts)
        context.environment_payload = lambda: {"language": "python"}
        first = chat.append("assistant", "Earlier response")
        before = context.recent_events()
        chat.annotate(first, actions=[{"status": "applied"}])
        chat.append("user", "Next request")
        self.assertEqual(context.recent_events()[:len(before)], before)
        payload = context.payload()
        self.assertEqual(list(payload)[-1], "durable_code")
        self.assertEqual(payload["durable_code"]["description"], "Current state of the durable code")
        self.assertEqual(chat.recent(None)[0]["actions"][0]["status"], "applied")

    def test_repeated_execution_failure_stops(self):
        self.service.session.execute = lambda *args, **kwargs: {"entry": {"id": str(len(self.messages)),
                                                                       "ok": False, "stderr": "NameError"}}
        with patch("coplot.server.urllib.request.urlopen", side_effect=self.transport(
            ['```coplot-run\nmissing\n```'] * 3
        )):
            result = self.service.respond("Proceed")
        self.assertEqual(result["turns"], 3)
        self.assertIn("same action failure", result["halt_reason"])

    def test_http_error_details_are_visible_but_never_executed(self):
        error = urllib.error.HTTPError("http://example.invalid", 400, "Bad request", {},
                                      io.BytesIO(b'Invalid template ```coplot-run\nprint(1)\n```'))
        with patch("coplot.server.urllib.request.urlopen", side_effect=error):
            result = self.service.respond("Proceed")
        self.assertEqual(result["actions"], [])
        self.assertIn("Invalid template", result["message"]["content"])

    def test_prompt_order_and_exploration_cue(self):
        context = {key: key for key in ["durable_code", "action_feedback", "workspace",
                                       "session_summary", "artifact_ledger", "recent_events"]}
        prompt = self.service._system_prompt(context)
        payload = json.loads(prompt.split("Context payload:\n")[1])
        self.assertEqual(list(payload), ["workspace", "session_summary", "recent_events",
                                        "artifact_ledger", "action_feedback", "durable_code"])
        self.assertNotIn("must be able to get the same result", prompt)

    def test_sglang_detection_and_optional_metadata(self):
        responses = [io.BytesIO(json.dumps({"data": [{"id": "qwen", "owned_by": "sglang",
                                                    "max_model_len": 262144}]}).encode()),
                     io.BytesIO(b'{"has_image_understanding": true, "has_audio_understanding": false}')]
        with patch("coplot.server.model_settings_store", SimpleNamespace(models_url=lambda url: url + "/models"), create=True), \
             patch("coplot.server.urllib.request.urlopen", side_effect=responses) as transport:
            result = server.fetch_models("http://localhost:8888/v1")
        self.assertEqual(result["endpoint_kind"], "sglang")
        self.assertEqual(result["supported_modalities"], ["text", "image"])
        self.assertEqual(result["models"][0]["context_window_tokens"], 262144)
        self.assertEqual(transport.call_args_list[1].args[0].full_url, "http://localhost:8888/get_model_info")
        with patch("coplot.server.urllib.request.urlopen", side_effect=urllib.error.URLError("missing")):
            self.assertEqual(server.fetch_sglang_modalities("http://localhost:8888/v1/chat/completions"), ["text"])


if __name__ == "__main__":
    unittest.main()

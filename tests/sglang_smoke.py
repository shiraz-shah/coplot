"""Optional live compatibility checks using only disposable synthetic workspaces.

Run: python3 tests/sglang_smoke.py --endpoint http://localhost:8888
"""
import argparse
import json
from pathlib import Path
import struct
import sys
import tempfile
import urllib.request
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coplot.server as server


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def run_case(endpoint, prompt, thinking=False):
    with tempfile.TemporaryDirectory(prefix="coplot-synthetic-") as directory:
        project = server.ProjectState.discover(Path(directory)).with_language("python")
        project.ensure_runtime()
        settings = server.ModelSettingsStore(project.model_settings_file, project.root / "defaults.json")
        server.model_settings_store = settings
        detected = server.fetch_models(endpoint)
        settings.write({"endpoint_url": endpoint, "model": detected["models"][0]["id"],
                        "language": "python", "endpoint_kind": detected["endpoint_kind"],
                        "supported_modalities": detected["supported_modalities"],
                        "reasoning_enabled": thinking, "max_tokens": 2048, "timeout_seconds": 60})
        image = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
                 + png_chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 64) * 64)) + png_chunk(b"IEND", b""))
        (project.plots_dir / "test.png").write_bytes(image)
        chat = server.ChatStore(project.chat_file)
        transcript = server.TranscriptStore(project.transcript_file)
        artifacts = server.ArtifactStore(project.artifacts_file, project.root)
        artifacts.sync_paths(server.artifact_roots(project), source="synthetic")
        jobs = server.ActiveJobStore()
        session = server.PythonSession(project, transcript, artifacts, jobs, project.plots_dir)
        shell = server.ShellSession(project, transcript, artifacts, jobs, project.root)
        context = server.ContextBuilder(project, chat, transcript, artifacts)
        agent = server.AgentService(project, chat, context, session, shell, settings)
        exchanges = []
        original = urllib.request.urlopen

        def capture(request, **kwargs):
            payload = json.loads(request.data)
            with original(request, **kwargs) as response:
                raw = response.read()
            exchanges.append({"request": payload, "response": json.loads(raw)})
            import io
            return io.BytesIO(raw)

        try:
            with patch("coplot.server.urllib.request.urlopen", side_effect=capture):
                result = agent.respond(prompt)
            return {"thinking": thinking, "capabilities": detected, "result": result,
                    "chat": chat.recent(None), "transcript": transcript.recent(None),
                    "durable_code": project.source_file.read_text(), "exchanges": exchanges}
        finally:
            session.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://localhost:8888")
    parser.add_argument("--output", default="/private/tmp/coplot-sglang-smoke.json")
    args = parser.parse_args()
    cases = {
        "six_steps": "This is a scratch-only protocol test. Execute exactly six separate coplot-run requests, one per model turn: print('STEP 1') then STEP 2 through STEP 6. Wait for each execution result before issuing the next. After step 6, answer Done. Do not edit the durable file or use shell commands.",
        "recovery_and_edit": "For a synthetic analysis, first run print(missing_values) in the live session, which will fail. Then repair by defining missing_values = [1, 2, 3, 4] and print their mean. Retain the working mean calculation in the durable file, including a comment containing the literal string ``` and a quoted string containing a newline. Run the working code and report the mean. Do not install packages.",
        "view": "Look at the plot test.png in the artifact ledger. Inspect its actual pixels and report its dominant color. Do not run code or shell commands or edit files.",
    }
    results = {}
    for name, prompt in cases.items():
        results[name] = run_case(args.endpoint, prompt)
        result = results[name]["result"]
        print(json.dumps({"case": name, "turns": result["turns"], "halt_reason": result["halt_reason"],
                          "actions": [(a["type"], a.get("status")) for a in result["actions"]],
                          "answer": result["message"]["content"]}), flush=True)
    results["view_thinking"] = run_case(args.endpoint, cases["view"], thinking=True)
    Path(args.output).write_text(json.dumps(results, indent=2))
    assert results["six_steps"]["result"]["turns"] > 4
    assert len([a for a in results["six_steps"]["result"]["actions"] if a["type"] == "execute_session"]) == 6
    for name in ("view", "view_thinking"):
        assert "red" in results[name]["result"]["message"]["content"].lower()
        assert any(a.get("status") == "attached" for a in results[name]["result"]["actions"])
    assert "missing_values" in results["recovery_and_edit"]["durable_code"]
    compile(results["recovery_and_edit"]["durable_code"], "synthetic.py", "exec")
    assert any("2.5" in entry.get("stdout", "") for entry in results["recovery_and_edit"]["transcript"])
    assert not any(case["result"]["halt_reason"] for case in results.values())
    print(f"Passed. Raw synthetic requests/responses saved to {args.output}")


if __name__ == "__main__":
    main()

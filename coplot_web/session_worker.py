from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import traceback
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plots-dir", required=True)
    args = parser.parse_args()
    session = WorkerSession(Path(args.plots_dir))
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = session.execute(
                str(request.get("code", "")),
                interactive=bool(request.get("interactive")),
            )
        except Exception:
            response = {
                "stdout": "",
                "stderr": traceback.format_exc(),
                "ok": False,
                "artifacts": [],
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class WorkerSession:
    def __init__(self, plots_dir: Path) -> None:
        self.plots_dir = plots_dir
        self.globals: dict[str, Any] = {
            "__name__": "__coplot_session__",
            "__package__": None,
            "__builtins__": __builtins__,
        }

    def execute(self, code: str, *, interactive: bool) -> dict[str, Any]:
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        ok = True
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            try:
                self._execute_code(code, interactive=interactive)
            except Exception:
                ok = False
                traceback.print_exc()
        return {
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "ok": ok,
            "artifacts": [],
        }

    def _execute_code(self, code: str, *, interactive: bool) -> None:
        if interactive:
            try:
                expression = compile(code, "<coplot-session>", "eval")
            except SyntaxError:
                pass
            else:
                value = eval(expression, self.globals)
                if value is not None:
                    print(repr(value))
                return
        exec(compile(code, "<coplot-session>", "exec"), self.globals)


if __name__ == "__main__":
    main()

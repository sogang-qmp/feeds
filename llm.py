"""Claude Code LLM interface via CLI subprocess."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")


def run_claude(prompt, model="haiku", timeout=600):
    """Run Claude Code in pipe mode and return result text."""
    base_dir = Path(__file__).parent
    tmp_dir = base_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    cmd = [
        CLAUDE_BIN, "-p", "-",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", model,
    ]

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(tmp_dir))
        os.close(fd)

        with open(tmp_path, "w") as stdout_f:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=stdout_f,
                stderr=subprocess.PIPE,
                cwd=str(base_dir),
            )
            _, stderr = proc.communicate(input=prompt.encode(), timeout=timeout)

        with open(tmp_path) as f:
            raw = f.read().strip()

        if not raw:
            raise RuntimeError(f"Empty Claude output. stderr: {stderr.decode()[:500]}")

        claude_out = json.loads(raw)
        if claude_out.get("is_error"):
            raise RuntimeError(claude_out.get("result", "unknown error"))
        return claude_out["result"]

    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"Claude subprocess timed out after {timeout}s")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def extract_json(text):
    """Extract JSON from Claude response text, handling ```json wrapping."""
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    # Try parsing the whole text
    return json.loads(text.strip())

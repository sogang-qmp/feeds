#!/usr/bin/env python3
"""Update research_profile.yaml from Cortex memory files.

Reads ~/drive/0_Cortex/memory/research_*.md, asks Claude to update
research_areas and keywords in the profile, preserving other sections.

Usage:
    python update_profile.py           # update in place
    python update_profile.py --dry-run # show diff without writing
"""

import argparse
import difflib
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

log = logging.getLogger("update_profile")

BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = Path.home() / "drive" / "0_Cortex" / "memory"
PROFILE_PATH = BASE_DIR / "research_profile.yaml"
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")


def load_memory_files():
    """Read all research_*.md files from Cortex memory."""
    files = sorted(MEMORY_DIR.glob("research_*.md"))
    contents = []
    for f in files:
        text = f.read_text()
        contents.append(f"### {f.name}\n{text}")
    return contents


def build_prompt(current_yaml: str, memories: list[str]) -> str:
    memory_block = "\n\n".join(memories)
    return f"""You are updating a research profile YAML used for scoring academic articles by relevance.

Below is the CURRENT profile YAML:
```yaml
{current_yaml}
```

Below are the researcher's CURRENT active research projects (from memory files):
{memory_block}

## Task
Update ONLY the `research_areas` and `keywords` sections to reflect the researcher's current and recent research interests based on the memory files above.

Rules:
- Output the COMPLETE updated YAML (all sections, not just the changed ones)
- Keep `researcher`, `opportunity_filters`, and any other sections EXACTLY as they are
- For `research_areas`: add/remove/reorder items under primary, secondary, methods, materials_systems to reflect current work
- For `keywords`: redistribute terms across strong/moderate/weak tiers based on current research focus. Add new relevant keywords implied by the research (e.g., CDW → "charge density wave"). Remove keywords no longer relevant.
- Update the "Last updated" date to {datetime.now().strftime('%Y-%m-%d')}
- Keep the same YAML style (comments, spacing)
- Output ONLY the YAML content, no markdown fences or explanation"""


def call_llm(prompt: str) -> str:
    """Run Claude Code in pipe mode and return result text."""
    tmp_dir = BASE_DIR / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    cmd = [
        CLAUDE_BIN, "-p", "-",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", "haiku",
    ]

    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(tmp_dir))
    os.close(fd)

    try:
        with open(tmp_path, "w") as stdout_f:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=stdout_f,
                stderr=subprocess.PIPE,
                cwd=str(BASE_DIR),
            )
            _, stderr = proc.communicate(input=prompt.encode(), timeout=300)

        with open(tmp_path) as f:
            raw = f.read().strip()

        if not raw:
            raise RuntimeError(f"Empty Claude output. stderr: {stderr.decode()[:500]}")

        claude_out = json.loads(raw)
        if claude_out.get("is_error"):
            raise RuntimeError(claude_out.get("result", "unknown error"))
        return claude_out["result"]
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(description="Update research profile from Cortex memory")
    parser.add_argument("--dry-run", action="store_true", help="Show diff without writing")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )

    # Load inputs
    current_yaml = PROFILE_PATH.read_text()
    memories = load_memory_files()

    if not memories:
        log.warning("No research memory files found in %s", MEMORY_DIR)
        sys.exit(0)

    log.info("Loaded %d research memory files", len(memories))

    # Call LLM
    prompt = build_prompt(current_yaml, memories)
    log.info("Calling Claude Code (haiku) to update profile...")
    new_yaml = call_llm(prompt)

    # Strip markdown fences if LLM included them
    if new_yaml.startswith("```"):
        lines = new_yaml.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        new_yaml = "\n".join(lines)

    # Validate YAML
    try:
        parsed = yaml.safe_load(new_yaml)
    except yaml.YAMLError as e:
        log.error("LLM returned invalid YAML: %s", e)
        sys.exit(1)

    # Sanity checks
    if "research_areas" not in parsed or "keywords" not in parsed:
        log.error("LLM output missing required sections")
        sys.exit(1)
    if "researcher" not in parsed:
        log.error("LLM output dropped researcher section")
        sys.exit(1)

    # Ensure trailing newline
    if not new_yaml.endswith("\n"):
        new_yaml += "\n"

    # Show diff
    diff = list(difflib.unified_diff(
        current_yaml.splitlines(keepends=True),
        new_yaml.splitlines(keepends=True),
        fromfile="research_profile.yaml (old)",
        tofile="research_profile.yaml (new)",
    ))

    if not diff:
        log.info("No changes needed")
        return

    diff_text = "".join(diff)
    if args.dry_run:
        print(diff_text)
        log.info("Dry run — no changes written")
        return

    # Write
    PROFILE_PATH.write_text(new_yaml)
    log.info("Updated %s", PROFILE_PATH)
    print(diff_text)


if __name__ == "__main__":
    main()

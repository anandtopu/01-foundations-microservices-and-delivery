"""Shared by the M9 exercises: run `docker compose` for THIS project from any directory.

`check=True` so a failed compose command stops the script instead of letting it print a verdict
about a kill that never happened (PR #7 review).
"""

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()

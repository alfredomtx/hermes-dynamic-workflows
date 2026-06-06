#!/usr/bin/env python3
"""Install a thin hermes-workflows wrapper for a cloned Hermes plugin."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def main() -> int:
    plugin_root = Path(__file__).resolve().parent.parent
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        target = bin_dir / "hermes-workflows.cmd"
        target.write_text(
            f'@echo off\r\nset "PYTHONPATH={plugin_root};%PYTHONPATH%"\r\n'
            f'"{sys.executable}" -m hermes_dynamic_workflows.tui.app %*\r\n',
            encoding="utf-8",
        )
    else:
        target = bin_dir / "hermes-workflows"
        target.write_text(
            "#!/bin/sh\n"
            f"PYTHONPATH={shlex.quote(str(plugin_root))}${{PYTHONPATH:+:$PYTHONPATH}} "
            f"exec {shlex.quote(sys.executable)} -m hermes_dynamic_workflows.tui.app \"$@\"\n",
            encoding="utf-8",
        )
        target.chmod(0o755)
    print(f"Installed {target}")
    print(f"Make sure {bin_dir} is on PATH, then run: hermes-workflows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

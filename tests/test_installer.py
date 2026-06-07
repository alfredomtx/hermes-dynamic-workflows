from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    def test_installed_wrapper_starts_tui_outside_plugin_directory(self):
        if os.name == "nt":
            self.skipTest("POSIX wrapper test")

        repository = Path(__file__).resolve().parent.parent
        installer = repository / "scripts" / "install-hermes-workflows.py"
        with tempfile.TemporaryDirectory() as home:
            environment = {**os.environ, "HOME": home}
            installed = subprocess.run(
                [sys.executable, str(installer)],
                cwd=Path(home),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            wrapper = Path(home) / ".local" / "bin" / "hermes-workflows"
            self.assertTrue(wrapper.is_file())
            self.assertTrue(os.access(wrapper, os.X_OK))

            launched = subprocess.run(
                [str(wrapper)],
                cwd=Path(home),
                env=environment,
                input="",
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertIn("Dynamic workflows", launched.stdout)
        self.assertIn("No workflow runs found.", launched.stdout)

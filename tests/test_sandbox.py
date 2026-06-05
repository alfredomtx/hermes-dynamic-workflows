from __future__ import annotations

import ast
import unittest

from hermes_dynamic_workflows.engine.config import PluginConfig
from hermes_dynamic_workflows.engine.errors import SandboxViolation
from hermes_dynamic_workflows.engine.sandbox import (
    LOOP_GUARD_NAME,
    extract_meta,
    parse_script,
)


class SandboxTests(unittest.TestCase):
    def test_blocks_import(self):
        with self.assertRaises(SandboxViolation):
            parse_script("import os\nreturn_value = 1", PluginConfig())

    def test_blocks_dunder_attribute(self):
        with self.assertRaises(SandboxViolation):
            parse_script("return_value = (1).__class__", PluginConfig())

    def test_allows_workflow_calls(self):
        tree = parse_script(
            """
meta = {"name": "ok"}

def workflow():
    return agent("hello")
""",
            PluginConfig(),
        )
        self.assertIsNotNone(tree)

    def test_allows_phase_objects(self):
        tree = parse_script(
            """
meta = {"name": "ok", "phases": [{"title": "Scan", "detail": "inspect", "model": "sonnet"}]}

def workflow():
    return agent("hello")
""",
            PluginConfig(),
        )
        meta = extract_meta(tree)
        self.assertEqual(meta["phases"][0]["title"], "Scan")


class ControlFlowAllowedTests(unittest.TestCase):
    """while/try/raise are pure control flow — now allowed (the docs' loop-
    until-budget / loop-until-dry / catch-gracefully patterns need them)."""

    def test_while_is_allowed(self):
        parse_script("while a > 0:\n    a = a - 1\n", PluginConfig())

    def test_try_except_exception_is_allowed(self):
        parse_script("try:\n    x = 1\nexcept Exception:\n    x = 2\n", PluginConfig())

    def test_raise_is_allowed(self):
        parse_script("raise Exception('boom')\n", PluginConfig())


class WildcardExceptForbiddenTests(unittest.TestCase):
    """A WorkflowHalt is BaseException, so `except Exception` can't catch it; we
    additionally forbid the wildcard forms that could."""

    def test_bare_except_rejected(self):
        with self.assertRaises(SandboxViolation):
            parse_script("try:\n    x = 1\nexcept:\n    x = 2\n", PluginConfig())

    def test_except_base_exception_rejected(self):
        with self.assertRaises(SandboxViolation):
            parse_script("try:\n    x = 1\nexcept BaseException:\n    x = 2\n", PluginConfig())

    def test_except_tuple_with_base_exception_rejected(self):
        with self.assertRaises(SandboxViolation):
            parse_script(
                "try:\n    x = 1\nexcept (ValueError, BaseException):\n    x = 2\n",
                PluginConfig(),
            )


class LoopInstrumentationTests(unittest.TestCase):
    def test_while_test_is_wrapped_with_guard(self):
        tree = parse_script("while a:\n    a = a - 1\n", PluginConfig())
        self.assertIn(LOOP_GUARD_NAME, ast.unparse(tree))

    def test_for_loop_not_instrumented(self):
        tree = parse_script("for i in range(3):\n    x = i\n", PluginConfig())
        self.assertNotIn(LOOP_GUARD_NAME, ast.unparse(tree))


if __name__ == "__main__":
    unittest.main()

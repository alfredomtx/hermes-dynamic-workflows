from __future__ import annotations

import os
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from hermes_dynamic_workflows.core.config import PluginConfig
from hermes_dynamic_workflows.run.manager import _approve_launch

META = {"name": "demo", "description": "a workflow"}


@contextmanager
def fake_approval(
    *,
    gateway=False,
    gateway_choice="once",
    notify_present=True,
    cli_choice="once",
    legacy_wait=False,
    gateway_timeout=1,
    install_touch=False,
):
    """Inject fake tools.approval / tools.terminal_tool so _approve_launch's
    channel logic can be exercised without the real Hermes engine."""
    appr = types.ModuleType("tools.approval")
    appr._is_gateway_approval_context = lambda: gateway
    appr.get_current_session_key = lambda default="default": "sess"
    appr._lock = threading.RLock()
    appr._gateway_queues = {}
    appr._get_approval_config = lambda: {"gateway_timeout": gateway_timeout}
    appr._fire_approval_hook = lambda *a, **k: None

    class ApprovalEntry:
        def __init__(self, data):
            self.event = threading.Event()
            self.data = data
            self.result = None

    appr._ApprovalEntry = ApprovalEntry

    def notify(*a, **k):
        queue = appr._gateway_queues.get("sess", [])
        if queue and gateway_choice != "timeout":
            queue[-1].result = gateway_choice
            queue[-1].event.set()

    appr._gateway_notify_cbs = {"sess": notify} if notify_present else {}
    if legacy_wait:
        appr._await_gateway_decision = lambda sk, cb, data, surface=None: {
            "resolved": True,
            "choice": gateway_choice,
        }
    appr.prompt_dangerous_approval = lambda command, description, approval_callback=None: cli_choice

    term = types.ModuleType("tools.terminal_tool")
    term._get_approval_callback = lambda: None

    pkg = types.ModuleType("tools")
    pkg.approval = appr
    pkg.terminal_tool = term
    modules = {"tools": pkg, "tools.approval": appr, "tools.terminal_tool": term}
    if install_touch:
        env_pkg = types.ModuleType("tools.environments")
        base = types.ModuleType("tools.environments.base")
        base.touch_activity_if_due = lambda state, label: (state["start"], state["last_touch"])
        pkg.environments = env_pkg
        env_pkg.base = base
        modules["tools.environments"] = env_pkg
        modules["tools.environments.base"] = base

    with patch.dict(sys.modules, modules):
        yield


class LaunchApprovalConfigTests(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(PluginConfig().require_launch_approval)


class LaunchApprovalDecisionTests(unittest.TestCase):
    def test_off_always_approves(self):
        approved, _ = _approve_launch(META, PluginConfig(require_launch_approval=False), None)
        self.assertTrue(approved)

    def test_gateway_approve(self):
        with fake_approval(gateway=True, gateway_choice="once"):
            approved, _ = _approve_launch(META, PluginConfig(), None)
        self.assertTrue(approved)

    def test_gateway_deny(self):
        with fake_approval(gateway=True, gateway_choice="deny"):
            approved, reason = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)
        self.assertIn("denied", reason)

    def test_gateway_legacy_wait_compat(self):
        with fake_approval(gateway=True, gateway_choice="once", legacy_wait=True):
            approved, _ = _approve_launch(META, PluginConfig(), None)
        self.assertTrue(approved)

    def test_gateway_no_channel_denies(self):
        with fake_approval(gateway=True, notify_present=False):
            approved, reason = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)
        self.assertIn("no gateway approval channel", reason)

    def test_gateway_timeout_activity_state_is_initialized(self):
        with fake_approval(gateway=True, gateway_choice="timeout", gateway_timeout=0.01, install_touch=True):
            approved, reason = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)
        self.assertIn("timed out", reason)

    def test_cli_approve(self):
        with fake_approval(gateway=False, cli_choice="once"), \
                patch.dict(os.environ, {"HERMES_INTERACTIVE": "1"}):
            approved, _ = _approve_launch(META, PluginConfig(), None)
        self.assertTrue(approved)

    def test_cli_deny(self):
        with fake_approval(gateway=False, cli_choice="deny"), \
                patch.dict(os.environ, {"HERMES_INTERACTIVE": "1"}):
            approved, _ = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)

    def test_headless_no_channel_denies(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES_INTERACTIVE"}
        with fake_approval(gateway=False, notify_present=False), patch.dict(os.environ, env, clear=True):
            approved, reason = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)
        self.assertIn("no interactive channel", reason)

    def test_ambient_gateway_session_key_recovers_lost_context(self):
        with fake_approval(gateway=False, gateway_choice="once", notify_present=True), patch.dict(
            os.environ,
            {"HERMES_SESSION_KEY": "sess"},
            clear=True,
        ):
            approved, _ = _approve_launch(META, PluginConfig(), None)
        self.assertTrue(approved)

    def test_lost_context_does_not_guess_unrelated_single_gateway_channel(self):
        with fake_approval(gateway=False, gateway_choice="once", notify_present=True), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            approved, reason = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)
        self.assertIn("no interactive channel", reason)

    def test_cron_does_not_recover_unrelated_single_gateway_channel(self):
        with fake_approval(gateway=False, gateway_choice="once", notify_present=True), patch.dict(
            os.environ,
            {"HERMES_CRON_SESSION": "1"},
            clear=True,
        ):
            approved, reason = _approve_launch(META, PluginConfig(), None)
        self.assertFalse(approved)
        self.assertIn("no interactive channel", reason)


if __name__ == "__main__":
    unittest.main()

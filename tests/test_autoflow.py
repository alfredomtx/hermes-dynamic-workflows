from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from hermes_dynamic_workflows.core import autoflow
from hermes_dynamic_workflows.core.autoflow import (
    AutoflowState,
    apply_steering,
    is_substantive,
    parse_toggle_command,
)
from hermes_dynamic_workflows.core.config import PluginConfig
from hermes_dynamic_workflows.adapters import autoflow_hook
from hermes_dynamic_workflows.adapters.autoflow_hook import decide, pre_gateway_dispatch_handler


class ParseToggleTests(unittest.TestCase):
    def test_on_variants(self):
        for raw in ("/autoflow on", "!autoflow enable", "autoflow start", "/autoflow 1", "/AUTOFLOW ON"):
            self.assertEqual(parse_toggle_command(raw), "on", raw)

    def test_off_variants(self):
        for raw in ("/autoflow off", "autoflow disable", "/autoflow stop", "/autoflow 0"):
            self.assertEqual(parse_toggle_command(raw), "off", raw)

    def test_bare_and_status(self):
        self.assertEqual(parse_toggle_command("/autoflow"), "status")
        self.assertEqual(parse_toggle_command("autoflow status"), "status")
        # Unrecognized arg degrades to status, never a silent flip.
        self.assertEqual(parse_toggle_command("/autoflow maybe"), "status")

    def test_not_a_command(self):
        for raw in ("", "  ", "please refactor autoflow handling", "/workflows", "autoflowing rivers"):
            self.assertIsNone(parse_toggle_command(raw), raw)


class SubstantiveTests(unittest.TestCase):
    def test_short_or_command_is_not_substantive(self):
        self.assertFalse(is_substantive("ok", 24))
        self.assertFalse(is_substantive("thanks!", 24))
        self.assertFalse(is_substantive("/autoflow on", 24))
        self.assertFalse(is_substantive("!run", 24))
        self.assertFalse(is_substantive("   ", 24))

    def test_long_message_is_substantive(self):
        self.assertTrue(is_substantive("audit every endpoint under src/routes for missing auth", 24))

    def test_threshold_is_inclusive(self):
        self.assertTrue(is_substantive("x" * 24, 24))
        self.assertFalse(is_substantive("x" * 23, 24))


class ApplySteeringTests(unittest.TestCase):
    def test_appends_directive(self):
        out = apply_steering("refactor the worker pool")
        self.assertIn("refactor the worker pool", out)
        self.assertIn("[autoflow on]", out)

    def test_idempotent(self):
        once = apply_steering("do the thing")
        twice = apply_steering(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("[autoflow on]"), 1)


class AutoflowStateTests(unittest.TestCase):
    def test_sticky_per_session(self):
        st = AutoflowState()
        self.assertFalse(st.is_on("s1"))
        self.assertTrue(st.set("s1", True))
        self.assertTrue(st.is_on("s1"))
        # Other session unaffected.
        self.assertFalse(st.is_on("s2"))
        self.assertFalse(st.set("s1", False))
        self.assertFalse(st.is_on("s1"))

    def test_empty_session_key_is_noop(self):
        st = AutoflowState()
        self.assertFalse(st.set("", True))
        self.assertFalse(st.is_on(""))

    def test_default_on_semantics(self):
        st = AutoflowState()
        # Unset session resolves to the passed default.
        self.assertFalse(st.is_on("s1", False))
        self.assertTrue(st.is_on("s1", True))
        # Explicit off survives even under default-on.
        st.set("s1", False)
        self.assertFalse(st.is_on("s1", True))
        # Explicit on survives even under default-off.
        st.set("s2", True)
        self.assertTrue(st.is_on("s2", False))
        # clear() drops the explicit choice -> back to default.
        st.clear("s1")
        self.assertTrue(st.is_on("s1", True))
        self.assertFalse(st.is_on("s1", False))


class DecideTests(unittest.TestCase):
    def test_toggle_takes_priority(self):
        self.assertEqual(decide("/autoflow on", is_on=False, min_chars=24)["kind"], "toggle")

    def test_steer_when_on_and_substantive(self):
        d = decide("audit every endpoint under src/routes for auth", is_on=True, min_chars=24)
        self.assertEqual(d["kind"], "steer")
        self.assertIn("[autoflow on]", d["text"])

    def test_pass_when_off(self):
        self.assertEqual(decide("audit everything in the repo for bugs", is_on=False, min_chars=24)["kind"], "pass")

    def test_pass_when_on_but_trivial(self):
        self.assertEqual(decide("ok thanks", is_on=True, min_chars=24)["kind"], "pass")


# --- Fakes for the gateway-coupled handler -----------------------------------

class FakeSource:
    def __init__(self, platform="telegram", chat_id="c1", thread_id=None):
        self.platform = platform
        self.chat_id = chat_id
        self.thread_id = thread_id


class FakeEvent:
    def __init__(self, text, source):
        self.text = text
        self.source = source


class FakeAdapter:
    def __init__(self):
        self.sends = []

    async def send(self, chat_id, content, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})


class FakeGateway:
    def __init__(self, session_key="agent:main:telegram:group:c1:7"):
        self._session_key = session_key
        self.adapters = {"telegram": FakeAdapter()}
        self.reasoning_overrides = {}

    def _session_key_for_source(self, source):
        return self._session_key

    def _set_session_reasoning_override(self, session_key, cfg):
        self.reasoning_overrides[session_key] = cfg


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Isolate the module-level state between tests.
        autoflow._STATE = AutoflowState()
        # Pin config to shipped defaults (default-off) so these tests are
        # hermetic and don't read the host's live config.yaml (which may set
        # auto_workflow_default_on=true for benchmarking).
        self._cfg_patch = patch.object(autoflow_hook, "load_config", return_value=PluginConfig())
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()

    async def test_toggle_on_skips_and_confirms_and_does_not_steer_yet(self):
        gw = FakeGateway()
        src = FakeSource(thread_id="7")
        result = pre_gateway_dispatch_handler(event=FakeEvent("/autoflow on", src), gateway=gw)
        self.assertEqual(result, {"action": "skip", "reason": "autoflow-toggle"})
        self.assertTrue(autoflow.state().is_on(gw._session_key))
        # Confirmation send is scheduled as a task; let the loop run it.
        await asyncio.sleep(0)
        sends = gw.adapters["telegram"].sends
        self.assertEqual(len(sends), 1)
        self.assertIn("autoflow ON", sends[0]["content"])
        self.assertNotIn("reasoning effort", sends[0]["content"])
        # Topic targeting preserved.
        self.assertEqual(sends[0]["metadata"], {"thread_id": "7"})

    async def test_steer_rewrites_without_changing_parent_effort(self):
        gw = FakeGateway()
        src = FakeSource(thread_id="7")
        autoflow.state().set(gw._session_key, True)
        msg = "audit every API endpoint under src/routes for missing auth checks"
        result = pre_gateway_dispatch_handler(event=FakeEvent(msg, src), gateway=gw)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("[autoflow on]", result["text"])
        self.assertIn(msg, result["text"])
        self.assertEqual(gw.reasoning_overrides, {})

    async def test_off_clears_state_without_changing_parent_effort(self):
        gw = FakeGateway()
        src = FakeSource(thread_id="7")
        autoflow.state().set(gw._session_key, True)
        existing = {"enabled": True, "effort": "low"}
        gw.reasoning_overrides[gw._session_key] = existing
        result = pre_gateway_dispatch_handler(event=FakeEvent("/autoflow off", src), gateway=gw)
        self.assertEqual(result, {"action": "skip", "reason": "autoflow-toggle"})
        self.assertFalse(autoflow.state().is_on(gw._session_key))
        self.assertEqual(gw.reasoning_overrides.get(gw._session_key), existing)

    async def test_plugin_config_has_no_autoflow_effort_setting(self):
        self.assertFalse(hasattr(PluginConfig(), "auto_workflow_effort"))

    async def test_pass_when_off(self):
        gw = FakeGateway()
        src = FakeSource()
        result = pre_gateway_dispatch_handler(
            event=FakeEvent("audit the whole repo for bugs and fix them", src), gateway=gw
        )
        self.assertIsNone(result)
        self.assertEqual(gw.reasoning_overrides, {})

    async def test_missing_event_or_gateway_is_noop(self):
        self.assertIsNone(pre_gateway_dispatch_handler(event=None, gateway=FakeGateway()))
        self.assertIsNone(pre_gateway_dispatch_handler(event=FakeEvent("hi", FakeSource()), gateway=None))

    async def test_empty_session_key_bails(self):
        gw = FakeGateway(session_key="")
        result = pre_gateway_dispatch_handler(event=FakeEvent("/autoflow on", FakeSource()), gateway=gw)
        self.assertIsNone(result)


class DefaultOnHandlerTests(unittest.IsolatedAsyncioTestCase):
    """auto_workflow_default_on=True: fresh sessions steer without /autoflow on,
    and an explicit /autoflow off still wins."""

    def setUp(self):
        autoflow._STATE = AutoflowState()

    async def test_default_on_steers_fresh_session(self):
        gw = FakeGateway()
        src = FakeSource(thread_id="7")
        msg = "audit every API endpoint under src/routes for missing auth checks across the app"
        with patch.object(autoflow_hook, "load_config", return_value=PluginConfig(auto_workflow_default_on=True)):
            # No /autoflow on issued — default-on should steer anyway.
            result = pre_gateway_dispatch_handler(event=FakeEvent(msg, src), gateway=gw)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("[autoflow on]", result["text"])
        self.assertEqual(gw.reasoning_overrides, {})

    async def test_default_on_explicit_off_disables_session(self):
        gw = FakeGateway()
        src = FakeSource(thread_id="7")
        msg = "audit every API endpoint under src/routes for missing auth checks across the app"
        with patch.object(autoflow_hook, "load_config", return_value=PluginConfig(auto_workflow_default_on=True)):
            # Explicit off records a sticky False that beats default-on.
            off = pre_gateway_dispatch_handler(event=FakeEvent("/autoflow off", src), gateway=gw)
            self.assertEqual(off, {"action": "skip", "reason": "autoflow-toggle"})
            result = pre_gateway_dispatch_handler(event=FakeEvent(msg, src), gateway=gw)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

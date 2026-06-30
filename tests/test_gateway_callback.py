from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes_dynamic_workflows.adapters import gateway_callback as gc_module
from hermes_dynamic_workflows.adapters.gateway_callback import on_gateway_callback
from hermes_dynamic_workflows.core.config import PluginConfig
from hermes_dynamic_workflows.run import manager as manager_module
from hermes_dynamic_workflows.run.manager import (
    _accepts_buttons,
    _control_buttons_for,
    _stop_buttons_for,
)


class GatewayCallbackHandlerTests(unittest.TestCase):
    def test_non_wf_data_returns_none(self):
        self.assertIsNone(on_gateway_callback(data="ea:once:5", authorized=True))
        self.assertIsNone(on_gateway_callback(data="", authorized=True))
        self.assertIsNone(on_gateway_callback(data=None, authorized=True))

    def test_unauthorized_does_not_call_stop_task(self):
        with patch.object(gc_module, "get_run_manager") as grm:
            directive = on_gateway_callback(data="wf:stop:wg123", authorized=False)
            grm.assert_not_called()
        self.assertTrue(directive["handled"])
        self.assertIn("Not authorized", directive["answer"])
        self.assertFalse(directive["strip_buttons"])

    def test_authorized_stop_calls_stop_task_and_strips(self):
        class FakeManager:
            def __init__(self):
                self.called_with = None

            def stop_task(self, task_id):
                self.called_with = task_id
                return {"message": "ok", "task_id": task_id}

        fake = FakeManager()
        with patch.object(gc_module, "get_run_manager", return_value=fake):
            directive = on_gateway_callback(data="wf:stop:wg123", authorized=True)
        self.assertEqual(fake.called_with, "wg123")
        self.assertTrue(directive["handled"])
        self.assertIn("Stopping", directive["answer"])
        self.assertTrue(directive["strip_buttons"])
        self.assertIsNone(directive["edit_text"])

    def test_unknown_task_reports_already_finished(self):
        class FakeManager:
            def stop_task(self, task_id):
                return None

        with patch.object(gc_module, "get_run_manager", return_value=FakeManager()):
            directive = on_gateway_callback(data="wf:stop:gone", authorized=True)
        self.assertTrue(directive["handled"])
        self.assertIn("already finished", directive["answer"].lower())
        self.assertTrue(directive["strip_buttons"])

    def test_empty_task_id_invalid(self):
        with patch.object(gc_module, "get_run_manager") as grm:
            directive = on_gateway_callback(data="wf:stop:", authorized=True)
            grm.assert_not_called()
        self.assertTrue(directive["handled"])
        self.assertIn("Invalid", directive["answer"])

    def test_stop_task_exception_is_swallowed(self):
        class FakeManager:
            def stop_task(self, task_id):
                raise RuntimeError("boom")

        with patch.object(gc_module, "get_run_manager", return_value=FakeManager()):
            directive = on_gateway_callback(data="wf:stop:wg123", authorized=True)
        self.assertTrue(directive["handled"])
        self.assertTrue(directive["strip_buttons"])

    def test_authorized_pause_calls_manager_pause(self):
        class FakeManager:
            def __init__(self):
                self.called_with = None

            def pause(self, run_id):
                self.called_with = run_id
                return True

        fake = FakeManager()
        with patch.object(gc_module, "get_run_manager", return_value=fake):
            directive = on_gateway_callback(data="wf:pause:wf_abc123", authorized=True)
        self.assertEqual(fake.called_with, "wf_abc123")
        self.assertIn("Paused", directive["answer"])
        self.assertFalse(directive["strip_buttons"])

    def test_authorized_resume_calls_manager_resume(self):
        class FakeManager:
            def resume(self, run_id):
                self.called_with = run_id
                return True

        fake = FakeManager()
        with patch.object(gc_module, "get_run_manager", return_value=fake):
            directive = on_gateway_callback(data="wf:resume:wf_abc123", authorized=True)
        self.assertEqual(fake.called_with, "wf_abc123")
        self.assertIn("Resumed", directive["answer"])

    def test_authorized_restart_calls_manager_restart_and_strips(self):
        class FakeManager:
            def restart(self, run_id):
                self.called_with = run_id
                return {"runId": "wf_new123"}

        fake = FakeManager()
        with patch.object(gc_module, "get_run_manager", return_value=fake):
            directive = on_gateway_callback(data="wf:restart:wf_old123", authorized=True)
        self.assertEqual(fake.called_with, "wf_old123")
        self.assertIn("wf_new123", directive["answer"])
        self.assertTrue(directive["strip_buttons"])

    def test_authorized_rerun_maps_to_restart(self):
        class FakeManager:
            def restart(self, run_id):
                self.called_with = run_id
                return {"runId": "wf_new123"}

        fake = FakeManager()
        with patch.object(gc_module, "get_run_manager", return_value=fake):
            directive = on_gateway_callback(data="wf:rerun:wf_old123", authorized=True)
        self.assertEqual(fake.called_with, "wf_old123")
        self.assertIn("Rerun", directive["answer"])


class StopButtonHelperTests(unittest.TestCase):
    def _record(self, status="running", task_id="wg123"):
        return {"status": status, "taskId": task_id, "runId": "wf_abc123", "scriptPath": "/tmp/workflow.py"}

    def test_stoppable_states_render_button(self):
        cfg = PluginConfig()
        for status in ("queued", "running", "paused"):
            buttons = _stop_buttons_for(self._record(status=status), cfg)
            self.assertIsNotNone(buttons)
            self.assertEqual(buttons[0]["callback_data"], "wf:stop:wg123")
            self.assertIn("Stop", buttons[0]["text"])

    def test_stopping_state_excluded(self):
        # stop_task returns None for "stopping" -> no button (avoids misleading toast).
        self.assertIsNone(_stop_buttons_for(self._record(status="stopping"), PluginConfig()))

    def test_terminal_states_no_button(self):
        cfg = PluginConfig()
        for status in ("completed", "failed", "stopped", "interrupted"):
            self.assertIsNone(_stop_buttons_for(self._record(status=status), cfg))

    def test_disabled_by_config(self):
        cfg = PluginConfig(notify_progress_stop_button=False)
        self.assertIsNone(_stop_buttons_for(self._record(), cfg))

    def test_no_task_id_no_button(self):
        self.assertIsNone(_stop_buttons_for(self._record(task_id=""), PluginConfig()))

    def test_callback_data_under_telegram_cap(self):
        buttons = _stop_buttons_for(self._record(task_id="wg8nxqxzq"), PluginConfig())
        self.assertLessEqual(len(buttons[0]["callback_data"].encode("utf-8")), 64)

    def test_running_control_buttons_include_pause_stop_restart(self):
        buttons = _control_buttons_for(self._record(status="running"), PluginConfig())
        callbacks = [button["callback_data"] for button in buttons]
        self.assertIn("wf:pause:wf_abc123", callbacks)
        self.assertIn("wf:stop:wg123", callbacks)
        self.assertIn("wf:restart:wf_abc123", callbacks)

    def test_paused_control_buttons_include_resume_stop_restart(self):
        buttons = _control_buttons_for(self._record(status="paused"), PluginConfig())
        callbacks = [button["callback_data"] for button in buttons]
        self.assertIn("wf:resume:wf_abc123", callbacks)
        self.assertIn("wf:stop:wg123", callbacks)
        self.assertIn("wf:restart:wf_abc123", callbacks)

    def test_terminal_control_buttons_include_rerun(self):
        buttons = _control_buttons_for(self._record(status="completed"), PluginConfig())
        self.assertEqual(buttons[0]["callback_data"], "wf:rerun:wf_abc123")
        self.assertIn("Rerun", buttons[0]["text"])

    def test_open_log_url_button_when_http_url_exists(self):
        record = self._record(status="running")
        record["logUrl"] = "https://example.com/log"
        buttons = _control_buttons_for(record, PluginConfig())
        self.assertEqual(buttons[1][0]["text"], "📄 Open log")
        self.assertEqual(buttons[1][0]["url"], "https://example.com/log")


class AcceptsButtonsProbeTests(unittest.TestCase):
    def test_method_with_buttons_param(self):
        def send(self, chat_id, content, buttons=None):
            pass
        self.assertTrue(_accepts_buttons(send))

    def test_method_with_var_keyword(self):
        def send(self, chat_id, content, **kwargs):
            pass
        self.assertTrue(_accepts_buttons(send))

    def test_method_without_buttons(self):
        def send(self, chat_id, content, metadata=None):
            pass
        self.assertFalse(_accepts_buttons(send))


if __name__ == "__main__":
    unittest.main()

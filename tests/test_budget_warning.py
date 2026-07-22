from __future__ import annotations

import unittest

from hermes_dynamic_workflows.core.budget_warning import find_budget_warnings


COMMON_OPTIONS = (
    '"provider": "openai-codex", '
    '"model": "gpt-5.6-luna", '
    '"reasoningEffort": "high", '
    '"maxTurns": {max_turns}, '
    '"maxToolCalls": {max_tool_calls}, '
    '"maxToolOutputChars": {max_output}'
)


def script_for(options: str, *, phases: str = '[{"title": "Inspect"}, {"title": "Synthesize"}]') -> str:
    return f'''meta = {{"name": "budget-check", "description": "Budget check", "phases": {phases}}}

phase("Inspect")
return await agent("inspect the repository", {{{options}}})
'''


class BudgetWarningTests(unittest.TestCase):
    def test_warns_for_low_literal_budget_on_default_tool_surface(self):
        script = script_for(
            COMMON_OPTIONS.format(max_turns=20, max_tool_calls=31, max_output=250000)
            + ', "label": "hunter"'
        )

        warnings = find_budget_warnings(script)

        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertIn("hunter", warning)
        self.assertIn("index 0", warning)
        self.assertIn("maxTurns=20", warning)
        self.assertIn("hard ceilings", warning)
        self.assertIn("launch continues", warning)
        self.assertIn("pipeline", warning)
        self.assertIn("increase only the limiting dimension", warning)

    def test_warns_for_low_dimensions_on_broad_explicit_toolsets(self):
        script = script_for(
            COMMON_OPTIONS.format(max_turns=30, max_tool_calls=20, max_output=200000)
            + ', "label": "synth", "toolsets": ["file", "web"]'
        )

        warnings = find_budget_warnings(script)

        self.assertEqual(len(warnings), 1)
        self.assertIn("maxToolCalls=20", warnings[0])
        self.assertIn("maxToolOutputChars=200000", warnings[0])

    def test_does_not_warn_for_single_phase_workflow(self):
        script = script_for(
            COMMON_OPTIONS.format(max_turns=1, max_tool_calls=1, max_output=1),
            phases='[{"title": "Only phase"}]',
        )

        self.assertEqual(find_budget_warnings(script), ())

    def test_does_not_warn_for_narrow_or_toolless_children(self):
        options_template = COMMON_OPTIONS.format(max_turns=1, max_tool_calls=1, max_output=1)
        scripts = (
            script_for(options_template + ', "toolsets": []'),
            script_for(options_template + ', "allowedTools": []'),
            script_for(options_template + ', "toolsets": ["file"]'),
            script_for(
                options_template
                + ', "agentType": "synthesizer", "toolsets": []'
            ),
        )

        for script in scripts:
            with self.subTest(script=script):
                self.assertEqual(find_budget_warnings(script), ())

    def test_literal_none_allowed_tools_inherits_default_surface(self):
        script = script_for(
            COMMON_OPTIONS.format(max_turns=20, max_tool_calls=30, max_output=300000)
            + ', "allowedTools": None'
        )

        warnings = find_budget_warnings(script)

        self.assertEqual(len(warnings), 1)
        self.assertIn("maxTurns=20", warnings[0])

    def test_does_not_guess_dynamic_phases_options_or_budgets(self):
        dynamic_options = '''meta = {"name": "dynamic", "description": "Dynamic", "phases": ["A", "B"]}

limit = 1
opts = {"maxTurns": limit, "maxToolCalls": 1, "maxToolOutputChars": 1}
return await agent("inspect", opts)
'''
        dynamic_budget = '''meta = {"name": "dynamic", "description": "Dynamic", "phases": ["A", "B"]}

limit = 1
return await agent("inspect", {"maxTurns": limit, "maxToolCalls": 1, "maxToolOutputChars": 1})
'''
        dynamic_phases = '''phase_names = ["A", "B"]
meta = {"name": "dynamic", "description": "Dynamic", "phases": phase_names}

return await agent("inspect", {"maxTurns": 1, "maxToolCalls": 1, "maxToolOutputChars": 1})
'''

        for script in (dynamic_options, dynamic_budget, dynamic_phases):
            with self.subTest(script=script):
                self.assertEqual(find_budget_warnings(script), ())

    def test_warning_identifies_index_when_label_is_missing(self):
        script = '''meta = {"name": "indexed", "description": "Indexed", "phases": ["A", "B"]}

first = await agent("first", {"maxTurns": 30, "maxToolCalls": 30, "maxToolOutputChars": 300000})
second = await agent("second", {"maxTurns": 20, "maxToolCalls": 30, "maxToolOutputChars": 300000})
return [first, second]
'''

        warnings = find_budget_warnings(script)

        self.assertEqual(len(warnings), 1)
        self.assertIn("index 1", warnings[0])


if __name__ == "__main__":
    unittest.main()

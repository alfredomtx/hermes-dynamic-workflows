from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


class DirectoryPluginTests(unittest.TestCase):
    def test_root_entrypoint_loads_as_hermes_namespace_package(self):
        plugin_root = Path(__file__).resolve().parent.parent
        namespace = "hermes_plugins"
        module_name = f"{namespace}.dynamic_workflows"

        parent = sys.modules.get(namespace)
        if parent is None:
            parent = types.ModuleType(namespace)
            parent.__path__ = []
            sys.modules[namespace] = parent

        spec = importlib.util.spec_from_file_location(
            module_name,
            plugin_root / "__init__.py",
            submodule_search_locations=[str(plugin_root)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        module.__path__ = [str(plugin_root)]
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.register))
        finally:
            for name in list(sys.modules):
                if name == module_name or name.startswith(f"{module_name}."):
                    sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()

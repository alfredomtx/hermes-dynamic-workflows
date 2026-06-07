"""Hermes directory-plugin entrypoint for dynamic workflows."""

from __future__ import annotations

if __package__:
    from .hermes_dynamic_workflows.entry import register
else:
    from hermes_dynamic_workflows.entry import register

__all__ = ["register"]

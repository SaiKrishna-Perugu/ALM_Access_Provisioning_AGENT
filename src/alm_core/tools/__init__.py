"""Typed, idempotent tools. Every write in the system goes through one of these.

The LLM never calls a tool directly and never constructs one of their arguments:
it proposes values, the Pydantic contracts in :mod:`alm_core.models` validate
them, and only then does a tool see them. That is what "the LLM is never in the
write path" means in practice.
"""
from .base import ToolContext, gather_per_user, guarded_write, record, to_thread

__all__ = ["ToolContext", "gather_per_user", "guarded_write", "record", "to_thread",
           "ewm", "jts", "evidence", "gpt_queue"]

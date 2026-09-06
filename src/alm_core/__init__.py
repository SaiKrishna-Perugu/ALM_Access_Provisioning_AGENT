"""Core library for the autonomous ALM provisioning system.

Layering, outermost first:

    alm_api      FastAPI: webhook receiver, approval endpoints, run queries
    alm_agents   LangGraph supervisor and the agent nodes
    alm_core     this package - contracts, tools, persistence, transport
    alm_worker   the domain-joined Windows worker (AD group changes)

Nothing in ``alm_core`` imports from the layers above it, and nothing here
imports an LLM client. The model lives in ``alm_agents`` and can only reach the
world through the validated contracts in :mod:`alm_core.models`.

The original CLI (``src/*.py``) is untouched and still runs standalone; this
package reimplements the same operations with the guarantees an unattended
system needs - typed errors, idempotency, durable audit, and an approval a
human actually gave.
"""
from . import errors, logging, models

__all__ = ["errors", "logging", "models"]
__version__ = "2.0.0"

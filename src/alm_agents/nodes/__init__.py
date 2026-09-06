"""The agent nodes. One responsibility each, wired together in ``graph.py``.

Only two of the nine touch a language model, and neither can cause a write:

  extraction  regex first; the model is a fallback for rows that failed to parse
  closure     the model may rephrase a line the template already produced

Everything else - intake, validation, approval, JTS provisioning, AD queueing,
verification, evidence, audit - is deterministic.
"""
from .approval import make_approval_node
from .auditor import make_auditor_node
from .closure import make_closure_node
from .evidence import make_evidence_node
from .extraction import make_extraction_node
from .intake import make_intake_node
from .provisioning import make_ad_node, make_jts_node, make_verification_node
from .validation import make_validation_node

__all__ = [
    "make_intake_node",
    "make_extraction_node",
    "make_validation_node",
    "make_approval_node",
    "make_jts_node",
    "make_ad_node",
    "make_verification_node",
    "make_evidence_node",
    "make_closure_node",
    "make_auditor_node",
]

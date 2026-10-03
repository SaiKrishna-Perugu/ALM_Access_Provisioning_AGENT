"""The domain-joined Windows worker for AD group changes.

Separate from the orchestrator because GPT authenticates with Windows Kerberos
SSO, which cannot be reproduced in a Linux container. The worker consumes jobs
from the shared job queue (or Pub/Sub), drives the GPT UI as the service
account, and records outcomes in the same ledger and audit table the
orchestrator uses - so a job appears in one audit trail regardless of which
host performed it.
"""
from .gpt import GptSession, submit_outcome

__all__ = ["GptSession", "submit_outcome"]

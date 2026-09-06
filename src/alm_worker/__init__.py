"""The domain-joined Windows worker for AD group changes.

Separate from the orchestrator because GPT authenticates with Windows Kerberos
SSO, which cannot be reproduced in a Linux container. The worker consumes jobs
from Service Bus, drives the GPT UI as the service account, and records outcomes
in the same ledger and audit table the orchestrator uses - so a job appears in
one audit trail regardless of which host performed it.
"""
from .gpt import GptSession, parse_submit_body

__all__ = ["GptSession", "parse_submit_body"]

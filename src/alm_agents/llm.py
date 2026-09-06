"""The only place a language model is called, and the rules that constrain it.

Three constraints, enforced here rather than trusted to prompts:

1. **Never in the write path.** Every function returns *proposals* validated
   against the Pydantic contracts. A hallucinated user ID fails ``USERID_PATTERN``
   and is dropped before any tool sees it.
2. **PII is minimised before the call.** Deterministic parsing runs first and
   the model is a fallback; when it is reached, e-mail addresses and display
   names are redacted from the prompt. The user ID survives only because it is
   the thing being extracted.
3. **Output is structurally validated.** Free text is never used as-is: comment
   drafts must match a template check, and extraction output must parse into
   models. A model that returns something unexpected produces no work, not
   surprising work.

If Vertex AI is not configured, or ``llm_enabled`` is false, every function
degrades to "no proposal" and the deterministic path stands alone. The system is
designed to be fully functional with the LLM switched off.
"""
from __future__ import annotations

import json
import re

from alm_core.logging import get_logger, redact_pii
from alm_core.models import USERID_PATTERN, RequestedUser, SourceWorkItem

log = get_logger("alm.llm")

EXTRACTION_SYSTEM = """You extract user IDs from a malformed access-request field.

The well-formed format is: LASTNAME,FIRSTNAME,email,USERID; repeated.
The text you are given failed to parse. Recover only what is genuinely present.

Rules:
- A user ID matches ^[A-Za-z]{1,3}[0-9][0-9A-Za-z]{3,8}$ (e.g. SF58083, T0195G3).
- Never invent, complete or correct a user ID. If you are not certain a token is
  a user ID as written, omit it.
- Names and e-mail addresses have been redacted; do not try to reconstruct them.
- Return JSON only: {"users": [{"userid": "...", "confidence": 0.0-1.0}]}
- An empty list is a correct and useful answer."""

COMMENT_SYSTEM = """You write one short status line for an ALM work-item comment.

You are given a fact: what the provisioning system actually did for a user.
Restate that fact in one clear sentence. Do not add, soften or embellish it.
Never claim an action that is not in the fact you were given.
Return the sentence only, with no preamble and no formatting."""

# A drafted comment must survive this: it may not assert an action the system
# did not record. Cheap, and it is the exact failure mode that put "User added
# to JTS" on eleven work items for users nobody added.
_FORBIDDEN_CLAIMS = {
    "created": (),
    "already_active": ("added", "created", "imported", "provisioned"),
    "unarchived": ("created", "imported"),
    "unknown": ("added", "created", "imported", "provisioned", "granted"),
}


def _is_anthropic(model: str) -> bool:
    """Claude models are served through Vertex Model Garden by a different client."""
    return model.lower().startswith("claude")


def _client(settings, *, model: str = "", max_tokens: int = 0,
            temperature: float | None = None):
    """Build a Vertex AI chat model, or None when the LLM is switched off.

    Authentication is Application Default Credentials - on Cloud Run that is the
    attached service account, so there is no API key anywhere in the system.

    Gemini and Claude are both first-class on Vertex; the model id decides which
    client is constructed, so moving between them is a configuration change
    rather than a code change.
    """
    if not settings.llm_enabled or not settings.project_id:
        return None

    model = model or settings.agent_model
    kwargs = {
        "model_name": model,
        "project": settings.project_id,
        "location": settings.vertex_region,
        "temperature": (settings.agent_temperature if temperature is None
                        else temperature),
        "max_output_tokens": max_tokens or settings.llm_max_output_tokens,
        "max_retries": 2,
    }

    try:
        if _is_anthropic(model):
            from langchain_google_vertexai.model_garden import ChatAnthropicVertex

            # The Anthropic client on Vertex spells two of these differently.
            return ChatAnthropicVertex(
                model_name=model,
                project=settings.project_id,
                location=settings.vertex_region,
                temperature=kwargs["temperature"],
                max_tokens=kwargs["max_output_tokens"],
            )
        from langchain_google_vertexai import ChatVertexAI

        return ChatVertexAI(**kwargs)
    except ImportError:
        log.warning("llm_unavailable",
                    reason="langchain-google-vertexai is not installed")
        return None
    except Exception as err:  # noqa: BLE001 - a bad region or model id
        log.warning("llm_client_failed", model=model, error=str(err))
        return None


_cached: dict[tuple, object] = {}


def _get(settings, key: str, **kwargs):
    cache_key = (id(settings), key)
    if cache_key not in _cached:
        _cached[cache_key] = _client(settings, **kwargs)
    return _cached[cache_key]


def get_client(settings):
    """The narrow-purpose model used by extraction and comment drafting."""
    return _get(settings, "helper", temperature=0.0)


def get_agent_llm(settings):
    """The model the agents reason and call tools with.

    Given a larger output budget than the helper: an agent working through
    fifteen users needs room for the tool calls, not just a sentence.
    """
    return _get(settings, "agent",
                max_tokens=max(2000, settings.llm_max_output_tokens))


def get_supervisor_llm(settings):
    """The routing model. Often a cheaper one - it emits one small JSON object.

    Temperature is pinned to zero regardless of ``agent_temperature``: routing
    should be reproducible, so that two identical situations do not take
    different paths for no reason anyone can explain afterwards.
    """
    return _get(settings, "supervisor", model=settings.routing_model,
                max_tokens=400, temperature=0.0)


def _invoke(settings, system: str, user: str) -> str:
    client = get_client(settings)
    if client is None:
        return ""
    try:
        response = client.invoke([("system", system), ("human", user)])
    except Exception as err:  # noqa: BLE001 - the LLM is optional by design
        log.warning("llm_call_failed", error=str(err))
        return ""
    content = getattr(response, "content", "")
    return content if isinstance(content, str) else str(content)


# ------------------------------------------------------------------ extraction

def _json_object(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return {}


def extract_users(settings, raw_field: str, work_item_id: str,
                  summary: str = "") -> list[RequestedUser]:
    """Last-resort extraction from a New Users field the parser could not read.

    Everything it returns is marked ``extracted_by_llm``, which forces the user
    to HIGH risk in validation and therefore onto the approval card with a flag.
    An approver always sees that a machine guessed this one.
    """
    if not raw_field.strip():
        return []

    prompt = (f"Work item: {work_item_id}\n"
              f"Field content (redacted):\n{redact_pii(raw_field, keep_userids=True)}")
    answer = _invoke(settings, EXTRACTION_SYSTEM, prompt)
    if not answer:
        return []

    payload = _json_object(answer)
    source = SourceWorkItem(work_item_id=work_item_id, summary=summary)
    users: list[RequestedUser] = []
    for item in payload.get("users", []) or []:
        userid = str(item.get("userid", "")).strip()
        if not re.match(USERID_PATTERN, userid):
            log.warning("llm_proposed_invalid_userid", work_item=work_item_id)
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        try:
            users.append(RequestedUser(
                userid=userid, source_work_items=[source],
                extracted_by_llm=True,
                extraction_confidence=max(0.0, min(1.0, confidence))))
        except Exception:  # pydantic ValidationError
            continue

    if users:
        log.info("llm_extraction_proposed", work_item=work_item_id, count=len(users))
    return users


# --------------------------------------------------------------------- closure

def draft_comment_line(settings, *, userid: str, display_name: str, action: str,
                       detail: str = "") -> str:
    """Draft one comment line, or return "" and let the template stand.

    The template is always correct; the model only makes it read better. Any
    draft that asserts something the recorded action does not support is
    discarded rather than corrected.
    """
    if not settings.llm_enabled:
        return ""
    fact = (f"User {userid} ({display_name}): the provisioning run recorded "
            f"'{action}'. {detail}".strip())
    draft = _invoke(settings, COMMENT_SYSTEM, redact_pii(fact, keep_userids=True)).strip()
    if not draft:
        return ""

    lowered = draft.lower()
    for forbidden in _FORBIDDEN_CLAIMS.get(action, ()):
        if forbidden in lowered:
            log.warning("llm_comment_rejected", userid=userid, action=action,
                        reason=f"asserted {forbidden!r} which the outcome does not support")
            return ""
    if len(draft) > 300 or "\n" in draft:
        return ""
    return draft

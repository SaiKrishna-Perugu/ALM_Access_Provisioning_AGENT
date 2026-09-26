"""The only place a language model is called, and the rules that constrain it.

Three constraints, enforced here rather than trusted to prompts:

1. **Never in the write path.** Every function returns *proposals* validated
   against the Pydantic contracts. User-ID recovery *selects* among tokens code
   found in the text and never generates one, so an invented ID cannot come out
   of it - see ``recover_userids``.
2. **PII is minimised before the call.** Deterministic parsing runs first and
   the model is a fallback; when it is reached, e-mail addresses and display
   names are redacted from the prompt. The user ID survives only because it is
   the thing being extracted.
3. **Output is structurally validated.** Free text is never used as-is: comment
   drafts must match a template check, and extraction output must parse into
   models. A model that returns something unexpected produces no work, not
   surprising work.

Where the model comes from is configuration (``ALM_LLM_PROVIDER``):

* ``gemini_api`` - the Gemini Developer API with a key from Google AI Studio.
  The quickest way in, and what the local sandbox uses.
* ``vertex`` - the same Gemini models on Vertex AI, authenticated as the Cloud
  Run service account. No key exists to leak; the production choice. Claude
  models from Model Garden are available on this provider only.

If no model is reachable, or ``llm_enabled`` is false, the helper functions
degrade to "no proposal" and the deterministic path stands alone. The agents
themselves need a model; the deterministic orchestration does not.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

from alm_core.logging import get_logger, redact_pii, scrub_secrets
from alm_core.models import USERID_PATTERN, RequestedUser, SourceWorkItem

log = get_logger("alm.llm")

EXTRACTION_SYSTEM = """You extract user IDs from a malformed access-request field.

The well-formed format is: LASTNAME,FIRSTNAME,email,USERID; repeated.
The text you are given failed to parse. Recover only what is genuinely present.

Rules:
- A user ID matches ^[A-Za-z]{1,3}[0-9][0-9A-Za-z]{3,8}$ (e.g. AB12345, CD67890).
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


def _gemini_generation(model: str) -> float:
    """2.5 for gemini-2.5-flash, 3.5 for gemini-3.5-flash; 0 when unknown."""
    found = re.search(r"gemini-(\d+(?:\.\d+)?)", model.lower())
    return float(found.group(1)) if found else 0.0


# One limiter per process, shared by every client: the quota belongs to the key
# (or the project), not to an individual agent. Nine agents each pacing
# themselves at the limit would together exceed it nine times over.
_limiter = None
_limiter_rpm = 0.0
_limiter_lock = threading.Lock()


def _rate_limiter(settings):
    global _limiter, _limiter_rpm
    from langchain_core.rate_limiters import InMemoryRateLimiter

    with _limiter_lock:
        if _limiter is None or _limiter_rpm != settings.llm_requests_per_minute:
            _limiter_rpm = settings.llm_requests_per_minute
            _limiter = InMemoryRateLimiter(
                requests_per_second=_limiter_rpm / 60.0,
                check_every_n_seconds=0.1, max_bucket_size=1)
        return _limiter


def _thinking_kwargs(model: str, level: str, supported: list[str] | None) -> dict:
    """Translate ALM_LLM_THINKING_LEVEL into what this model understands."""
    if level == "default":
        return {}
    generation = _gemini_generation(model)
    if generation >= 3:
        if supported and level not in supported:
            # e.g. Pro models have no "minimal"; take the lightest they offer.
            order = ["minimal", "low", "medium", "high"]
            lighter = [lvl for lvl in order if lvl in supported]
            if not lighter:
                return {}
            level = lighter[0]
        return {"thinking_level": level}
    if generation == 2.5 and "flash" in model.lower() and level == "minimal":
        return {"thinking_budget": 0}  # Flash can switch thinking off; Pro cannot
    return {}


def _client(settings, *, model: str = "", max_tokens: int = 0,
            temperature: float | None = None):
    """Build the chat model for the configured provider, or None if there is none.

    Gemini on either provider is ``ChatGoogleGenerativeAI`` - the Gemini API with
    a key, or Vertex AI (``vertexai=True``) with Application Default
    Credentials. Claude ids go to ``ChatAnthropicVertex``. Moving between any of
    them is a configuration change, not a code change.
    """
    if not settings.llm_enabled:
        return None

    model = model or settings.agent_model
    temperature = settings.agent_temperature if temperature is None else temperature
    max_tokens = max_tokens or settings.llm_max_output_tokens

    try:
        if _is_anthropic(model):
            if settings.llm_provider != "vertex" or not settings.project_id:
                log.warning("llm_unavailable", model=model,
                            reason="Claude models need ALM_LLM_PROVIDER=vertex and a project")
                return None
            from langchain_google_vertexai.model_garden import ChatAnthropicVertex

            return ChatAnthropicVertex(
                model_name=model, project=settings.project_id,
                location=settings.vertex_region, temperature=temperature,
                max_tokens=max_tokens, rate_limiter=_rate_limiter(settings))

        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs: dict = {
            "model": model,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "max_retries": 3,
            "timeout": 120,
            "rate_limiter": _rate_limiter(settings),
        }
        if settings.llm_provider == "vertex":
            if not settings.project_id:
                log.warning("llm_unavailable", reason="vertex provider without a project")
                return None
            kwargs.update(vertexai=True, project=settings.project_id,
                          location=settings.vertex_region)
        else:
            from alm_core.credentials import gemini_api_key
            from alm_core.errors import CredentialError

            try:
                kwargs["google_api_key"] = gemini_api_key(settings)
            except CredentialError as err:
                log.warning("llm_unavailable", reason=err.message)
                return None
            if settings.llm_provider == "vertex_express":
                # Vertex AI "express mode": an API key from the Google Cloud
                # console, no project or service account. Same models.
                kwargs["vertexai"] = True

        client = ChatGoogleGenerativeAI(**kwargs)
        profile = getattr(client, "profile", None) or {}
        thinking = _thinking_kwargs(model, settings.llm_thinking_level,
                                    profile.get("reasoning_effort_levels"))
        # Rebuilt rather than copied: the constructor validates the combination.
        return ChatGoogleGenerativeAI(**kwargs, **thinking) if thinking else client
    except ImportError:
        log.warning("llm_unavailable",
                    reason="langchain-google-genai is not installed "
                           "(pip install -r requirements-cloud.txt)")
        return None
    except Exception as err:  # noqa: BLE001 - a bad region or model id
        log.warning("llm_client_failed", model=model,
                    error=scrub_secrets(f"{type(err).__name__}: {err}"))
        return None


_cached: dict[tuple, object] = {}


def _get(settings, key: str, **kwargs):
    cache_key = (id(settings), key)
    if cache_key not in _cached:
        _cached[cache_key] = _client(settings, **kwargs)
    return _cached[cache_key]


def reset_clients() -> None:
    """Forget built clients - for tests, and after the key is rotated."""
    global _limiter
    _cached.clear()
    _typesafe_keys.clear()
    _limiter = None


def get_client(settings):
    """The narrow-purpose model used by extraction and comment drafting."""
    return _get(settings, "helper", temperature=0.0)


def get_agent_llm(settings):
    """The model the agents reason and call tools with.

    Given a larger output budget than the helper: an agent working through
    fifteen users needs room for the tool calls, not just a sentence.
    """
    # Thinking tokens come out of the same budget on Gemini, so the floor is
    # generous: a truncated tool call is a wasted request.
    return _get(settings, "agent",
                max_tokens=max(8192, settings.llm_max_output_tokens))


def get_supervisor_llm(settings):
    """The routing model. Often a cheaper one - it emits one small JSON object.

    Temperature is pinned to zero regardless of ``agent_temperature``: routing
    should be reproducible, so that two identical situations do not take
    different paths for no reason anyone can explain afterwards.
    """
    return _get(settings, "supervisor", model=settings.routing_model,
                max_tokens=2048, temperature=0.0)


def _invoke(settings, system: str, user: str) -> str:
    client = get_client(settings)
    if client is None:
        return ""
    try:
        response = client.invoke([("system", system), ("human", user)])
    except Exception as err:  # noqa: BLE001 - the LLM is optional by design
        log.warning("llm_call_failed", error=scrub_secrets(str(err)))
        return ""
    return response_text(response)


def response_text(response) -> str:
    """The answer text of a response, whatever shape the provider returned.

    Gemini may return a list of content parts (text, thinking, signatures)
    rather than a string; ``str()`` of that list is not an answer.
    """
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type", "text") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _json_object(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return {}


# ------------------------------------------------------------------ extraction
#
# Select, never generate. Code finds every token in the text shaped like a user
# ID; a model only judges which of those tokens are people being requested. The
# returned ID is the token copied from the source, so an ID that is not in the
# text cannot come out of extraction, whichever model is asked or however it
# misbehaves.

# Recall over precision: case-insensitive, any surrounding punctuation. The
# judge rejects part numbers and references; the finder must not miss a user.
_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,3}[0-9][0-9A-Za-z]{3,8}(?![A-Za-z0-9])")

# Nouls per TypeSafe request. Rows are short; this bounds a pathological paste.
_TYPESAFE_BATCH = 40

_TYPESAFE_CRITERIA_TRUE = (
    "The text asks for the person with this user ID to be added, given access, or "
    "provisioned, or lists them among the users being requested.")
_TYPESAFE_CRITERIA_FALSE = (
    "The token is not a person being requested: a part number, ticket, version, "
    "project code or other reference, or a user ID mentioned for another reason - "
    "the requester, a manager, an approver, or someone to be removed.")


def userid_candidates(text: str) -> list[str]:
    """Every distinct token in ``text`` that could be a user ID, in order."""
    found: dict[str, None] = {}
    for match in _CANDIDATE_RE.finditer(text or ""):
        userid = match.group(0).upper()
        if re.match(USERID_PATTERN, userid):
            found.setdefault(userid, None)
    return list(found)


@dataclass
class Recovery:
    """What user-ID recovery considered and concluded for one field."""

    method: str                                   # typesafe | gemini | none
    candidates: list[str] = field(default_factory=list)
    accepted: dict[str, float] = field(default_factory=dict)
    rejected: dict[str, float] = field(default_factory=dict)
    error: str = ""


_typesafe_keys: dict[int, str | None] = {}


def _typesafe_key(settings) -> str | None:
    """Resolved once per settings object; None means "not configured"."""
    if id(settings) not in _typesafe_keys:
        from alm_core.credentials import typesafe_api_key

        _typesafe_keys[id(settings)] = typesafe_api_key(settings)
    return _typesafe_keys[id(settings)]


def _typesafe_judge(settings, state: dict, candidates: list[str], client=None
                    ) -> dict[str, float]:
    """One Noul per candidate: is this token a person being requested?

    Nouls, not one Choice: a row can request several users, and each candidate
    deserves its own probability rather than a share of a single distribution.
    """
    from typesafe_sdk import Noul, NoulCriteria

    owned = client is None
    if owned:
        from typesafe_sdk import TypeSafeClient

        client = TypeSafeClient(api_key=_typesafe_key(settings),
                                model=settings.typesafe_model, timeout=30.0)
    criteria = NoulCriteria(true=_TYPESAFE_CRITERIA_TRUE, false=_TYPESAFE_CRITERIA_FALSE)
    probabilities: dict[str, float] = {}
    try:
        for start in range(0, len(candidates), _TYPESAFE_BATCH):
            batch = candidates[start:start + _TYPESAFE_BATCH]
            questions = {
                f"c{start + i}": Noul(
                    instructions=(f"Does `rows` ask for the person with user ID {uid} "
                                  f"(`candidates.c{start + i}`) to be given ALM access?"),
                    criteria=criteria)
                for i, uid in enumerate(batch)}
            response = client.system_one(state=state, questions=questions)
            for i, uid in enumerate(batch):
                answer = response.nouls.get(f"c{start + i}")
                if answer is not None:
                    probabilities[uid] = float(answer.noul)
    finally:
        if owned:
            client.close()
    return probabilities


def _gemini_judge(settings, prompt: str, candidates: list[str]) -> dict[str, float]:
    """The generative fallback, held to the same rule: only candidates count."""
    answer = _invoke(settings, EXTRACTION_SYSTEM, prompt)
    if not answer:
        return {}
    allowed = set(candidates)
    judged: dict[str, float] = {}
    for item in _json_object(answer).get("users", []) or []:
        userid = str(item.get("userid", "")).strip().upper()
        if userid not in allowed:
            # Well-formed or not, an ID that is not in the text was invented.
            log.warning("llm_proposed_userid_not_in_text", userid_shape_ok=bool(
                re.match(USERID_PATTERN, userid)))
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        judged[userid] = max(0.0, min(1.0, confidence))
    # A candidate the model did not mention is one it judged not requested.
    return {uid: judged.get(uid, 0.0) for uid in candidates}


def recover_userids(settings, raw_field: str, work_item_id: str, summary: str = "",
                    *, typesafe_client=None) -> Recovery:
    """Judge which user-ID-shaped tokens in ``raw_field`` are requested users."""
    text = redact_pii(raw_field or "", keep_userids=True)
    # Candidates come from the text the judge will see, so the two cannot differ.
    candidates = userid_candidates(text)
    if not candidates:
        return Recovery(method="none")

    provider = settings.extraction_provider
    use_typesafe = provider == "typesafe" or (
        provider == "auto" and _typesafe_key(settings) is not None)

    recovery = Recovery(method="none", candidates=candidates)
    probabilities: dict[str, float] = {}
    if use_typesafe:
        state = {"work_item": {"id": work_item_id, "summary": summary},
                 "rows": text,
                 "candidates": {f"c{i}": uid for i, uid in enumerate(candidates)}}
        try:
            probabilities = _typesafe_judge(settings, state, candidates, typesafe_client)
            recovery.method = "typesafe"
        except ImportError:
            recovery.error = "typesafe-sdk is not installed"
        except Exception as err:  # noqa: BLE001 - an outage degrades, never crashes
            recovery.error = scrub_secrets(f"{type(err).__name__}: {err}")[:300]
        if recovery.error:
            log.warning("typesafe_extraction_failed", work_item=work_item_id,
                        error=recovery.error)
            if provider == "typesafe":
                return recovery  # explicitly TypeSafe-only: leave it for a human

    if recovery.method == "none" and settings.llm_enabled:
        prompt = f"Work item: {work_item_id}\nField content (redacted):\n{text}"
        probabilities = _gemini_judge(settings, prompt, candidates)
        recovery.method = "gemini" if probabilities else "none"

    threshold = settings.extraction_min_probability
    for uid in candidates:
        probability = probabilities.get(uid)
        if probability is None:
            continue
        (recovery.accepted if probability >= threshold else recovery.rejected)[uid] = \
            round(probability, 4)
    log.info("userid_recovery", work_item=work_item_id, method=recovery.method,
             candidates=len(candidates), accepted=len(recovery.accepted))
    return recovery


def extract_users(settings, raw_field: str, work_item_id: str, summary: str = "",
                  *, typesafe_client=None) -> list[RequestedUser]:
    """Last-resort extraction from a New Users field the parser could not read.

    Everything it returns is marked ``extracted_by_llm``, which forces the user
    to HIGH risk in validation and therefore onto the approval card with a flag
    and the judge's probability. An approver always sees that a machine chose
    this one.
    """
    if not (raw_field or "").strip():
        return []
    recovery = recover_userids(settings, raw_field, work_item_id, summary,
                               typesafe_client=typesafe_client)
    source = SourceWorkItem(work_item_id=work_item_id, summary=summary)
    users: list[RequestedUser] = []
    for userid, probability in recovery.accepted.items():
        try:
            users.append(RequestedUser(
                userid=userid, source_work_items=[source], extracted_by_llm=True,
                extraction_confidence=probability))
        except Exception:  # noqa: S112 - skip invalid user candidate validation failures
            continue
    if users:
        log.info("llm_extraction_proposed", work_item=work_item_id, count=len(users),
                 method=recovery.method)
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

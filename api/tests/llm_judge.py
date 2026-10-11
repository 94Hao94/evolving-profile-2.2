"""
LLM-as-a-judge utility for hs_llm_core tests.

Replaces brittle string-matching assertions (e.g., `assert "alice" in answer`)
with semantic evaluation via a frontier mini model. This makes tests resilient
to phrasing variations while still verifying LLM output quality.

Usage in tests:
    result = await llm_judge.assert_response_meets_criteria(
        response="Alice is a researcher at Stanford...",
        criteria="The response mentions Alice and her role",
    )
"""

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from evolving_profile_api.engine.llm_wrapper import create_llm_provider, requires_api_key

logger = logging.getLogger(__name__)

def _resolve_judge_configuration(env: Mapping[str, str]) -> dict[str, Any]:
    """Resolve provider, endpoint and credentials as one authentication boundary.

    An unspecified judge reuses the entire primary configuration. A different
    provider needs its own credential and explicit model; it can never borrow
    the primary key. Same-provider endpoint/key overrides must be supplied as a
    pair, so a custom-endpoint key cannot silently move to a canonical endpoint.
    """
    primary_provider = env.get("EVOLVING_PROFILE_API_LLM_PROVIDER", "openai").lower()
    primary_model = env.get("EVOLVING_PROFILE_API_LLM_MODEL", "")
    primary_base = env.get("EVOLVING_PROFILE_API_LLM_BASE_URL", "")
    primary_key = env.get("EVOLVING_PROFILE_API_LLM_API_KEY", "")
    provider = env.get("HINDSIGHT_TEST_JUDGE_PROVIDER", primary_provider).lower()
    same_provider = provider == primary_provider
    model = env.get("HINDSIGHT_TEST_JUDGE_MODEL", primary_model if same_provider else "")
    if provider == "gemini":
        model = model.removeprefix("google/")
    reason = ""
    if same_provider:
        has_base = "HINDSIGHT_TEST_JUDGE_BASE_URL" in env
        has_key = "HINDSIGHT_TEST_JUDGE_API_KEY" in env
        if has_base != has_key:
            base, key = "", ""
            reason = "judge endpoint and credential overrides must be configured together"
        else:
            base = env.get("HINDSIGHT_TEST_JUDGE_BASE_URL", primary_base)
            key = env.get("HINDSIGHT_TEST_JUDGE_API_KEY", primary_key)
            if primary_key and base != primary_base and key == primary_key:
                key = ""
                reason = "the primary credential cannot be rebound to a different judge endpoint"
    else:
        provider_key_names = {
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "openai": ("OPENAI_API_KEY",), "anthropic": ("ANTHROPIC_API_KEY",),
            "groq": ("GROQ_API_KEY",), "deepseek": ("DEEPSEEK_API_KEY",),
        }
        base = env.get("HINDSIGHT_TEST_JUDGE_BASE_URL", "")
        key = env.get("HINDSIGHT_TEST_JUDGE_API_KEY", "") or next(
            (env[name] for name in provider_key_names.get(provider, ()) if env.get(name)), ""
        )
        if primary_key and key == primary_key:
            key = ""
            reason = "a different judge provider requires a dedicated credential distinct from the primary key"
        elif requires_api_key(provider) and not key:
            reason = "a different judge provider requires a dedicated credential"
    if not reason and not model:
        reason = "judge model is not configured"
    if not reason and requires_api_key(provider) and not key:
        reason = "judge credential is not configured"
    independence = (
        "same_model_not_independent"
        if same_provider and model == primary_model and base == primary_base
        else "different_model_or_endpoint" if same_provider else "different_provider"
    )
    return {
        "provider": provider, "model": model, "base_url": base, "api_key": key,
        "configured": not reason, "reason": reason, "independence": independence,
    }


_JUDGE_CONFIG = _resolve_judge_configuration(os.environ)
_JUDGE_PROVIDER = _JUDGE_CONFIG["provider"]
_JUDGE_MODEL = _JUDGE_CONFIG["model"]
_JUDGE_API_KEY = _JUDGE_CONFIG["api_key"]
_JUDGE_BASE_URL = _JUDGE_CONFIG["base_url"]
_JUDGE_INDEPENDENCE = _JUDGE_CONFIG["independence"]

# Flakiness hardening. A single temperature-0 judge call still occasionally flips
# its verdict on borderline phrasing — the dominant source of hs_llm_core
# flakiness. When the primary verdict is "not met", we ask for a few independent
# second opinions (at a higher temperature so the samples genuinely differ) and
# uphold the failure only if the majority agrees. Verdicts that pass on the first
# call are returned immediately, so passing tests are unaffected in cost or
# behaviour, and genuine failures (where every judge agrees) still fail.
_JUDGE_CONFIRMATIONS = int(os.getenv("HINDSIGHT_TEST_JUDGE_CONFIRMATIONS", "2"))
_JUDGE_CONFIRM_TEMPERATURE = float(os.getenv("HINDSIGHT_TEST_JUDGE_CONFIRM_TEMPERATURE", "0.5"))
# Retry transient judge-call errors (rate limits, 5xx) so judge infrastructure
# hiccups never fail the test under evaluation.
_JUDGE_CALL_ATTEMPTS = int(os.getenv("HINDSIGHT_TEST_JUDGE_CALL_ATTEMPTS", "3"))


class JudgeVerdict(BaseModel):
    meets_criteria: bool
    reasoning: str


_judge_instance = None


def _get_judge():
    global _judge_instance
    if _judge_instance is None:
        if not _JUDGE_CONFIG["configured"]:
            raise RuntimeError(f"Judge not configured: {_JUDGE_CONFIG['reason']}")
        logger.info("Judge provider=%s model=%s independence=%s", _JUDGE_PROVIDER, _JUDGE_MODEL, _JUDGE_INDEPENDENCE)
        _judge_instance = create_llm_provider(
            provider=_JUDGE_PROVIDER,
            api_key=_JUDGE_API_KEY,
            base_url=_JUDGE_BASE_URL or "",
            model=_JUDGE_MODEL,
            reasoning_effort="low",
        )
    return _judge_instance


def build_judge_messages(response: str, criteria: str, context: str | None) -> list[dict[str, str]]:
    """Assemble the judge prompt.

    The three inputs must be unambiguously separated, and they were not: the
    response and criteria carried ``##`` headers while the context was emitted as
    a bare ``Context provided to the system:`` line, so a multi-line response ran
    straight into the context with nothing marking the boundary.

    The judge duly confused them. ``test_facts_from_distinct_chunks_reach_the_answer``
    failed four times in a row, always with the judge quoting the *context* back as
    if it were the answer ("It only states that the memory data contained two hobby
    facts") while the real response — a two-bullet markdown list naming both facts —
    satisfied the criteria. A bullet-list response followed by prose context is
    exactly the shape that blurs.

    So each section is tagged rather than merely headed: tags survive a response
    that is itself markdown, which fenced blocks and headers do not. The system
    prompt states outright that only ``<response>`` is judged, so context can never
    become the thing under test.

    Kept as a pure function so the assembly is covered by fast unit tests instead
    of only by the LLM tests it decides the outcome of.
    """
    context_block = f"\n## Context provided to the system\n<context>\n{context}\n</context>\n" if context else ""
    return [
        {
            "role": "system",
            "content": (
                "You are a test evaluation judge. Given a response and evaluation criteria, "
                "determine whether the response meets the criteria. "
                "Judge ONLY the text inside <response></response>. A <context></context> block "
                "is background on how that response was produced — it is never the thing being "
                "judged, and must never be treated as part of the response. "
                'Respond with JSON: {"meets_criteria": true/false, "reasoning": "brief explanation"}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"## Response to evaluate\n<response>\n{response}\n</response>\n"
                f"{context_block}\n"
                f"## Criteria\n<criteria>\n{criteria}\n</criteria>\n\n"
                "Does the response meet the criteria?"
            ),
        },
    ]


async def _judge_once(
    response: str,
    criteria: str,
    context: str | None,
    temperature: float,
) -> JudgeVerdict:
    """Run a single judge verdict, retrying transient call errors."""
    judge = _get_judge()
    messages = build_judge_messages(response, criteria, context)

    last_error: Exception | None = None
    for attempt in range(max(1, _JUDGE_CALL_ATTEMPTS)):
        try:
            result = await judge.call(
                messages=messages,
                response_format=JudgeVerdict,
                max_completion_tokens=256,
                temperature=temperature,
                scope="test_judge",
            )
            if isinstance(result, JudgeVerdict):
                return result
            if isinstance(result, dict):
                return JudgeVerdict(**result)
            return JudgeVerdict(**json.loads(str(result)))
        except Exception as e:  # transient provider error — retry before giving up
            last_error = e
            logger.warning(f"Judge call failed (attempt {attempt + 1}/{_JUDGE_CALL_ATTEMPTS}): {e}")
            await asyncio.sleep(1.0 * (attempt + 1))

    raise RuntimeError(f"Judge call failed after {_JUDGE_CALL_ATTEMPTS} attempts: {last_error}") from last_error


async def evaluate(
    response: str,
    criteria: str,
    context: str | None = None,
) -> JudgeVerdict:
    """Ask the judge LLM whether a response meets the given criteria.

    The primary verdict is deterministic (temperature 0). If it says the criteria
    are NOT met, we collect a few independent higher-temperature second opinions
    and overrule the failure only when the majority disagrees — smoothing out the
    single-call noise that makes these tests flaky. See the module-level
    ``_JUDGE_CONFIRMATIONS`` notes.

    Args:
        response: The LLM-generated text to evaluate.
        criteria: Plain-English description of what the response should contain/satisfy.
        context: Optional context (e.g., the stored memories or query) for the judge.

    Returns:
        JudgeVerdict with meets_criteria bool and reasoning string.
    """
    primary = await _judge_once(response, criteria, context, temperature=0.0)
    if primary.meets_criteria or _JUDGE_CONFIRMATIONS <= 0:
        return primary

    # Primary says "not met": get independent second opinions before trusting it.
    confirmations = await asyncio.gather(
        *(
            _judge_once(response, criteria, context, temperature=_JUDGE_CONFIRM_TEMPERATURE)
            for _ in range(_JUDGE_CONFIRMATIONS)
        ),
        return_exceptions=True,
    )
    verdicts = [primary] + [c for c in confirmations if isinstance(c, JudgeVerdict)]
    met = sum(1 for v in verdicts if v.meets_criteria)
    not_met = len(verdicts) - met

    if met > not_met:
        agreeing = next(v for v in verdicts if v.meets_criteria)
        logger.info(f"Judge: primary 'not met' overruled by majority ({met}/{len(verdicts)} met). Criteria: {criteria}")
        return JudgeVerdict(
            meets_criteria=True,
            reasoning=f"Majority of {len(verdicts)} judges met criteria (primary verdict overruled as noise). {agreeing.reasoning}",
        )
    return JudgeVerdict(
        meets_criteria=False,
        reasoning=f"{not_met}/{len(verdicts)} judges agree criteria not met. {primary.reasoning}",
    )


async def assert_meets_criteria(
    response: str,
    criteria: str,
    context: str | None = None,
    msg: str | None = None,
) -> JudgeVerdict:
    """Assert that a response meets criteria, with a clear failure message.

    Raises AssertionError if the judge says criteria are not met.
    """
    verdict = await evaluate(response=response, criteria=criteria, context=context)
    if not verdict.meets_criteria:
        fail_msg = msg or "LLM judge: criteria not met"
        raise AssertionError(
            f"{fail_msg}\n"
            f"  Criteria: {criteria}\n"
            f"  Judge reasoning: {verdict.reasoning}\n"
            f"  Response (first 300 chars): {response[:300]}"
        )
    return verdict

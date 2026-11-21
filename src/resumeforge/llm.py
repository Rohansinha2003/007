"""The only module that talks to an LLM. Swap the provider by replacing `_call_anthropic`.

Callers pass a pydantic model; the wrapper requests JSON matching its schema and validates the reply.
All text is redacted before it leaves the machine.
"""
from __future__ import annotations

import json
import os
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .redact import redact

T = TypeVar("T", bound=BaseModel)

MODEL = os.environ.get("RESUMEFORGE_MODEL", "claude-opus-5-5")


class LLMError(RuntimeError):
    pass


def _strict_schema(schema: dict) -> dict:
    """Structured outputs need additionalProperties:false and every property required."""
    defs = schema.get("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            if "$ref" in node and len(node) > 1:  # pydantic may attach description next to $ref
                node = {"$ref": node["$ref"]}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for k in ("title", "default"):
                node.pop(k, None)
            return {k: fix(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    out = fix(schema)
    if defs:
        out["$defs"] = {k: fix(v) for k, v in defs.items()}
    return out


def _call_anthropic(system: str, user: str, schema: dict, max_tokens: int) -> str:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMError("ANTHROPIC_API_KEY is not set. Export it, or use --offline.")
    client = anthropic.Anthropic()
    try:
        # Server-side fallback re-runs the request on another model if this one declines.
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError as e:
        raise LLMError(f"rate limited: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"connection error: {e}") from e
    if resp.stop_reason == "refusal":
        raise LLMError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("response truncated at max_tokens")
    return next(b.text for b in resp.content if b.type == "text")


# Tests replace this with a fake. Signature: (system, user, schema, max_tokens) -> json text.
backend: Callable[[str, str, dict, int], str] = _call_anthropic


def complete_json(system: str, user: str, model: type[T], max_tokens: int = 16000) -> T:
    schema = _strict_schema(model.model_json_schema())
    raw = backend(redact(system), redact(user), schema, max_tokens)
    try:
        return model.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        raise LLMError(f"LLM returned invalid JSON for {model.__name__}: {e}") from e

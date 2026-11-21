"""The only module that talks to an LLM.

- Structured output: callers pass a pydantic model. Its JSON schema is sent as the required
  output format, and the reply is validated against the same model.
- Retries: the SDK retries rate limits, 5xx and connection errors with backoff. This wrapper
  retries invalid or truncated output, telling the model what was wrong.
- Caching: validated results are stored in SQLite by a hash of model + prompt + schema.
- Logging: every call (including cache hits and failures) is written to llm_calls with tokens and cost.
- Redaction: system and user text are redacted before anything leaves the machine.

To swap providers, replace `anthropic_backend`. It must return (text, usage dict).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .redact import redact

T = TypeVar("T", bound=BaseModel)

MODEL = os.environ.get("RESUMEFORGE_MODEL", "claude-opus-5-5")

# USD per million tokens (input, output, cache read). Snapshot of published prices, 2026-11.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
}


class LLMError(RuntimeError):
    pass


class OutputInvalid(LLMError):
    """The model replied, but not with valid JSON for the schema."""


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def cost_usd(self) -> float | None:
        p = PRICES.get(self.model)
        if not p:
            return None
        return round((self.input_tokens * p[0] + self.output_tokens * p[1] + self.cache_read_tokens * p[2]) / 1e6, 6)


Backend = Callable[[str, str, dict, int, str], tuple[str, Usage]]


def strict_schema(schema: dict) -> dict:
    """Structured outputs need additionalProperties:false and every property required."""
    defs = schema.get("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            if "$ref" in node and len(node) > 1:
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

    out = fix({k: v for k, v in schema.items() if k != "$defs"})
    if defs:
        out["$defs"] = {k: fix(v) for k, v in defs.items()}
    return out


def anthropic_backend(system: str, user: str, schema: dict, max_tokens: int, model: str) -> tuple[str, Usage]:
    import anthropic

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise LLMError("ANTHROPIC_API_KEY is not set.")
    client = anthropic.Anthropic(max_retries=4)  # SDK backs off on 408/409/429/5xx and connection errors
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # another model retries the request if this one declines
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIStatusError as e:
        raise LLMError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"connection error: {e}") from e
    u = resp.usage
    usage = Usage(model=resp.model or model, input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                  cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
    if resp.stop_reason == "refusal":
        raise LLMError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise OutputInvalid("response truncated at max_tokens")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return text, usage


class LLM:
    def __init__(self, conn: sqlite3.Connection | None = None, backend: Backend | None = None,
                 model: str = MODEL, max_attempts: int = 3):
        self.conn, self.backend, self.model, self.max_attempts = conn, backend or anthropic_backend, model, max_attempts

    @staticmethod
    def cache_key(model: str, system: str, user: str, schema: dict) -> str:
        blob = json.dumps({"model": model, "system": system, "user": user, "schema": schema}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _log(self, purpose, usage: Usage | None, *, cached=False, ok=True, attempts=1, error=None):
        if self.conn is None:
            return
        self.conn.execute(
            """INSERT INTO llm_calls (ts, purpose, model, cached, ok, attempts, input_tokens, output_tokens,
                                      cache_read_tokens, cost_usd, error)
               VALUES (datetime('now'),?,?,?,?,?,?,?,?,?,?)""",
            (purpose, usage.model if usage else self.model, int(cached), int(ok), attempts,
             usage.input_tokens if usage else 0, usage.output_tokens if usage else 0,
             usage.cache_read_tokens if usage else 0, (usage.cost_usd if usage else 0.0) if not cached else 0.0,
             error))
        self.conn.commit()

    def complete_json(self, system: str, user: str, out: type[T], *, purpose: str = "",
                      max_tokens: int = 16000, use_cache: bool = True) -> T:
        system, user = redact(system), redact(user)
        schema = strict_schema(out.model_json_schema())
        key = self.cache_key(self.model, system, user, schema)
        if use_cache and self.conn is not None:
            row = self.conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    result = out.model_validate_json(row[0])
                    self._log(purpose, Usage(self.model), cached=True)
                    return result
                except ValidationError:
                    pass  # schema changed since caching; fall through and refresh
        total = Usage(self.model)
        prompt, last_error = user, None
        for attempt in range(1, self.max_attempts + 1):
            try:
                text, usage = self.backend(system, prompt, schema, max_tokens, self.model)
                total.model = usage.model
                total.input_tokens += usage.input_tokens
                total.output_tokens += usage.output_tokens
                total.cache_read_tokens += usage.cache_read_tokens
                result = out.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError, OutputInvalid) as e:
                last_error = f"{type(e).__name__}: {str(e)[:500]}"
                if isinstance(e, OutputInvalid):
                    max_tokens = min(max_tokens * 2, 64000)
                prompt = (f"{user}\n\nYour previous reply was rejected ({last_error}). "
                          "Reply with JSON that matches the schema exactly.")
                continue
            except LLMError as e:
                self._log(purpose, total, ok=False, attempts=attempt, error=str(e)[:500])
                raise
            if self.conn is not None:
                self.conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,datetime('now'))",
                                  (key, total.model, result.model_dump_json()))
            self._log(purpose, total, attempts=attempt)
            return result
        self._log(purpose, total, ok=False, attempts=self.max_attempts, error=last_error)
        raise LLMError(f"invalid output after {self.max_attempts} attempts: {last_error}")

import json

import pytest
from pydantic import BaseModel

from resumeforge import db, llm
from resumeforge.llm import LLM, LLMError, OutputInvalid, Usage


class Item(BaseModel):
    name: str
    ids: list[int]


class Out(BaseModel):
    items: list[Item]


GOOD = json.dumps({"items": [{"name": "a", "ids": [1]}]})


class FakeBackend:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, system, user, schema, max_tokens, model):
        self.calls.append({"system": system, "user": user, "schema": schema, "max_tokens": max_tokens})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, Usage(model, input_tokens=1000, output_tokens=200)


def test_strict_schema():
    s = llm.strict_schema(Out.model_json_schema())
    assert s["additionalProperties"] is False and s["required"] == ["items"]
    assert s["$defs"]["Item"]["additionalProperties"] is False and "title" not in s["$defs"]["Item"]


def test_validates_redacts_and_logs_cost(conn):
    fb = FakeBackend(GOOD)
    out = LLM(conn, backend=fb).complete_json("s", "key ghp_" + "x" * 36, Out, purpose="t")
    assert out.items[0].ids == [1] and "ghp_" not in fb.calls[0]["user"]
    row = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert (row["purpose"], row["ok"], row["cached"], row["input_tokens"], row["output_tokens"]) == ("t", 1, 0, 1000, 200)
    assert row["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)  # claude-opus-5-5 prices


def test_cache_by_input_hash(conn):
    fb = FakeBackend(GOOD, GOOD)
    c = LLM(conn, backend=fb)
    c.complete_json("s", "same", Out)
    c.complete_json("s", "same", Out)
    assert len(fb.calls) == 1
    c.complete_json("s", "different", Out)
    assert len(fb.calls) == 2
    assert [r[0] for r in conn.execute("SELECT cached FROM llm_calls ORDER BY id")] == [0, 1, 0]
    fb.replies.append(GOOD)
    c.complete_json("s", "same", Out, use_cache=False)
    assert len(fb.calls) == 3


def test_retries_invalid_output_with_feedback(conn):
    fb = FakeBackend("not json", '{"items": [{"name": 1}]}', GOOD)
    out = LLM(conn, backend=fb).complete_json("s", "u", Out)
    assert out.items[0].name == "a" and len(fb.calls) == 3
    assert "previous reply was rejected" in fb.calls[1]["user"]
    row = conn.execute("SELECT attempts, input_tokens FROM llm_calls").fetchone()
    assert tuple(row) == (3, 3000)  # tokens of every attempt are counted


def test_truncation_retries_with_more_tokens(conn):
    fb = FakeBackend(OutputInvalid("truncated"), GOOD)
    LLM(conn, backend=fb).complete_json("s", "u", Out, max_tokens=1000)
    assert [c["max_tokens"] for c in fb.calls] == [1000, 2000]


def test_gives_up_and_logs_failure(conn):
    fb = FakeBackend("x", "y", "z")
    with pytest.raises(LLMError, match="after 3 attempts"):
        LLM(conn, backend=fb).complete_json("s", "u", Out)
    assert conn.execute("SELECT ok FROM llm_calls").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_api_errors_are_not_retried_here(conn):
    fb = FakeBackend(LLMError("API error 400: bad"))
    with pytest.raises(LLMError, match="400"):
        LLM(conn, backend=fb).complete_json("s", "u", Out)
    assert len(fb.calls) == 1


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        llm.anthropic_backend("s", "u", {}, 10, "claude-opus-5-5")


def test_unknown_model_cost_is_none():
    assert Usage("some-other-model", 10, 10).cost_usd is None

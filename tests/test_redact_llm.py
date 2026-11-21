import json

import pytest
from pydantic import BaseModel

from resumeforge import llm
from resumeforge.redact import redact


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuv", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP", "postgres://user:hunter2pass@db.example.com:5432/app",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "someone@example.com",
])
def test_redacts_secrets(secret):
    out = redact(f"before {secret} after")
    assert secret not in out and "REDACTED" in out


def test_plain_text_untouched():
    assert redact("Built a REST API in FastAPI") == "Built a REST API in FastAPI"


class Item(BaseModel):
    name: str
    ids: list[int]


class Out(BaseModel):
    items: list[Item]


def test_llm_validates_and_redacts(monkeypatch):
    seen = {}

    def backend(system, user, schema, max_tokens):
        seen.update(user=user, schema=schema)
        return json.dumps({"items": [{"name": "a", "ids": [1]}]})

    monkeypatch.setattr(llm, "backend", backend)
    out = llm.complete_json("s", "key ghp_" + "x" * 30, Out)
    assert out.items[0].ids == [1] and "ghp_" not in seen["user"]
    assert seen["schema"]["additionalProperties"] is False
    assert seen["schema"]["$defs"]["Item"]["required"] == ["name", "ids"]


def test_llm_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(llm, "backend", lambda *a: '{"items": 3}')
    with pytest.raises(llm.LLMError):
        llm.complete_json("s", "u", Out)


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm._call_anthropic("s", "u", {}, 10)

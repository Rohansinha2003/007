"""Secret and PII redaction. Applied at ingest time and again before every LLM call."""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("OPENAI_KEY", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("STRIPE_KEY", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("DB_URL", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"]+:[^\s'\"@]+@[^\s'\"]+")),
    # key=value / key: value assignments for secret-looking names
    ("SECRET", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|AUTH)[A-Z0-9_]*)"
        r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{8,})\3"
    )),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
]


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    for label, pat in _PATTERNS:
        if label == "SECRET":
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_{label}]", text)
        else:
            text = pat.sub(f"[REDACTED_{label}]", text)
    return text

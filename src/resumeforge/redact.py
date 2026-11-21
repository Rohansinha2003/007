"""Secret and PII redaction. Runs on every text field the database stores (see db.py)
and again on every LLM request (see llm.py).

Patterns are ordered: whole blocks first, then URLs carrying credentials, then vendor
token formats, then key=value assignments, then private hosts, then emails.
"""
from __future__ import annotations

import re

_SECRET_NAME = (r"(?:[A-Za-z0-9_\-]*?(?:API[_\-]?KEY|APIKEY|SECRET(?:[_\-]?KEY)?|ACCESS[_\-]?KEY(?:[_\-]?ID)?|"
                r"AUTH[_\-]?TOKEN|ACCESS[_\-]?TOKEN|REFRESH[_\-]?TOKEN|BEARER[_\-]?TOKEN|TOKEN|PRIVATE[_\-]?KEY|"
                r"CLIENT[_\-]?SECRET|SIGNING[_\-]?KEY|WEBHOOK[_\-]?SECRET|CREDENTIALS?|DSN))")
_PASSWORD_NAME = r"(?:[A-Za-z0-9_\-]*?(?:PASSWORD|PASSWD|PASSPHRASE|PWD))"
# Values that are references to a secret, not the secret itself.
_NOT_A_VALUE = r"(?!os\.environ|process\.env|getenv|env\(|\$\{|\$[A-Z_]|<|\[REDACTED|None\b|null\b|undefined\b|true\b|false\b)"
_PRIVATE_HOST = (r"(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
                 r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|\[?::1\]?|[A-Za-z0-9\-.]+\.(?:local|internal|corp|lan|intranet|home\.arpa))")
_SECRET_PARAMS = (r"(?:access_token|refresh_token|id_token|token|api_key|apikey|key|secret|client_secret|password|pwd|"
                  r"sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|"
                  r"x-goog-credential|code|auth|session|sessionid|sv|se)")

_PATTERNS: list[tuple[str, re.Pattern[str], str | None]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), None),
    # scheme://user:password@host...  -> whole URL
    ("URL_CREDENTIALS", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@'\"<>]+:[^\s/@'\"<>]+@[^\s'\"<>)]+"), None),
    # secret-looking query parameters: keep URL, drop the value
    ("URL_PARAM", re.compile(rf"(?i)([?&]{_SECRET_PARAMS}=)[^&#\s'\"<>]+"), r"\1[REDACTED]"),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), None),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), None),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), None),
    ("GITLAB_TOKEN", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), None),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b"), None),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), None),
    ("GOOGLE_OAUTH", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"), None),
    ("HUGGINGFACE_TOKEN", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), None),
    ("NPM_TOKEN", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), None),
    ("SLACK_TOKEN", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), None),
    ("SLACK_WEBHOOK", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), None),
    ("DISCORD_WEBHOOK", re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_\-]+"), None),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), None),
    ("SENDGRID_KEY", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"), None),
    ("TWILIO_KEY", re.compile(r"\bSK[0-9a-fA-F]{32}\b"), None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), None),
    ("AUTH_HEADER", re.compile(r"(?i)\b(authorization\s*[:=]\s*['\"]?(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=\-]{8,}"),
     r"\1[REDACTED_AUTH_HEADER]"),
    ("BEARER", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{20,}"), r"\1[REDACTED_BEARER]"),
    # NAME=value, NAME: value, "name": "value", --password value
    ("SECRET", re.compile(rf"(?i)\b({_SECRET_NAME})\b(['\"]?\s*[:=]\s*)(['\"]?){_NOT_A_VALUE}([^\s'\",;]{{8,}})\3"),
     r"\1\2\3[REDACTED_SECRET]\3"),
    # password: x / PASSWORD=x / "password": "x"
    ("PASSWORD", re.compile(rf"(?i)\b({_PASSWORD_NAME})\b(['\"]?\s*[:=]\s*)(['\"]?){_NOT_A_VALUE}([^\s'\",;]{{4,}})\3"),
     r"\1\2\3[REDACTED_PASSWORD]\3"),
    # CLI flags only: --password x, --db-password x  (plain prose like "password reset" is left alone)
    ("PASSWORD_FLAG", re.compile(rf"(?i)(--{_PASSWORD_NAME}\s+)(['\"]?){_NOT_A_VALUE}([^\s'\",;]{{4,}})\2"),
     r"\1\2[REDACTED_PASSWORD]\2"),
    ("PRIVATE_URL", re.compile(rf"(?i)\b(?:https?|wss?|ftp)://{_PRIVATE_HOST}(?::\d+)?[^\s'\"<>)\]]*"), None),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b"), None),
]


def redact(text: str | None) -> str:
    """Return text with secrets, private URLs and emails replaced by [REDACTED_<TYPE>] markers."""
    if not text:
        return text or ""
    for label, pat, repl in _PATTERNS:
        text = pat.sub(repl if repl is not None else f"[REDACTED_{label}]", text)
    return text


def redact_obj(value):
    """Redact every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value

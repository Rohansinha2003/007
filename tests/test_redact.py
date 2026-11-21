"""All secrets below are fake and exist only to exercise the patterns."""
import pytest

from resumeforge.redact import redact, redact_obj

FAKE = {
    "anthropic": "sk-ant-api03-FAKEfakeFAKEfake1234567890",
    "openai": "sk-proj-FAKEfakeFAKEfakeFAKE123456",
    "github_classic": "ghp_FAKEfakeFAKEfakeFAKEfake1234567890",
    "github_oauth": "gho_FAKEfakeFAKEfakeFAKEfake1234567890",
    "github_pat": "github_pat_11FAKE0fake_FAKEfakeFAKEfake123",
    "gitlab": "glpat-FAKEfakeFAKEfake12345",
    "aws": "AKIAFAKEFAKEFAKEFAKE",
    "google": "AIzaSyFAKEfakeFAKEfakeFAKEfakeFAKE12345",
    "google_oauth": "ya29.FAKEfakeFAKEfakeFAKEfake",
    "huggingface": "hf_FAKEfakeFAKEfakeFAKEfakeFAKEfake12",
    "npm": "npm_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234",
    "slack": "xoxb-1234567890-FAKEfakeFAKE",
    "stripe": "sk_live_FAKEfakeFAKEfake1234",
    "sendgrid": "SG.FAKEfakeFAKEfake12.FAKEfakeFAKEfake1234",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.FAKEfakeFAKEfake",
    "email": "someone.fake@example.com",
}


@pytest.mark.parametrize("name,secret", FAKE.items())
def test_vendor_tokens(name, secret):
    out = redact(f"before {secret} after")
    assert secret not in out and "[REDACTED_" in out and out.startswith("before ") and out.endswith(" after")


@pytest.mark.parametrize("text,secret", [
    ("OPENAI_API_KEY=abc123def456ghi789", "abc123def456ghi789"),
    ('export STRIPE_SECRET_KEY="whsec_fakefakefake"', "whsec_fakefakefake"),
    ('{"api_key": "fakeKEYvalue123456"}', "fakeKEYvalue123456"),
    ("client_secret: zzFAKEzzFAKEzz", "zzFAKEzzFAKEzz"),
    ("DB_PASSWORD=hunter2", "hunter2"),
    ('password: "s3cr3t!"', "s3cr3t!"),
    ("mysql -u root --password fakepw99", "fakepw99"),
    ("Authorization: Bearer abcdefghij0123456789xyz", "abcdefghij0123456789xyz"),
    ("curl -H 'Authorization: token 0123456789abcdef'", "0123456789abcdef"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----", "MIIfake"),
])
def test_assignments_headers_keys(text, secret):
    assert secret not in redact(text)


@pytest.mark.parametrize("text,gone", [
    ("postgres://admin:fakepass@db.example.com:5432/app", "fakepass"),
    ("https://user:fakepass@git.example.com/repo.git", "fakepass"),
    ("http://localhost:3000/admin", "localhost:3000"),
    ("http://192.168.1.20:8080/api", "192.168.1.20"),
    ("http://10.0.0.5/metrics", "10.0.0.5"),
    ("https://grafana.internal/d/abc", "grafana.internal"),
    ("https://bucket.s3.amazonaws.com/f.pdf?X-Amz-Signature=deadbeefFAKE&X-Amz-Expires=60", "deadbeefFAKE"),
    ("https://api.example.com/cb?code=FAKECODE123&state=x", "FAKECODE123"),
    ("https://hooks.slack.com/services/T000/B000/FAKEFAKE", "FAKEFAKE"),
])
def test_private_and_credential_urls(text, gone):
    assert gone not in redact(text)


def test_signed_url_keeps_non_secret_parts():
    out = redact("https://example.com/f.pdf?token=FAKE123&page=2")
    assert out == "https://example.com/f.pdf?token=[REDACTED]&page=2"


@pytest.mark.parametrize("text", [
    "Built a REST API in FastAPI with JWT auth",
    "https://github.com/someone/repo and https://deepnova-ten.vercel.app",
    "tokenizer = AutoTokenizer.from_pretrained('Qwen/Qwen2.5-1.5B')",
    "max_tokens=16000, input_tokens: 14749",
    "API_KEY = os.environ['API_KEY']",
    "const key = process.env.OPENAI_API_KEY",
    "password_reset_flow() handles forgotten passwords",
    "Set the TOKEN env var before running",
    "There is no password reset flow; build password reset next",
    "show incorrect-password errors",
    'enc = tok(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)',
])
def test_ordinary_text_untouched(text):
    assert redact(text) == text


def test_redact_obj_nested():
    out = redact_obj({"a": ["x ghp_" + "F" * 36], "b": 3, "c": {"d": "me@fake.com"}})
    assert "ghp_" not in out["a"][0] and out["b"] == 3 and "fake.com" not in out["c"]["d"]

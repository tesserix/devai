"""PII masking — email/phone/IP, and the cross-user email helper."""

from devai.services.redact import mask_email, mask_pii, redact_secrets


def test_mask_email_keeps_domain_drops_local():
    assert mask_email("alice@corp.com") == "a***@corp.com"
    assert mask_email("bob.smith@x.io") == "b***@x.io"
    assert mask_email("") == ""
    assert mask_email("not-an-email") == "not-an-email"


def test_mask_pii_covers_email_phone_ip():
    out = mask_pii("contact alice@corp.com or +1 415 555 0100 at 10.20.3.146")
    assert "alice@corp.com" not in out
    assert "a***@corp.com" in out
    assert "415 555" not in out
    assert "10.20.3.146" not in out and "10.x.x.146" in out


def test_pii_and_secret_masking_compose():
    raw = "user alice@corp.com key sk-ant-abcdefgh12345"
    masked = mask_pii(redact_secrets(raw))
    assert "alice@corp.com" not in masked
    assert "sk-ant-abcdefgh12345" not in masked
    assert "sk-ant-***" in masked


def test_scrub_structure_masks_nested_secrets_and_pii():
    from devai.services.redact import scrub_structure

    payload = [
        {
            "sender": "alice@corp.com",
            "content": "deploying with token ghp_abcdefgh12345 to 10.20.3.146",
            "config": {"api_key": "super-secret-value", "model": "gemini-2.5-flash"},
        }
    ]
    out = scrub_structure(payload)
    s = str(out)
    assert "alice@corp.com" not in s and "a***@corp.com" in s
    assert "ghp_abcdefgh12345" not in s
    assert "10.20.3.146" not in s
    # Secret-named field masked wholesale; non-secret field preserved.
    assert out[0]["config"]["api_key"] == "***"
    assert out[0]["config"]["model"] == "gemini-2.5-flash"


def test_scrub_combines_both_passes():
    from devai.services.redact import scrub

    out = scrub("alice@corp.com used sk-ant-abcdefgh12345")
    assert "alice@corp.com" not in out and "sk-ant-***" in out

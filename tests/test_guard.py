from __future__ import annotations

import logging

import pytest

from hub import guard
from hub.errors import ApiError

SAMPLES = {
    "health_card": "health card ABCD-1234-5678-12 end",
    "sin": "sin 123-456-789 end",
    "passport": "passport AB123456 end",
    "driver_licence": "driver's licence number D1234-567890-12 end",
    "bank_account": "account 123456789012 end",
    "payment_card": "pay 4111 1111 1111 1111 end",
    "openai_key": "key sk-" + "a1" * 12 + " end",
    "aws_key": "key AKIA" + "ABCDEFGH12345678" + " end",
    "private_key": "-----BEGIN PRIVATE KEY-----\nabc",
    "jwt": "tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3.sig",
    "credential": "password: hunter2hunter2",
}


def cfg(**env: str) -> guard.GuardConfig:
    return guard.load(env)


def test_every_detector_has_a_sample_or_is_covered():
    assert {d.name for d in guard.DETECTORS} - set(SAMPLES) == {"payment_card_checksum"}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_modes_per_detector(name):
    text = SAMPLES[name]
    default = next(d.default for d in guard.DETECTORS if d.name == name)
    if default == "redact":
        out, counts = guard.redact(text, cfg())
        assert counts and "[REDACTED:" in out
    else:
        assert guard.find_block(text, cfg()).detector == name
    off = cfg(**{f"PKB_GUARD_{name.upper()}": "off"})
    assert guard.find_block(text, off) is None
    assert guard.redact(text, off)[0] == text
    blocking = cfg(**{f"PKB_GUARD_{name.upper()}": "block"})
    assert guard.find_block(text, blocking).detector == name
    redacting = cfg(**{f"PKB_GUARD_{name.upper()}": "redact"})
    assert guard.find_block(text, redacting) is None


def test_allow_list_exact_and_pattern():
    text = SAMPLES["sin"]
    assert guard.redact(text, cfg(PKB_GUARD_ALLOW='["123-456-789"]'))[0] == text
    assert guard.redact(text, cfg(PKB_GUARD_ALLOW_PATTERNS='["123-.*"]'))[0] == text


def test_payment_card_checksum_option():
    text = "pay 1234 5678 9012 3456 end"
    assert guard.redact(text, cfg())[1]
    assert not guard.redact(text, cfg(PKB_GUARD_PAYMENT_CARD_REQUIRE_CHECKSUM="true"))[1]


@pytest.mark.parametrize("env", [
    {"PKB_GUARD_SIN": "maybe"},
    {"PKB_GUARD_NOPE": "off"},
    {"PKB_GUARD_ALLOW": "not json"},
    {"PKB_GUARD_ALLOW_PATTERNS": '["("]'},
])
def test_bad_config_is_rejected(env):
    with pytest.raises(guard.GuardConfigError):
        guard.load(env)


def test_bad_config_fails_startup(monkeypatch, tmp_path):
    from hub import create_app
    from tests.conftest import make_settings

    monkeypatch.setenv("PKB_GUARD_SIN", "maybe")
    with pytest.raises(guard.GuardConfigError):
        create_app(make_settings(tmp_path))


def test_block_error_and_logs_omit_value(caplog):
    secret = SAMPLES["openai_key"]
    finding = guard.find_block(secret, cfg())
    err = guard.blocked_error(finding)
    assert isinstance(err, ApiError)
    assert secret.split()[1] not in str(err.message) + str(getattr(err, "extra", ""))
    with caplog.at_level(logging.DEBUG):
        guard.log_startup(cfg(PKB_GUARD_SIN="off", PKB_GUARD_ALLOW='["123-456-789"]'))
    assert "123-456-789" not in caplog.text
    assert "sin is OFF" in caplog.text


def test_reduced_flag():
    assert not cfg().reduced
    assert cfg(PKB_GUARD_JWT="off").reduced

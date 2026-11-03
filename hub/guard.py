"""Sensitive-data guard: one detector table, configurable per detector.

Defaults reproduce the historical behaviour exactly: identifiers are redacted, secrets block the
capture. Matched values are never logged or returned; findings carry the detector and position.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ApiError

log = logging.getLogger(__name__)

MODES = ("block", "redact", "off")
ENV_PREFIX = "PKB_GUARD_"
_SEP = r"[ \-]?"


def _luhn(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    total = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


@dataclass(frozen=True)
class Detector:
    name: str
    kind: str
    pattern: re.Pattern
    default: str
    span_group: int = 0  # group holding the sensitive value; other groups are kept on redaction
    checksum: bool = False


DETECTORS: tuple[Detector, ...] = (
    Detector("health_card", "health_card", re.compile(
        rf"\b[A-Z]{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}(?:{_SEP}\d{{1,4}})?\b"), "redact"),
    Detector("sin", "government_id", re.compile(
        r"(?<![\d-])\d{3}[ -]\d{3}[ -]\d{3}(?![\d-])"), "redact"),
    Detector("passport", "government_id", re.compile(
        r"(\bpassport(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*)([A-Z]{1,2}\d{6,8}|[A-Z0-9]{8,9})\b",
        re.IGNORECASE), "redact", 2),
    Detector("driver_licence", "government_id", re.compile(
        r"(\b(?:driver'?s?|driving)\s+licen[cs]e(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*(?:is\s+)?)"
        r"([A-Z0-9][A-Z0-9 \-]{5,19}[A-Z0-9])(?![A-Za-z0-9])", re.IGNORECASE), "redact", 2),
    Detector("bank_account", "bank_account", re.compile(
        r"(\b(?:account|IBAN)\b(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*(?:is\s+)?)"
        r"([A-Z]{2}\d{2}[A-Z0-9 ]{10,30}[A-Z0-9]|\d[\d \-]{4,32}\d)(?![A-Za-z0-9])",
        re.IGNORECASE), "redact", 2),
    Detector("payment_card", "payment_card", re.compile(
        r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), "redact"),
    Detector("openai_key", "openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), "block"),
    Detector("aws_key", "aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "block"),
    Detector("private_key", "private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "block"),
    Detector("jwt", "jwt", re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\."), "block"),
    Detector("credential", "credential", re.compile(
        r"(?i)(password|mot de passe|passwd|api[_ -]?key|token|secret)\s*[:=]\s*\S{6,}"), "block"),
    Detector("payment_card_checksum", "payment_card", re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
             "block", checksum=True),
)
BY_NAME = {detector.name: detector for detector in DETECTORS}


class GuardConfigError(ValueError):
    """Raised at startup for an invalid guard configuration."""


@dataclass(frozen=True)
class Finding:
    detector: str
    kind: str
    position: int


@dataclass(frozen=True)
class GuardConfig:
    modes: Mapping[str, str] = field(default_factory=lambda: {d.name: d.default for d in DETECTORS})
    allow_exact: frozenset[str] = frozenset()
    allow_patterns: tuple[re.Pattern, ...] = ()
    payment_card_require_checksum: bool = False

    def mode(self, name: str) -> str:
        return self.modes[name]

    def summary(self) -> dict[str, str]:
        return {detector.name: self.modes[detector.name] for detector in DETECTORS}

    @property
    def reduced(self) -> bool:
        return any(mode == "off" for mode in self.modes.values())

    def allowed(self, value: str) -> bool:
        return value in self.allow_exact or any(
            pattern.fullmatch(value) for pattern in self.allow_patterns)


def _string_list(raw: object, source: str) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise GuardConfigError(f"{source} must be a JSON array of strings") from exc
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise GuardConfigError(f"{source} must be a list of non-empty strings")
    return raw


def _bool(raw: object, source: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("1", "true", "yes", "on", "0", "false",
                                                        "no", "off"):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    raise GuardConfigError(f"{source} must be true or false")


def _read_file(path: str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) if text.strip() else {}
    except (OSError, yaml.YAMLError) as exc:
        raise GuardConfigError(f"PKB_GUARD_CONFIG could not be read: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise GuardConfigError("PKB_GUARD_CONFIG must contain a mapping")
    unknown = set(data) - {"detectors", "allow", "allow_patterns",
                           "payment_card_require_checksum"}
    if unknown:
        raise GuardConfigError(f"PKB_GUARD_CONFIG has unknown keys: {sorted(unknown)}")
    return data


def load(env: Mapping[str, str] | None = None, *, block_secrets: bool = True) -> GuardConfig:
    """Build the configuration from PKB_GUARD_* variables and an optional YAML/JSON file.

    Environment variables win over the file. block_secrets=False is the legacy
    CAPTURE_BLOCK_SECRETS switch: it turns the default-block detectors off.
    """
    env = os.environ if env is None else env
    modes = {d.name: ("off" if d.default == "block" and not block_secrets else d.default)
             for d in DETECTORS}
    file_data = _read_file(env["PKB_GUARD_CONFIG"]) if env.get("PKB_GUARD_CONFIG") else {}

    file_modes = file_data.get("detectors", {})
    if not isinstance(file_modes, dict):
        raise GuardConfigError("PKB_GUARD_CONFIG detectors must be a mapping")
    for name, value in file_modes.items():
        if name not in BY_NAME:
            raise GuardConfigError(f"PKB_GUARD_CONFIG: unknown detector {name!r}")
        modes[name] = _mode(value, f"PKB_GUARD_CONFIG detectors.{name}")

    known = {ENV_PREFIX + d.name.upper() for d in DETECTORS} | {
        ENV_PREFIX + suffix for suffix in
        ("ALLOW", "ALLOW_PATTERNS", "CONFIG", "PAYMENT_CARD_REQUIRE_CHECKSUM")}
    for key in env:
        if key.startswith(ENV_PREFIX) and key not in known:
            raise GuardConfigError(f"unknown guard variable {key}")
    for detector in DETECTORS:
        key = ENV_PREFIX + detector.name.upper()
        if env.get(key):
            modes[detector.name] = _mode(env[key], key)

    allow = file_data.get("allow", [])
    patterns = file_data.get("allow_patterns", [])
    checksum = file_data.get("payment_card_require_checksum", False)
    if env.get(ENV_PREFIX + "ALLOW"):
        allow = env[ENV_PREFIX + "ALLOW"]
    if env.get(ENV_PREFIX + "ALLOW_PATTERNS"):
        patterns = env[ENV_PREFIX + "ALLOW_PATTERNS"]
    if env.get(ENV_PREFIX + "PAYMENT_CARD_REQUIRE_CHECKSUM"):
        checksum = env[ENV_PREFIX + "PAYMENT_CARD_REQUIRE_CHECKSUM"]

    compiled = []
    for index, item in enumerate(_string_list(patterns, "PKB_GUARD_ALLOW_PATTERNS")):
        if len(item) > 200:
            raise GuardConfigError(f"allow pattern #{index + 1} is longer than 200 characters")
        try:
            compiled.append(re.compile(item))
        except re.error as exc:
            raise GuardConfigError(f"allow pattern #{index + 1} is not a valid regex") from exc
    return GuardConfig(
        modes=modes,
        allow_exact=frozenset(_string_list(allow, "PKB_GUARD_ALLOW")),
        allow_patterns=tuple(compiled),
        payment_card_require_checksum=_bool(checksum, "PKB_GUARD_PAYMENT_CARD_REQUIRE_CHECKSUM"),
    )


def _mode(value: object, source: str) -> str:
    mode = str(value).strip().lower()
    if mode not in MODES:
        raise GuardConfigError(f"{source} must be one of {', '.join(MODES)}")
    return mode


_current = GuardConfig()


def configure(config: GuardConfig) -> None:
    global _current
    _current = config


def current() -> GuardConfig:
    return _current


def log_startup(config: GuardConfig) -> None:
    log.info("guard detectors: %s", ", ".join(f"{k}={v}" for k, v in config.summary().items()))
    for name, mode in config.summary().items():
        if mode == "off":
            log.warning("guard detector %s is OFF: matching values are neither redacted nor "
                        "blocked", name)
    if config.allow_exact or config.allow_patterns:
        log.info("guard allow-list: %d exact, %d patterns",
                 len(config.allow_exact), len(config.allow_patterns))


def _ignored(detector: Detector, span: str, config: GuardConfig) -> bool:
    """True for a match that fails the checksum or is allow-listed."""
    if detector.checksum and not _luhn(span):
        return True
    if (detector.name == "payment_card" and config.payment_card_require_checksum
            and not _luhn(span)):
        return True
    return config.allowed(span)


def _blocked(detector: Detector, text: str, config: GuardConfig) -> Finding | None:
    for match in detector.pattern.finditer(text):
        if not _ignored(detector, match.group(detector.span_group), config):
            return Finding(detector.name, detector.kind, match.start(detector.span_group))
    return None


def find_block(text: str, config: GuardConfig | None = None) -> Finding | None:
    """First hit among detectors in block mode; never exposes the matched value."""
    config = config or _current
    for detector in DETECTORS:
        if config.mode(detector.name) == "block":
            finding = _blocked(detector, text, config)
            if finding:
                return finding
    return None


def blocked_error(finding: Finding) -> ApiError:
    return ApiError("secret_detected", f"possible {finding.kind} detected", 409,
                    kind=finding.kind, detector=finding.detector, position=finding.position)


def redact(text: str, config: GuardConfig | None = None) -> tuple[str, dict[str, int]]:
    """Return (redacted text, counts per kind). Spans are never returned.

    Detectors in redact mode are substituted. A detector that normally redacts but was set to
    block rejects here too, so every model-bound path honours it.
    """
    config = config or _current
    counts: Counter[str] = Counter()
    for detector in DETECTORS:
        mode = config.mode(detector.name)
        if mode == "block" and detector.default == "redact":
            finding = _blocked(detector, text, config)
            if finding:
                raise blocked_error(finding)
        if mode != "redact":
            continue
        text = _substitute(detector, text, config, counts)
    return text, dict(counts)


def _substitute(detector: Detector, text: str, config: GuardConfig, counts: Counter) -> str:
    marker = f"[REDACTED:{detector.kind}]"
    prefix_group = 1 if detector.span_group else 0

    def sub(match: re.Match) -> str:
        if _ignored(detector, match.group(detector.span_group), config):
            return match.group(0)
        counts[detector.kind] += 1
        return (match.group(prefix_group) if prefix_group else "") + marker

    return detector.pattern.sub(sub, text)

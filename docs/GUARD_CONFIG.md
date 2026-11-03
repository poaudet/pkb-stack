# Sensitive-data guard configuration

Every capture input is checked by `hub/guard.py` before storage or any LLM call.
Each detector has a mode: `redact` (replace with `[REDACTED:<kind>]`), `block`
(reject with 409 `secret_detected`; the response carries kind, detector and
position, never the value) or `off`.

| Detector | Default | Variable |
|---|---|---|
| health_card, sin, passport, driver_licence, bank_account, payment_card | redact | `PKB_GUARD_<NAME>` |
| openai_key, aws_key, private_key, jwt, credential | block | `PKB_GUARD_<NAME>` |
| payment_card_checksum (Luhn-valid card numbers) | block | `PKB_GUARD_PAYMENT_CARD_CHECKSUM` |

Block defaults become `off` when `PKB_CAPTURE_BLOCK_SECRETS=false` (legacy switch).

Other settings:

- `PKB_GUARD_ALLOW`: JSON array of exact values that are never flagged.
- `PKB_GUARD_ALLOW_PATTERNS`: JSON array of regexes; a value is allowed on a full match.
- `PKB_GUARD_PAYMENT_CARD_REQUIRE_CHECKSUM=true`: redact only Luhn-valid card numbers.
- `PKB_GUARD_CONFIG`: path to a YAML/JSON file with the same keys (environment wins).

Unknown `PKB_GUARD_*` variables, invalid modes or bad regexes stop startup with a clear
error. Turning any detector `off` logs a warning at startup, sets `guard.reduced` in
`/health`, and shows a banner on the Status tab. Logs never include matched values.
Note: the tidy-up LLM path and embeddings are not covered by the guard.

# Changelog

## 0.1.0 (unreleased; built and tested in CI, not yet on PyPI)

First working release. The inherited Phase 1 code did not import; every item below was
implemented or fixed in this repository, with tests (see `DECISIONS.md` for the reasons).

### Added
- `cfa_shoc` package (src layout, MIT): public API in `cfa_shoc/__init__.py`.
- Transaction state machine: 14 states, 24 legal transitions, `FINAL_STATES`, compensation
  exits, `authorization_revoked`.
- `RUN_TOOL_NODE` orchestration wired to the real services: ledger refresh, normalization,
  preflight, policy, signature/expiry check, lease, dispatch, verification, commit gate.
  Each state change is stored with its audit event in one database transaction.
- Real preflight checks: input schema (built-in validator), per-tenant quotas, contract
  `max_risk`, in-flight conflicts (idempotency key, exclusive tools), ledger currency.
- Policy lanes A (automatic), B (contract pre-authorization) and C (explicit confirmation via
  `RUN_TOOL_NODE.confirm` / `decline`).
- HMAC-SHA256 signed policy and commit decisions with injected keys.
- Result verifier: status, output schema, tool/connector match, idempotency echo.
- Idempotent replay of committed calls.
- Durable per-transaction hash-chained audit trail; `StorageManager.verify_chain`.
- Reconciliation of PENDING_EXTERNAL / UNKNOWN / COMPENSATION_REQUIRED / dead-dispatcher
  transactions (`RUN_TOOL_NODE.reconcile`, `ReconciliationWorker.run_once`), with optional
  per-tool compensators.
- DRxit batons (`cfa_shoc.baton`): signed, validated, acknowledged handoffs between legs.
- Provenance certificates (`cfa_shoc.provenance`, Ed25519, Merkle commitment) and the
  `cfa-shoc verify` command (`[provenance]` extra).
- `examples/demo_echo.py`, run in CI.

### Fixed (inherited defects D1 to D19)
- Import failure (`PolicyDecision` field order), contradictory terminal-state model, dead-end
  states, orchestrator calls to methods that did not exist, lost context ids, ledger hash that
  changed on every refresh (every commit was rejected), state not persisted, in-memory-only
  audit chain, stub checks that always passed, unsigned "signatures", broken test fixtures and
  assertions that passed for broken code, packaging, deprecated APIs, false documentation.

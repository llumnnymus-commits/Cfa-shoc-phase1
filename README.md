# CFA-SHOC

[![CI](https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/workflows/ci.yml/badge.svg)](https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/workflows/ci.yml)

**A transaction layer for AI agent tool calls.** Each tool call an agent proposes becomes a
transaction. It must pass preflight checks, a signed policy decision, a dispatch lease and
result verification before a commit gate accepts it. Every step is recorded in a
hash-chained audit trail. A committed call can be exported as a signed provenance
certificate that anyone can verify offline.

It **produces audit evidence**. It does not make anyone compliant with any law or standard.

Status: version 0.1.0 (alpha). CI builds and tests it on Python 3.11, 3.12 and 3.13. It is
**not yet published to PyPI**.

## Why

An agent that sends email, moves money, edits files or writes to a database needs more than
a function call. You need to know what it was allowed to do, what it actually did, whether
the result was checked, and proof that nobody edited the record afterwards. CFA-SHOC puts a
deterministic state machine between the model's proposal and the tool:

```
PROPOSED -> PREFLIGHTED -> AUTHORIZED -> DISPATCHED -> RETURNED -> VERIFIED -> COMMITTED
               |               |             |  \                     |
            REJECTED        REJECTED      FAILED  PENDING_EXTERNAL / UNKNOWN -> reconcile,
                                                   or COMPENSATION_REQUIRED -> COMPENSATED
```

14 states and 24 legal transitions. Nothing can reach COMMITTED without passing through
VERIFIED and the commit gate.

## Quick example

```python
from uuid import uuid4
from cfa_shoc import (
    ActionNormalizer, AuditLedger, CapabilitySnapshotService, CommitMembrane, OrchestrationContext,
    PolicyDecisionPoint, PreflightService, ResultVerifier, RUN_TOOL_NODE, StorageManager, ToolAdapter,
)

storage = StorageManager("sqlite:///:memory:")
storage.init_db()
ledger = CapabilitySnapshotService()
ledger.update_tool_state("tool_echo", {"id": "tool_echo"})  # the tool exists in the ledger
tools = ToolAdapter()
tools.register("tool_echo", lambda req: {"status": "succeeded", "output": {"echo": req.arguments["message"]}})

run = RUN_TOOL_NODE(storage, ledger, ActionNormalizer(ledger), PreflightService(ledger),
                    PolicyDecisionPoint(), tools, ResultVerifier(), CommitMembrane(), AuditLedger())
ctx = OrchestrationContext(uuid4(), uuid4(), uuid4(), principal_id="me", tenant_id="t1", requested_by="model")

tx, result = run.execute({"tool_id": "tool_echo", "operation": "echo",
                          "arguments": {"message": "hi"}, "risk_class": "low"}, ctx)
print(tx.state.value, "-", result.reason)      # COMMITTED - Transaction committed successfully
print(storage.verify_chain(tx.transaction_id))  # (True, None)
```

[`tests/test_readme.py`](tests/test_readme.py) runs this exact block in CI. The full demo,
[`examples/demo_echo.py`](examples/demo_echo.py), runs as its own CI job. It shows an approved
call, a denied call, a human-confirmed call, the audit trail, chain verification and a
provenance certificate.

## Guarantees, and the test that proves each one

| Guarantee | Proven by |
|---|---|
| An unregistered tool is refused before any dispatch | [`test_orchestrator_flow.py::test_unregistered_tool_is_refused_before_dispatch`](tests/test_orchestrator_flow.py) |
| A call the policy denies never reaches the tool | [`test_orchestrator_flow.py::test_medium_risk_call_is_rejected_by_policy_and_never_dispatched`](tests/test_orchestrator_flow.py) |
| A forged or expired policy decision is revoked before dispatch | [`test_real_checks.py::test_forged_policy_decision_is_revoked_before_dispatch`](tests/test_real_checks.py), [`::test_expired_policy_decision_is_revoked_before_dispatch`](tests/test_real_checks.py) |
| Policy and commit decisions are HMAC-signed; tampering is detected | [`test_real_checks.py::TestPolicySignatures`](tests/test_real_checks.py), [`::TestCommitSignatures`](tests/test_real_checks.py) |
| No path reaches COMMITTED except through VERIFIED and the commit gate | [`test_chaos_and_properties.py::test_property_no_state_jump_to_committed`](tests/test_chaos_and_properties.py), [`test_real_checks.py::TestCommitSignatures::test_gate_refuses_unverified_transaction`](tests/test_real_checks.py) |
| No transaction can get stuck: every non-terminal state has an exit | [`test_state_machine.py::TestStepThreeTransitions::test_every_non_terminal_state_has_an_exit`](tests/test_state_machine.py) |
| A commit is refused if the capability ledger or the contract changed after authorization | [`test_orchestrator_flow.py::test_ledger_change_during_dispatch_blocks_commit`](tests/test_orchestrator_flow.py), [`::test_contract_change_during_dispatch_blocks_commit`](tests/test_orchestrator_flow.py) |
| Each state change and its audit event are stored together; the stored state always equals the in-memory state | [`test_orchestrator_flow.py::test_durable_state_matches_memory_after_every_scenario`](tests/test_orchestrator_flow.py) |
| The stored audit trail is hash-chained per transaction; editing, deleting or re-hashing an event is detected at its index | [`test_audit_and_reconcile.py::test_editing_a_stored_payload_is_detected`](tests/test_audit_and_reconcile.py), [`::test_deleting_an_event_is_detected`](tests/test_audit_and_reconcile.py), [`::test_recomputing_an_edited_hash_breaks_the_next_link`](tests/test_audit_and_reconcile.py) |
| Retrying a committed call with the same idempotency key returns the original transaction without dispatching again | [`test_orchestration.py::TestConcurrency::test_duplicate_proposal_uses_same_transaction`](tests/test_orchestration.py) |
| A second call with the same idempotency key is refused while the first is in flight | [`test_real_checks.py::test_same_idempotency_key_in_flight_conflicts`](tests/test_real_checks.py) |
| Arguments and outputs are validated against the tool's declared schemas | [`test_real_checks.py::test_schema_check_rejects_bad_arguments`](tests/test_real_checks.py), [`::TestResultVerifier::test_output_schema_enforced`](tests/test_real_checks.py) |
| Per-tenant quotas and contract risk caps are enforced in preflight | [`test_real_checks.py::test_quota_exhausted_is_rejected_before_dispatch`](tests/test_real_checks.py), [`::test_risk_above_contract_max_risk_is_rejected`](tests/test_real_checks.py) |
| Risky calls can require a named human approver (lane C); nothing is dispatched before confirmation | [`test_real_checks.py::test_lane_c_waits_in_preflighted_without_dispatch`](tests/test_real_checks.py), [`::test_lane_c_confirm_commits_with_named_approver`](tests/test_real_checks.py) |
| A timeout is recorded as UNKNOWN, never as success; reconciliation settles it with the provider | [`test_orchestrator_flow.py::test_timeout_ends_unknown`](tests/test_orchestrator_flow.py), [`test_audit_and_reconcile.py::test_unknown_succeeded_commits`](tests/test_audit_and_reconcile.py) |
| A possible side effect with an unknown outcome leads to compensation, not silence | [`test_audit_and_reconcile.py::test_unknown_with_side_effect_requires_then_gets_compensation`](tests/test_audit_and_reconcile.py) |
| A provenance certificate verifies offline; any edit to it, or to the stored events, is detected | [`test_provenance.py`](tests/test_provenance.py) (spec acceptance tests 1 to 7) |
| Agent-to-agent handoffs (DRxit batons) fail closed when unsupported, unsigned, contradictory or missing evidence | [`test_baton.py`](tests/test_baton.py) (spec acceptance tests 1 to 9) |

## Concepts

- **Capability ledger** (`CapabilitySnapshotService`): the tools, connectors, quotas and
  policies that exist right now. Its hash covers content only, so it changes only when the
  content changes.
- **Contract** (`OrchestrationContext.contract`): what this episode may do: `scopes`,
  `max_risk`, `pre_authorized` entries (lane B) and `confirm_up_to` (lane C).
- **Policy lanes**: A, automatic (low risk, no lasting side effect); B, pre-authorized in the
  contract; C, explicit confirmation by a named approver. Everything else is denied.
- **Commit gate** (`CommitMembrane`): re-reads the ledger and the contract at commit time and
  refuses if either changed, or if the result was not verified.
- **Audit trail**: `StorageManager.verify_chain(tx_id)` returns `(ok, first_bad_index)`.
- **Reconciliation**: `ReconciliationWorker(..., orchestrator).run_once()` settles UNKNOWN,
  PENDING_EXTERNAL and COMPENSATION_REQUIRED transactions.
- **Provenance**: `cfa_shoc.provenance.issue_certificate(...)`, then
  `cfa-shoc verify cert.json --trust did:key:...` (needs the `[provenance]` extra).
- **DRxit batons**: `cfa_shoc.baton.seal / validate / acknowledge / retire / chain`, for
  signed handoffs between legs of a long agent job ([spec](docs/DRXIT_BATON_SPEC.md)).

## Limits (read before relying on it)

- **Alpha.** Tested with SQLite only. PostgreSQL is supported through SQLAlchemy but not tested
  in CI.
- **Single process.** Quota usage counters live in memory per `CapabilitySnapshotService`.
  Dispatch leases are checked by a database query, not by row locks, and concurrent writers
  across processes have not been tested.
- **Keys.** Policy and commit decisions use HMAC-SHA256. Without a `signing_key`, each process
  makes a random key, so its decisions verify only within that process. Pass keys explicitly
  if decisions must be checked elsewhere.
- **Tool results are not signed** by tools in this version. `ResultVerifier` checks status,
  schema, tool/connector match and idempotency echo, and reports `signature_valid=False`.
- **Certificates** use the spec's source form: an Ed25519 signature over the JCS-canonical
  `credentialSubject`, with a `did:key` issuer. They are VC-shaped and verifiable with this
  library. They are not claimed to verify with generic Verifiable Credential tooling;
  `eddsa-jcs-2022` is future work.
- **Audit chain anchor.** Rewriting the *last* stored event together with its hash cannot be
  detected from the database alone. A provenance certificate issued earlier is the external
  anchor that exposes it.
- **Schema validation** supports a subset of JSON Schema (type, required, properties,
  additionalProperties false, enum, length and range limits, items, maxItems).
- The API is synchronous.

## Install and develop

From a checkout: `pip install -e ".[dev]"`, then `pytest`. CI does exactly this on every push
([workflow](.github/workflows/ci.yml)). Runtime dependency: SQLAlchemy. Optional:
`cryptography` for provenance (`[provenance]` extra).

## History

The files in [`docs/history/`](docs/history/) are from before this rewrite. They contain
claims that were false when written ("production-ready", "70+ tests, all passing"). The
inherited code did not import. What was fixed, and why, is in [`DECISIONS.md`](DECISIONS.md)
and [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT, see [`LICENSE`](LICENSE).

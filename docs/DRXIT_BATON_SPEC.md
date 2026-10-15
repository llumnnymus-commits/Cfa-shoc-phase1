# DRxit Baton — implementation spec for `cfa_shoc/baton.py`

Source: the owner's VisceRL / NossR / DRxit document in Google Drive, where DRxit means
"Driven Reinforcement Exit". The field names and rules below are taken from that document.
Lines marked **(added)** were added here so the design can be implemented and tested; they are
not in the source.

## Idea in one paragraph
Long agent jobs are split into bounded **legs**. Each leg does one verifiable piece of work.
It then writes a **baton**: a structured, signed handoff object holding what it verified, what
it produced, what is unresolved, how it scored, and the contract for the next leg. The next leg
starts only after it validates and acknowledges the baton. Only then does the old leg retire.
"The exit is not disappearance. The exit is propulsion." In CFA-SHOC terms, every tool call a
leg makes is a CFA-SHOC transaction, and the baton is the governed record that links legs.

## Rules from the source (enforce in code)
1. A baton is a real artifact, not a prose summary.
2. "No iteration may hand off intention without evidence." Every claim in `world_state.confirmed_facts`
   needs at least one `evidence_ref`.
3. A malformed, unsupported, contradictory baton, or one missing `next_leg_contract.rollback_state`,
   fails closed. The handoff is refused. It is never passed on silently.
4. The predecessor retires only after the baton is **persisted**, **schema-validated** and
   **acknowledged** by the successor.
5. The successor inherits verified facts, artifacts, traces, decisions, tests, rewards, open
   contradictions and constraints. It does not inherit raw hidden reasoning.
6. Termination is a verification decision, not the agent's feeling that it is done.

## Baton schema (JSON)
```json
{
  "baton_version": "1",                         // (added)
  "run_id": "drxit-2026-08-14-001",
  "leg": 4,
  "parent_leg": 3,
  "parent_baton_hash": "sha256 hex or null",    // (added) chains batons like the audit log
  "global_objective": "Deliver a verified production-safe implementation",
  "local_objective": "Diagnose failing integration test",
  "world_state": {
    "confirmed_facts": [{"fact": "...", "evidence_refs": ["tx:<uuid>", "artifact:<path>"]}],
    "uncertainties": ["..."],
    "open_contradictions": ["..."],              // (added, named in source text)
    "constraints": ["..."]                       // (added, named in source text)
  },
  "artifacts": [{"path": "test-report.json", "sha256": "..."}],    // (added)
  "transactions": ["<cfa-shoc transaction uuid>", "..."],          // (added) link to audit trail
  "evaluation": {
    "reward": 0.0,
    "verifier_results": [],
    "cost": 0.0,
    "latency_ms": 0,
    "failure_mode": null
  },
  "handoff_decision": "HANDOFF_TO_VERIFIER",
  "next_leg_contract": {
    "required_capability": "debugger",
    "objective": "Test adapter against the staging database",
    "entry_conditions": [],
    "entry_state": ["test-report.json", "environment-manifest.json"],
    "success_condition": [],
    "completion_criteria": ["integration suite passes", "rollback procedure verified"],
    "authority": "staging-only",
    "rollback_state": null
  },
  "created_at": "ISO-8601 UTC",                  // (added)
  "signature": "hmac-sha256 hex (Ed25519 once provenance lands)"   // (added)
}
```

`handoff_decision` is one of: `HANDOFF_TO_PLANNER`, `HANDOFF_TO_BUILDER`,
`HANDOFF_TO_EXECUTOR`, `HANDOFF_TO_CRITIC`, `HANDOFF_TO_VERIFIER`, `HANDOFF_TO_RECOVERY`,
`HANDOFF_TO_HUMAN`, `COMMIT_AND_CLOSE`.

Note on `rollback_state`: the source example shows `null`, and the source rule says a baton
missing the required rollback state fails closed. Implement it like this: the key must be
present. `null` is allowed only when `handoff_decision` is `COMMIT_AND_CLOSE`, or when every
listed transaction has side_effect_class `none`.

## Leg lifecycle (the invariant kernel)
`read contract → inspect inherited evidence → bounded work → verify → emit baton → evaluate
reward → nominate next leg → retire`

States for a baton: `DRAFT → SEALED (signed and persisted) → ACKNOWLEDGED (successor
validated it) → RETIRED (predecessor released resources)`, or `REJECTED` (validation failed,
handoff refused).

## Coordinator checks before acknowledging (from the source)
1. Was the local objective actually completed? `evaluation.verifier_results` must all pass,
   unless `failure_mode` is set and the decision is RECOVERY or HUMAN.
2. Which global requirements are still unsatisfied? These must appear in
   `next_leg_contract.completion_criteria` or `world_state.uncertainties`.
3. Is the proposed successor eligible? `required_capability` must be in the coordinator's
   registry.
4. Does the successor have the necessary authority and tools? Check `authority` against the
   CFA-SHOC contract scopes.
5. Is the baton complete, backed by artifacts, and internally consistent? Every artifact sha256
   must match, every transaction id must exist in storage and be in a final state, and the
   signature must verify.
6. Decide: proceed, retry, reroute, recover, escalate, or close.

## API to implement
```python
seal(baton: dict, key: bytes, storage) -> SealedBaton              # validate, sign, persist
validate(baton: dict, key: bytes, storage, registry) -> list[str]  # [] = valid
acknowledge(baton_id, successor_id, storage) -> None               # only if validate == []
retire(baton_id, storage) -> None                                  # only if ACKNOWLEDGED
chain(run_id, storage) -> list[SealedBaton]                        # verifies parent_baton_hash links
```

## Acceptance tests (step 10 is done when all pass in CI)
1. A valid baton seals, validates, acknowledges and retires, in that order only.
2. `retire` before `acknowledge` raises an error.
3. A confirmed fact with no evidence_refs is rejected.
4. A missing `rollback_state` key is rejected. `null` with irreversible transactions is rejected.
5. Changing any field after sealing makes the signature check fail.
6. An artifact whose sha256 doesn't match is rejected.
7. A transaction id that doesn't exist, or isn't in a final state, is rejected.
8. A 3-leg chain verifies. Breaking the middle `parent_baton_hash` is detected at the right index.
9. An unknown `handoff_decision` or unknown `required_capability` is rejected.

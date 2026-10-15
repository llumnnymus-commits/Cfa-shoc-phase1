"""
CFA-SHOC demo: three tool calls through the transaction layer.

  1. a low-risk echo           -> approved automatically (lane A), COMMITTED
  2. a medium-risk echo        -> denied by policy, REJECTED, never dispatched
  3. a medium-risk echo with an
     explicit-confirmation lane -> waits in PREFLIGHTED, a named human confirms, COMMITTED

Then it prints the audit trail, verifies every stored hash chain, and issues and verifies
a signed provenance certificate. Every outcome is asserted: the script exits non-zero if
anything differs. CI runs it as its own job.

Run:  pip install "cfa-shoc[provenance]"  then  python examples/demo_echo.py
"""

import sys
from uuid import uuid4

from cfa_shoc import (
    ActionNormalizer, AuditLedger, CapabilitySnapshotService, CommitMembrane,
    OrchestrationContext, PolicyDecisionPoint, PreflightService, ResultVerifier, RUN_TOOL_NODE,
    StorageManager, ToolAdapter, TransactionState,
)
from cfa_shoc.provenance import generate_signing_key, issue_certificate, issuer_did, verify_certificate


def build():
    storage = StorageManager("sqlite:///:memory:")
    storage.init_db()

    capability = CapabilitySnapshotService()
    capability.update_tool_state("tool_echo", {
        "id": "tool_echo",
        "scopes": ["echo"],
        "input_schema": {
            "type": "object",
            "required": ["message"],
            "properties": {"message": {"type": "string", "maxLength": 200}},
            "additionalProperties": False,
        },
        "output_schema": {"type": "object", "required": ["echo"]},
    })

    adapter = ToolAdapter()
    dispatched = []

    def echo(request):
        dispatched.append(request.arguments["message"])
        return {"status": "succeeded", "output": {"echo": request.arguments["message"]}}

    adapter.register("tool_echo", echo)

    orchestrator = RUN_TOOL_NODE(
        storage=storage,
        capability_service=capability,
        normalizer=ActionNormalizer(capability),
        preflight=PreflightService(capability),
        policy_point=PolicyDecisionPoint(),
        tool_adapter=adapter,
        verifier=ResultVerifier(),
        commit_membrane=CommitMembrane(),
        audit_ledger=AuditLedger(),
    )
    return orchestrator, storage, dispatched


def context(contract):
    return OrchestrationContext(
        proposal_id=uuid4(), episode_id=uuid4(), task_node_id=uuid4(),
        principal_id="demo-user", tenant_id="demo-tenant", requested_by="model",
        contract=contract,
    )


def call(message, risk):
    return {
        "tool_id": "tool_echo",
        "operation": "echo",
        "arguments": {"message": message},
        "risk_class": risk,
        "side_effect_class": "none",
    }


def print_trail(storage, transaction):
    session = storage.get_session()
    try:
        events = storage.get_events_for_transaction(session, str(transaction.transaction_id))
        for event in events:
            arrow = f"{event.from_state or '-':>11} -> {event.to_state or '-':<11}"
            print(f"    #{event.sequence} {event.event_type:<24} {arrow} {event.event_hash[:16]}")
    finally:
        session.close()


def check(condition, message):
    if not condition:
        print(f"DEMO FAILED: {message}")
        sys.exit(1)


def main():
    orchestrator, storage, dispatched = build()
    contract = {"scopes": ["echo"], "confirm_up_to": "medium"}

    print("1) Low-risk echo (lane A: automatic)")
    approved, result = orchestrator.execute(call("hello", "low"), context(contract))
    print(f"   -> {approved.state.value}: {result.reason}")
    print_trail(storage, approved)
    check(approved.state == TransactionState.COMMITTED, "low-risk call should commit")

    print("\n2) Medium-risk echo, contract without a confirmation lane")
    denied, result = orchestrator.execute(call("rm -rf maybe", "medium"), context({"scopes": ["echo"]}))
    print(f"   -> {denied.state.value}: {result.reason}")
    print_trail(storage, denied)
    check(denied.state == TransactionState.REJECTED, "medium-risk call should be denied")
    check("rm -rf maybe" not in dispatched, "a denied call must never reach the tool")

    print("\n3) Medium-risk echo, contract allows explicit confirmation (lane C)")
    waiting, result = orchestrator.execute(call("ship it", "medium"), context(contract))
    print(f"   -> {waiting.state.value}: {result.reason}")
    check(waiting.state == TransactionState.PREFLIGHTED and result.requires_confirmation,
          "lane C call should wait for confirmation")
    check("ship it" not in dispatched, "nothing is dispatched before confirmation")
    confirmed, result = orchestrator.confirm(waiting.transaction_id, approver_id="alice@example.com")
    print(f"   confirmed by alice@example.com -> {confirmed.state.value}: {result.reason}")
    print_trail(storage, confirmed)
    check(confirmed.state == TransactionState.COMMITTED, "confirmed call should commit")
    check(confirmed.policy_decision["approver_id"] == "alice@example.com", "approver recorded")

    print("\n4) Audit chains")
    for transaction in (approved, denied, confirmed):
        ok, bad = storage.verify_chain(transaction.transaction_id)
        print(f"   {transaction.transaction_id}: {'intact' if ok else f'BROKEN at {bad}'}")
        check(ok, "every stored chain must verify")

    print("\n5) Provenance certificate for call 1")
    key = generate_signing_key()
    certificate = issue_certificate(approved.transaction_id, storage, key)
    commitment = certificate["credentialSubject"]["cryptographic_commitment"]
    print(f"   issuer       {certificate['issuer']}")
    print(f"   merkle root  {commitment['merkle_root']} ({commitment['leaf_count']} leaves)")
    ok, reason = verify_certificate(certificate, {issuer_did(key)}, storage=storage)
    print(f"   verification {reason}")
    check(ok and reason == "VALID", "certificate should verify")

    print(f"\nDispatched to the tool: {dispatched}")
    check(dispatched == ["hello", "ship it"], "exactly the two approved calls reached the tool")
    print("DEMO OK")


if __name__ == "__main__":
    main()

"""
Shared test environment: a fully wired RUN_TOOL_NODE with tool_echo registered.
"""

from uuid import uuid4

from cfa_shoc.orchestrator import OrchestrationContext, RUN_TOOL_NODE
from cfa_shoc.services import (
    ActionNormalizer, AuditLedger, CapabilitySnapshotService, CommitMembrane,
    PolicyDecisionPoint, PreflightService, ResultVerifier, ToolAdapter,
)
from cfa_shoc.storage import StorageManager


class Env:
    """A fully wired orchestrator with tool_echo registered and a call counter."""

    def __init__(self):
        self.storage = StorageManager("sqlite:///:memory:")
        self.storage.init_db()
        self.capability = CapabilitySnapshotService()
        self.capability.update_tool_state("tool_echo", {"id": "tool_echo", "scopes": []})
        self.adapter = ToolAdapter()
        self.calls = []
        self.adapter.register("tool_echo", self._echo)
        self.audit = AuditLedger()
        self.orchestrator = RUN_TOOL_NODE(
            storage=self.storage,
            capability_service=self.capability,
            normalizer=ActionNormalizer(self.capability),
            preflight=PreflightService(self.capability),
            policy_point=PolicyDecisionPoint(),
            tool_adapter=self.adapter,
            verifier=ResultVerifier(),
            commit_membrane=CommitMembrane(),
            audit_ledger=self.audit,
        )

    def _echo(self, request):
        self.calls.append(request)
        return {"status": "succeeded", "output": {"echo": request.arguments.get("message")}}

    def context(self, **kwargs):
        return OrchestrationContext(
            proposal_id=uuid4(), episode_id=uuid4(), task_node_id=uuid4(),
            principal_id="user_1", tenant_id="tenant_1", requested_by="model", **kwargs,
        )

    def run(self, proposal, context=None):
        return self.orchestrator.execute(proposal, context or self.context())

    def stored(self, transaction):
        session = self.storage.get_session()
        try:
            row = self.storage.get_transaction(session, str(transaction.transaction_id))
            events = self.storage.get_events_for_transaction(session, str(transaction.transaction_id))
            return row, [e.event_type for e in events]
        finally:
            session.close()

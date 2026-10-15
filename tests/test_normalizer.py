"""
Unit tests for ActionNormalizer (handoff step 4: D6, D7).
"""

from uuid import uuid4

from cfa_shoc.domain import RiskClass, RollbackStrategy, SideEffectClass
from cfa_shoc.services import ActionNormalizer, CapabilitySnapshotService


def _normalizer():
    capability = CapabilitySnapshotService()
    capability.update_tool_state("tool_echo", {"id": "tool_echo"})
    return ActionNormalizer(capability)


LEDGER = {"hash": "ledger_hash_1", "contract_hash": "contract_hash_1"}


def test_valid_input_produces_proposal():
    proposal, error = _normalizer().normalize(
        {
            "tool_id": "tool_echo",
            "operation": "echo",
            "arguments": {"message": "hi"},
            "risk_class": "low",
            "side_effect_class": "reversible",
            "rollback_strategy": "compensation",
            "idempotency_key": "k1",
        },
        LEDGER,
    )
    assert error is None
    assert proposal.tool_id == "tool_echo"
    assert proposal.operation == "echo"
    assert proposal.arguments == {"message": "hi"}
    assert proposal.risk_class == RiskClass.LOW
    assert proposal.side_effect_class == SideEffectClass.REVERSIBLE
    assert proposal.rollback_strategy == RollbackStrategy.COMPENSATION
    assert proposal.idempotency_key == "k1"
    assert proposal.ledger_hash == "ledger_hash_1"
    assert proposal.contract_hash == "contract_hash_1"


def test_unregistered_tool_is_refused():
    proposal, error = _normalizer().normalize(
        {"tool_id": "tool_missing", "operation": "op"}, LEDGER
    )
    assert proposal is None
    assert "not registered" in error


def test_bad_risk_class_returns_error_not_exception():
    proposal, error = _normalizer().normalize(
        {"tool_id": "tool_echo", "operation": "echo", "risk_class": "extreme"}, LEDGER
    )
    assert proposal is None
    assert "risk_class" in error


def test_bad_side_effect_class_returns_error_not_exception():
    proposal, error = _normalizer().normalize(
        {"tool_id": "tool_echo", "operation": "echo", "side_effect_class": "explosive"}, LEDGER
    )
    assert proposal is None
    assert "side_effect_class" in error


def test_bad_rollback_strategy_returns_error_not_exception():
    proposal, error = _normalizer().normalize(
        {"tool_id": "tool_echo", "operation": "echo", "rollback_strategy": "pray"}, LEDGER
    )
    assert proposal is None
    assert "rollback_strategy" in error


def test_context_ids_are_preserved():
    proposal_id, episode_id, task_node_id = uuid4(), uuid4(), uuid4()
    proposal, error = _normalizer().normalize(
        {"tool_id": "tool_echo", "operation": "echo"},
        LEDGER,
        proposal_id=proposal_id,
        episode_id=episode_id,
        task_node_id=task_node_id,
        principal_id="user_1",
        tenant_id="tenant_1",
    )
    assert error is None
    assert proposal.proposal_id == proposal_id
    assert proposal.episode_id == episode_id
    assert proposal.task_node_id == task_node_id
    assert proposal.principal_id == "user_1"
    assert proposal.tenant_id == "tenant_1"


def test_ids_generated_when_absent():
    proposal, _ = _normalizer().normalize({"tool_id": "tool_echo", "operation": "echo"}, LEDGER)
    assert proposal.proposal_id is not None
    assert proposal.idempotency_key  # generated


def test_missing_operation_and_non_dict_input():
    normalizer = _normalizer()
    assert normalizer.normalize({"tool_id": "tool_echo"}, LEDGER)[1] == "Missing tool_id or operation"
    assert normalizer.normalize("free-form text", LEDGER)[1] == "Proposal must be a dictionary"
    assert normalizer.normalize(
        {"tool_id": "tool_echo", "operation": "echo", "arguments": []}, LEDGER
    )[1] == "Arguments must be a dictionary"

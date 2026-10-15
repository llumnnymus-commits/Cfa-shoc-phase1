"""
DRxit baton: the governed, signed handoff between legs of a long agent job.

Spec: docs/DRXIT_BATON_SPEC.md. A leg does one verifiable piece of work, then writes a baton
holding what it verified, what it produced, what is unresolved, how it scored, and the
contract for the next leg. The next leg starts only after the baton is sealed (signed and
persisted), validated and acknowledged; only then does the predecessor retire.

Lifecycle: DRAFT (an unsealed dict) -> SEALED -> ACKNOWLEDGED -> RETIRED, or REJECTED when
validation fails (the handoff is refused, never passed on silently).

Ids: a baton's id is ``baton_hash`` = SHA-256 of the canonical JSON of the baton without its
``signature``. A successor's ``parent_baton_hash`` is its parent's id, so batons form a chain.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .audit import canonical_json
from .domain import TransactionState, utcnow
from .state_machine import TransactionStateMachine
from .storage import BatonORM, StorageManager, ToolTransactionORM

BATON_VERSION = "1"

HANDOFF_DECISIONS = {
    "HANDOFF_TO_PLANNER", "HANDOFF_TO_BUILDER", "HANDOFF_TO_EXECUTOR", "HANDOFF_TO_CRITIC",
    "HANDOFF_TO_VERIFIER", "HANDOFF_TO_RECOVERY", "HANDOFF_TO_HUMAN", "COMMIT_AND_CLOSE",
}

TOP_LEVEL_KEYS = {
    "baton_version", "run_id", "leg", "parent_leg", "parent_baton_hash", "global_objective",
    "local_objective", "world_state", "artifacts", "transactions", "evaluation",
    "handoff_decision", "next_leg_contract", "created_at", "signature",
}
WORLD_STATE_KEYS = {"confirmed_facts", "uncertainties", "open_contradictions", "constraints"}
EVALUATION_KEYS = {"reward", "verifier_results", "cost", "latency_ms", "failure_mode"}
CONTRACT_KEYS = {
    "required_capability", "objective", "entry_conditions", "entry_state", "success_condition",
    "completion_criteria", "authority", "rollback_state",
}

# A transaction a baton cites must be done moving forward (COMMITTED counts).
SETTLED_STATES = {state.value for state in TransactionStateMachine.TERMINAL_STATES}


class BatonRejected(ValueError):
    """The baton failed validation; the handoff is refused."""

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class BatonStateError(RuntimeError):
    """An operation was attempted in the wrong lifecycle state."""


class BatonChainError(ValueError):
    """A run's baton chain is broken; `index` is the first bad baton (ordered by leg)."""

    def __init__(self, message: str, index: int):
        super().__init__(f"{message} (index {index})")
        self.index = index


@dataclass
class SealedBaton:
    baton_id: str
    run_id: str
    leg: int
    parent_baton_hash: Optional[str]
    baton: Dict[str, Any]
    state: str
    successor_id: Optional[str] = None
    errors: List[str] = field(default_factory=list)


def baton_hash(baton: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical baton without its signature (the baton id)."""
    unsigned = {k: v for k, v in baton.items() if k != "signature"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def sign(baton: Mapping[str, Any], key: bytes) -> str:
    """HMAC-SHA256 over the canonical baton without its signature."""
    unsigned = {k: v for k, v in baton.items() if k != "signature"}
    return hmac.new(_key(key), canonical_json(unsigned).encode("utf-8"), hashlib.sha256).hexdigest()


def seal(baton: Dict[str, Any], key: bytes, storage: StorageManager) -> SealedBaton:
    """
    Validate structure and evidence, sign, and persist a baton (DRAFT -> SEALED).

    Raises:
        BatonRejected: the baton is malformed, unsupported, contradictory or unsafe;
            nothing is stored
    """
    draft = dict(baton)
    draft.setdefault("created_at", utcnow().isoformat())
    draft["signature"] = ""
    errors = _content_errors(draft, storage)
    if errors:
        raise BatonRejected(errors)
    draft["signature"] = sign(draft, key)
    baton_id = baton_hash(draft)

    session = storage.get_session()
    try:
        if session.get(BatonORM, baton_id) is not None:
            raise BatonRejected([f"baton {baton_id} is already sealed"])
        if session.query(BatonORM).filter(
            BatonORM.run_id == draft["run_id"], BatonORM.leg == draft["leg"]
        ).first() is not None:
            raise BatonRejected([f"run {draft['run_id']} already has a baton for leg {draft['leg']}"])
        record = BatonORM(
            baton_hash=baton_id, run_id=draft["run_id"], leg=draft["leg"],
            parent_baton_hash=draft["parent_baton_hash"], content=draft, state="SEALED",
            validated=0, errors=[], created_at=utcnow(),
        )
        session.add(record)
        session.commit()
        return _to_sealed(record)
    finally:
        session.close()


def validate(
    baton: Dict[str, Any],
    key: bytes,
    storage: StorageManager,
    registry: Mapping[str, Iterable[str]],
    artifact_root: Optional[str] = None,
) -> List[str]:
    """
    The coordinator's checks before acknowledging. Returns [] when the baton is valid.

    `registry` maps each available capability to the authorities it may act with
    (its CFA-SHOC contract scopes). `artifact_root` is the directory artifact paths are
    relative to (default: the current directory).

    The outcome is recorded on the stored baton: a sealed baton that fails is REJECTED
    (fail closed); one that passes may be acknowledged.
    """
    errors = _content_errors(baton, storage)

    # Signature
    signature = baton.get("signature")
    if not isinstance(signature, str) or not signature:
        errors.append("signature is missing")
    elif not hmac.compare_digest(signature, sign(baton, key)):
        errors.append("signature does not verify")

    # Successor eligibility and authority (coordinator checks 3 and 4)
    contract = baton.get("next_leg_contract") if isinstance(baton.get("next_leg_contract"), dict) else {}
    capability = contract.get("required_capability")
    if capability is not None:
        if capability not in registry:
            errors.append(f"required_capability {capability!r} is not in the registry")
        elif contract.get("authority") not in set(registry[capability]):
            errors.append(
                f"authority {contract.get('authority')!r} is not granted to capability {capability!r}"
            )

    # Artifacts must exist and match their sha256 (coordinator check 5)
    root = artifact_root or os.getcwd()
    for index, artifact in enumerate(baton.get("artifacts") or []):
        if not isinstance(artifact, dict) or "path" not in artifact or "sha256" not in artifact:
            continue  # reported by _content_errors
        path = os.path.join(root, artifact["path"])
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            errors.append(f"artifacts[{index}]: {artifact['path']} cannot be read")
            continue
        if digest != artifact["sha256"]:
            errors.append(f"artifacts[{index}]: sha256 mismatch for {artifact['path']}")

    # Persisted? (rule 4) and record the outcome
    baton_id = baton_hash(baton)
    session = storage.get_session()
    try:
        record = session.get(BatonORM, baton_id)
        if record is None:
            errors.append("baton is not sealed and persisted (unknown baton hash)")
        elif record.state == "SEALED":
            if errors:
                record.state = "REJECTED"
                record.validated = 0
                record.errors = list(errors)
            else:
                record.validated = 1
                record.errors = []
            session.commit()
        elif record.state == "REJECTED":
            errors.append("baton was already rejected")
    finally:
        session.close()
    return errors


def acknowledge(baton_id: str, successor_id: str, storage: StorageManager) -> None:
    """
    The successor accepts the handoff (SEALED -> ACKNOWLEDGED). Only a baton whose last
    validate() returned [] can be acknowledged.
    """
    if not successor_id:
        raise ValueError("successor_id is required")
    session = storage.get_session()
    try:
        record = _get(session, baton_id)
        if record.state != "SEALED":
            raise BatonStateError(f"baton {baton_id} is {record.state}, expected SEALED")
        if not record.validated:
            raise BatonStateError(f"baton {baton_id} has not passed validation")
        if baton_hash(record.content) != record.baton_hash:
            raise BatonStateError(f"baton {baton_id} content changed after sealing")
        record.state = "ACKNOWLEDGED"
        record.successor_id = successor_id
        record.acknowledged_at = utcnow()
        session.commit()
    finally:
        session.close()


def retire(baton_id: str, storage: StorageManager) -> None:
    """The predecessor releases its resources (ACKNOWLEDGED -> RETIRED)."""
    session = storage.get_session()
    try:
        record = _get(session, baton_id)
        if record.state != "ACKNOWLEDGED":
            raise BatonStateError(f"baton {baton_id} is {record.state}; retire needs ACKNOWLEDGED")
        record.state = "RETIRED"
        record.retired_at = utcnow()
        session.commit()
    finally:
        session.close()


def chain(run_id: str, storage: StorageManager) -> List[SealedBaton]:
    """
    A run's batons ordered by leg, after checking every link.

    Raises:
        BatonChainError: a stored baton no longer matches its id, or a parent_baton_hash
            does not point at the previous leg's baton
    """
    session = storage.get_session()
    try:
        records = session.query(BatonORM).filter(BatonORM.run_id == run_id).order_by(BatonORM.leg).all()
        sealed = []
        for index, record in enumerate(records):
            if baton_hash(record.content) != record.baton_hash:
                raise BatonChainError("stored baton content does not match its hash", index)
            if index == 0:
                if record.content.get("parent_baton_hash") is not None and \
                        session.get(BatonORM, record.content["parent_baton_hash"]) is None:
                    raise BatonChainError("first baton points at an unknown parent", index)
            else:
                previous = records[index - 1]
                if record.content.get("parent_baton_hash") != previous.baton_hash:
                    raise BatonChainError("parent_baton_hash does not match the previous leg", index)
                if record.leg != previous.leg + 1:
                    raise BatonChainError("legs are not consecutive", index)
            sealed.append(_to_sealed(record))
        return sealed
    finally:
        session.close()


# ------------------------------------------------------------------ internals


def _key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("key must be at least 16 bytes")
    return bytes(key)


def _get(session, baton_id: str) -> BatonORM:
    record = session.get(BatonORM, baton_id)
    if record is None:
        raise BatonStateError(f"unknown baton {baton_id}")
    return record


def _to_sealed(record: BatonORM) -> SealedBaton:
    return SealedBaton(
        baton_id=record.baton_hash, run_id=record.run_id, leg=record.leg,
        parent_baton_hash=record.parent_baton_hash, baton=dict(record.content),
        state=record.state, successor_id=record.successor_id, errors=list(record.errors or []),
    )


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _content_errors(baton: Any, storage: StorageManager) -> List[str]:
    """Structure, evidence, consistency, rollback and transaction checks (no key, no registry)."""
    if not isinstance(baton, dict):
        return ["baton must be a JSON object"]
    errors: List[str] = []

    missing = TOP_LEVEL_KEYS - set(baton)
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
    unknown = set(baton) - TOP_LEVEL_KEYS
    if unknown:
        errors.append(f"unsupported fields (raw reasoning is not inherited): {sorted(unknown)}")
    if errors:
        return errors

    if baton["baton_version"] != BATON_VERSION:
        errors.append(f"baton_version must be {BATON_VERSION!r}")
    for name in ("run_id", "global_objective", "local_objective"):
        if not isinstance(baton[name], str) or not baton[name]:
            errors.append(f"{name} must be a non-empty string")
    if not isinstance(baton["created_at"], str):
        errors.append("created_at must be an ISO-8601 string")
    else:
        try:
            datetime.fromisoformat(baton["created_at"])
        except ValueError:
            errors.append("created_at must be an ISO-8601 string")

    # Legs and parent link
    leg, parent_leg, parent_hash = baton["leg"], baton["parent_leg"], baton["parent_baton_hash"]
    if not isinstance(leg, int) or isinstance(leg, bool) or leg < 1:
        errors.append("leg must be an integer >= 1")
    if (parent_leg is None) != (parent_hash is None):
        errors.append("parent_leg and parent_baton_hash must both be set or both be null")
    elif parent_leg is not None:
        if not isinstance(parent_leg, int) or parent_leg != leg - 1:
            errors.append("parent_leg must be leg - 1")
        session = storage.get_session()
        try:
            parent = session.get(BatonORM, parent_hash)
            if parent is None:
                errors.append(f"parent baton {parent_hash} is not sealed")
            elif parent.run_id != baton["run_id"] or parent.leg != parent_leg:
                errors.append("parent baton belongs to a different run or leg")
            elif parent.state == "REJECTED":
                errors.append("parent baton was rejected")
        finally:
            session.close()

    # World state: every confirmed fact needs evidence (rule 2); no contradictions
    world = baton["world_state"]
    if not isinstance(world, dict):
        errors.append("world_state must be an object")
        world = {}
    else:
        if set(world) - WORLD_STATE_KEYS:
            errors.append(f"world_state has unsupported fields: {sorted(set(world) - WORLD_STATE_KEYS)}")
        for name in ("uncertainties", "open_contradictions", "constraints"):
            if not _is_str_list(world.get(name, [])):
                errors.append(f"world_state.{name} must be a list of strings")
    facts = world.get("confirmed_facts", [])
    fact_texts = set()
    if not isinstance(facts, list):
        errors.append("world_state.confirmed_facts must be a list")
        facts = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict) or not isinstance(fact.get("fact"), str) or not fact["fact"]:
            errors.append(f"confirmed_facts[{index}] must have a non-empty 'fact'")
            continue
        fact_texts.add(fact["fact"])
        refs = fact.get("evidence_refs")
        if not _is_str_list(refs) or not refs:
            errors.append(f"confirmed_facts[{index}] has no evidence_refs (intention without evidence)")
    unresolved = set(world.get("uncertainties", []) or []) | set(world.get("open_contradictions", []) or [])
    for text in sorted(fact_texts & unresolved):
        errors.append(f"contradiction: {text!r} is both confirmed and unresolved")

    # Artifacts and transactions
    artifacts = baton["artifacts"]
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
    else:
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or \
                    not isinstance(artifact.get("sha256"), str) or len(artifact["sha256"]) != 64:
                errors.append(f"artifacts[{index}] needs a 'path' and a 64-hex 'sha256'")
    transactions = baton["transactions"]
    side_effects: List[str] = []
    if not _is_str_list(transactions):
        errors.append("transactions must be a list of transaction ids")
    else:
        session = storage.get_session()
        try:
            for tx_id in transactions:
                row = session.get(ToolTransactionORM, tx_id)
                if row is None:
                    errors.append(f"transaction {tx_id} does not exist")
                elif row.state not in SETTLED_STATES:
                    errors.append(f"transaction {tx_id} is not in a final state ({row.state})")
                else:
                    side_effects.append(row.side_effect_class)
        finally:
            session.close()

    # Evaluation
    evaluation = baton["evaluation"]
    decision = baton["handoff_decision"]
    if decision not in HANDOFF_DECISIONS:
        errors.append(f"unknown handoff_decision {decision!r}")
    if not isinstance(evaluation, dict) or set(evaluation) != EVALUATION_KEYS:
        errors.append(f"evaluation must have exactly {sorted(EVALUATION_KEYS)}")
        evaluation = {}
    else:
        if not _is_number(evaluation["reward"]) or not _is_number(evaluation["cost"]):
            errors.append("evaluation.reward and evaluation.cost must be numbers")
        if not isinstance(evaluation["latency_ms"], int) or isinstance(evaluation["latency_ms"], bool):
            errors.append("evaluation.latency_ms must be an integer")
        if evaluation["failure_mode"] is not None and not isinstance(evaluation["failure_mode"], str):
            errors.append("evaluation.failure_mode must be a string or null")
        results = evaluation["verifier_results"]
        if not isinstance(results, list) or not all(
            isinstance(r, dict) and isinstance(r.get("passed"), bool) for r in results
        ):
            errors.append("evaluation.verifier_results must be a list of {name, passed: bool}")
        else:
            # Coordinator check 1: was the local objective actually completed?
            all_passed = all(r["passed"] for r in results)
            excused = evaluation["failure_mode"] is not None and decision in (
                "HANDOFF_TO_RECOVERY", "HANDOFF_TO_HUMAN"
            )
            if not all_passed and not excused:
                errors.append("verifier_results failed without failure_mode + RECOVERY/HUMAN handoff")
            if decision == "COMMIT_AND_CLOSE" and (not results or not all_passed):
                errors.append("COMMIT_AND_CLOSE needs passing verifier_results (termination is a verification decision)")

    # Next-leg contract
    contract = baton["next_leg_contract"]
    if not isinstance(contract, dict):
        errors.append("next_leg_contract must be an object")
        return errors
    if set(contract) != CONTRACT_KEYS:
        if "rollback_state" not in contract:
            errors.append("next_leg_contract.rollback_state is missing")
        others = (CONTRACT_KEYS - set(contract)) - {"rollback_state"}
        if others:
            errors.append(f"next_leg_contract missing fields: {sorted(others)}")
        if set(contract) - CONTRACT_KEYS:
            errors.append(f"next_leg_contract has unsupported fields: {sorted(set(contract) - CONTRACT_KEYS)}")
        return errors
    for name in ("entry_conditions", "entry_state", "success_condition", "completion_criteria"):
        if not isinstance(contract[name], list):
            errors.append(f"next_leg_contract.{name} must be a list")
    closing = decision == "COMMIT_AND_CLOSE"
    if contract["required_capability"] is None and not closing:
        errors.append("next_leg_contract.required_capability is required unless COMMIT_AND_CLOSE")
    if contract["rollback_state"] is None and not closing:
        if any(effect != "none" for effect in side_effects):
            errors.append("rollback_state is null but a listed transaction has side effects")
    # Coordinator check 2: what is unsatisfied must be carried forward
    if closing:
        if world.get("uncertainties") or world.get("open_contradictions"):
            errors.append("COMMIT_AND_CLOSE with open uncertainties or contradictions")
    elif not contract.get("completion_criteria") and not world.get("uncertainties"):
        errors.append("handoff carries no completion_criteria or uncertainties for the next leg")
    return errors

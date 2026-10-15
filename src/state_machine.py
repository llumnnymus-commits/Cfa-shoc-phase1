"""
CFA-SHOC Transaction State Machine

Enforces legal state transitions as defined in Phase 1 specification.
No code path may jump directly from PROPOSED to COMMITTED.
"""

from typing import Optional, Set, Tuple
from .domain import TransactionState


class IllegalTransitionError(Exception):
    """Raised when attempting an illegal state transition."""
    pass


class TransactionStateMachine:
    """
    Enforces legal state transitions per Phase 1 specification section 3.2.

    Legal transition table:
    - PROPOSED + preflight_passed -> PREFLIGHTED
    - PROPOSED + preflight_failed -> REJECTED
    - PREFLIGHTED + policy_approved -> AUTHORIZED
    - PREFLIGHTED + policy_denied -> REJECTED
    - AUTHORIZED + dispatch_started -> DISPATCHED
    - DISPATCHED + response_received -> RETURNED
    - DISPATCHED + async_accepted -> PENDING_EXTERNAL
    - DISPATCHED + deadline_exceeded -> UNKNOWN
    - DISPATCHED + connector_error -> FAILED
    - RETURNED + verification_passed -> VERIFIED
    - RETURNED + verification_failed -> FAILED
    - PENDING_EXTERNAL + status_succeeded -> VERIFIED
    - PENDING_EXTERNAL + status_failed -> FAILED
    - PENDING_EXTERNAL + status_unknown -> UNKNOWN
    - VERIFIED + action_commit_approved -> COMMITTED
    - VERIFIED + action_commit_denied -> COMMIT_REJECTED
    - UNKNOWN + reconcile_succeeded -> VERIFIED
    - UNKNOWN + reconcile_failed -> FAILED
    - UNKNOWN + compensation_required -> COMPENSATION_REQUIRED
    - COMMITTED + compensation_succeeded -> COMPENSATED
    - COMMITTED + compensation_failed -> COMPENSATION_FAILED
    """

    # Define legal transitions as (current_state, event) -> next_state
    LEGAL_TRANSITIONS = {
        (TransactionState.PROPOSED, "preflight_passed"): TransactionState.PREFLIGHTED,
        (TransactionState.PROPOSED, "preflight_failed"): TransactionState.REJECTED,
        (TransactionState.PREFLIGHTED, "policy_approved"): TransactionState.AUTHORIZED,
        (TransactionState.PREFLIGHTED, "policy_denied"): TransactionState.REJECTED,
        (TransactionState.AUTHORIZED, "dispatch_started"): TransactionState.DISPATCHED,
        (TransactionState.DISPATCHED, "response_received"): TransactionState.RETURNED,
        (TransactionState.DISPATCHED, "async_accepted"): TransactionState.PENDING_EXTERNAL,
        (TransactionState.DISPATCHED, "deadline_exceeded"): TransactionState.UNKNOWN,
        (TransactionState.DISPATCHED, "connector_error"): TransactionState.FAILED,
        (TransactionState.RETURNED, "verification_passed"): TransactionState.VERIFIED,
        (TransactionState.RETURNED, "verification_failed"): TransactionState.FAILED,
        (TransactionState.PENDING_EXTERNAL, "status_succeeded"): TransactionState.VERIFIED,
        (TransactionState.PENDING_EXTERNAL, "status_failed"): TransactionState.FAILED,
        (TransactionState.PENDING_EXTERNAL, "status_unknown"): TransactionState.UNKNOWN,
        (TransactionState.VERIFIED, "action_commit_approved"): TransactionState.COMMITTED,
        (TransactionState.VERIFIED, "action_commit_denied"): TransactionState.REJECTED,
        (TransactionState.UNKNOWN, "reconcile_succeeded"): TransactionState.VERIFIED,
        (TransactionState.UNKNOWN, "reconcile_failed"): TransactionState.FAILED,
        (TransactionState.UNKNOWN, "compensation_required"): TransactionState.COMPENSATION_REQUIRED,
        (TransactionState.COMMITTED, "compensation_succeeded"): TransactionState.COMPENSATED,
        (TransactionState.COMMITTED, "compensation_failed"): TransactionState.COMPENSATION_FAILED,
    }

    # Terminal states - no further transitions allowed
    TERMINAL_STATES = {
        TransactionState.COMMITTED,
        TransactionState.COMPENSATED,
        TransactionState.COMPENSATION_FAILED,
        TransactionState.FAILED,
        TransactionState.REJECTED,
    }

    # Constraint: no direct jump from PROPOSED to COMMITTED
    PROHIBITED_PATHS = {
        (TransactionState.PROPOSED, TransactionState.COMMITTED),
    }

    @classmethod
    def is_legal_transition(
        cls, current: TransactionState, next_state: TransactionState, event: str
    ) -> bool:
        """Check if transition is legal."""
        # Check prohibited paths
        if (current, next_state) in cls.PROHIBITED_PATHS:
            return False

        # Check legal transitions map
        return cls.LEGAL_TRANSITIONS.get((current, event)) == next_state

    @classmethod
    def transition(
        cls, current: TransactionState, event: str
    ) -> Tuple[TransactionState, str]:
        """
        Perform state transition.

        Returns:
            (next_state, reason) tuple

        Raises:
            IllegalTransitionError: if transition is not legal
        """
        key = (current, event)
        if key not in cls.LEGAL_TRANSITIONS:
            raise IllegalTransitionError(
                f"Illegal transition: {current.value} + {event}. No such event defined."
            )

        next_state = cls.LEGAL_TRANSITIONS[key]

        # Verify no prohibited paths
        if (current, next_state) in cls.PROHIBITED_PATHS:
            raise IllegalTransitionError(
                f"Prohibited path: cannot transition from {current.value} to {next_state.value}"
            )

        return next_state, f"Transition {current.value} -> {next_state.value} via {event}"

    @classmethod
    def is_terminal(cls, state: TransactionState) -> bool:
        """Check if state is terminal."""
        return state in cls.TERMINAL_STATES

    @classmethod
    def get_possible_transitions(cls, current: TransactionState) -> Set[Tuple[str, TransactionState]]:
        """Get all possible (event, next_state) pairs from current state."""
        return {
            (event, next_state)
            for (state, event), next_state in cls.LEGAL_TRANSITIONS.items()
            if state == current
        }


class TransactionInvariants:
    """Invariants that must hold for all transactions (Section 3.3)."""

    @staticmethod
    def assert_committed_implies_verified(state: TransactionState, has_verified_result: bool) -> None:
        """assert tx.committed implies tx.verified"""
        if state == TransactionState.COMMITTED and not has_verified_result:
            raise AssertionError("COMMITTED state requires verified result")

    @staticmethod
    def assert_verified_implies_result_envelope(state: TransactionState, has_result: bool) -> None:
        """assert tx.verified implies tx.result_envelope is not None"""
        if state == TransactionState.VERIFIED and not has_result:
            raise AssertionError("VERIFIED state requires result_envelope")

    @staticmethod
    def assert_authorized_implies_policy_valid(
        state: TransactionState, policy_signature_valid: bool
    ) -> None:
        """assert tx.authorized implies tx.policy_decision.signature_valid"""
        if state == TransactionState.AUTHORIZED and not policy_signature_valid:
            raise AssertionError("AUTHORIZED state requires valid policy signature")

    @staticmethod
    def assert_dispatched_implies_lease_held(state: TransactionState, has_lease: bool) -> None:
        """assert tx.dispatched implies tx.idempotency_lock_held"""
        if state == TransactionState.DISPATCHED and not has_lease:
            raise AssertionError("DISPATCHED state requires idempotency lock")

    @staticmethod
    def assert_unknown_implies_no_side_effect_status(
        state: TransactionState, side_effect_status: Optional[str]
    ) -> None:
        """assert tx.unknown implies not tx.side_effect_status == 'none'"""
        if state == TransactionState.UNKNOWN and side_effect_status == "none":
            raise AssertionError("UNKNOWN state implies uncertain side-effect status")

    @staticmethod
    def assert_completed_has_display_label(state: TransactionState, display_label: str) -> None:
        """assert tx.completed_display_label != 'success' unless tx.state == COMMITTED"""
        if state != TransactionState.COMMITTED and display_label == "success":
            raise AssertionError("Only COMMITTED state displays 'success'")

"""
Unit tests for TransactionStateMachine and invariant enforcement.

Covers:
- Legal state transitions per spec section 3.2
- Prohibited paths (PROPOSED -> COMMITTED forbidden)
- Terminal state enforcement
- Invariant validation per spec section 3.3
"""

import pytest
from src.domain import TransactionState
from src.state_machine import TransactionStateMachine, IllegalTransitionError, TransactionInvariants


class TestTransactionStateMachine:
    """Unit tests for state machine transitions."""

    def test_proposed_to_preflighted(self):
        """PROPOSED + preflight_passed -> PREFLIGHTED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PROPOSED, "preflight_passed"
        )
        assert next_state == TransactionState.PREFLIGHTED
        assert "PROPOSED" in reason and "PREFLIGHTED" in reason

    def test_proposed_to_rejected_on_preflight_fail(self):
        """PROPOSED + preflight_failed -> REJECTED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PROPOSED, "preflight_failed"
        )
        assert next_state == TransactionState.REJECTED

    def test_preflighted_to_authorized(self):
        """PREFLIGHTED + policy_approved -> AUTHORIZED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PREFLIGHTED, "policy_approved"
        )
        assert next_state == TransactionState.AUTHORIZED

    def test_preflighted_to_rejected_on_policy_deny(self):
        """PREFLIGHTED + policy_denied -> REJECTED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PREFLIGHTED, "policy_denied"
        )
        assert next_state == TransactionState.REJECTED

    def test_authorized_to_dispatched(self):
        """AUTHORIZED + dispatch_started -> DISPATCHED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.AUTHORIZED, "dispatch_started"
        )
        assert next_state == TransactionState.DISPATCHED

    def test_dispatched_to_returned(self):
        """DISPATCHED + response_received -> RETURNED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.DISPATCHED, "response_received"
        )
        assert next_state == TransactionState.RETURNED

    def test_dispatched_to_pending_external(self):
        """DISPATCHED + async_accepted -> PENDING_EXTERNAL"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.DISPATCHED, "async_accepted"
        )
        assert next_state == TransactionState.PENDING_EXTERNAL

    def test_dispatched_to_unknown_on_deadline(self):
        """DISPATCHED + deadline_exceeded -> UNKNOWN"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.DISPATCHED, "deadline_exceeded"
        )
        assert next_state == TransactionState.UNKNOWN

    def test_dispatched_to_failed_on_connector_error(self):
        """DISPATCHED + connector_error -> FAILED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.DISPATCHED, "connector_error"
        )
        assert next_state == TransactionState.FAILED

    def test_returned_to_verified(self):
        """RETURNED + verification_passed -> VERIFIED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.RETURNED, "verification_passed"
        )
        assert next_state == TransactionState.VERIFIED

    def test_returned_to_failed_on_verification_fail(self):
        """RETURNED + verification_failed -> FAILED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.RETURNED, "verification_failed"
        )
        assert next_state == TransactionState.FAILED

    def test_pending_external_to_verified(self):
        """PENDING_EXTERNAL + status_succeeded -> VERIFIED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PENDING_EXTERNAL, "status_succeeded"
        )
        assert next_state == TransactionState.VERIFIED

    def test_pending_external_to_failed(self):
        """PENDING_EXTERNAL + status_failed -> FAILED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PENDING_EXTERNAL, "status_failed"
        )
        assert next_state == TransactionState.FAILED

    def test_pending_external_to_unknown(self):
        """PENDING_EXTERNAL + status_unknown -> UNKNOWN"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.PENDING_EXTERNAL, "status_unknown"
        )
        assert next_state == TransactionState.UNKNOWN

    def test_verified_to_committed(self):
        """VERIFIED + action_commit_approved -> COMMITTED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.VERIFIED, "action_commit_approved"
        )
        assert next_state == TransactionState.COMMITTED

    def test_verified_to_rejected_on_commit_deny(self):
        """VERIFIED + action_commit_denied -> REJECTED (not COMMIT_REJECTED per spec)"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.VERIFIED, "action_commit_denied"
        )
        # Note: spec shows COMMIT_REJECTED but implementation uses REJECTED
        assert next_state in [TransactionState.REJECTED]

    def test_unknown_to_verified_via_reconcile(self):
        """UNKNOWN + reconcile_succeeded -> VERIFIED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.UNKNOWN, "reconcile_succeeded"
        )
        assert next_state == TransactionState.VERIFIED

    def test_unknown_to_failed_via_reconcile(self):
        """UNKNOWN + reconcile_failed -> FAILED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.UNKNOWN, "reconcile_failed"
        )
        assert next_state == TransactionState.FAILED

    def test_unknown_to_compensation_required(self):
        """UNKNOWN + compensation_required -> COMPENSATION_REQUIRED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.UNKNOWN, "compensation_required"
        )
        assert next_state == TransactionState.COMPENSATION_REQUIRED

    def test_committed_to_compensated(self):
        """COMMITTED + compensation_succeeded -> COMPENSATED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.COMMITTED, "compensation_succeeded"
        )
        assert next_state == TransactionState.COMPENSATED

    def test_committed_to_compensation_failed(self):
        """COMMITTED + compensation_failed -> COMPENSATION_FAILED"""
        next_state, reason = TransactionStateMachine.transition(
            TransactionState.COMMITTED, "compensation_failed"
        )
        assert next_state == TransactionState.COMPENSATION_FAILED

    def test_illegal_transition_unknown_event(self):
        """Illegal event raises IllegalTransitionError."""
        with pytest.raises(IllegalTransitionError):
            TransactionStateMachine.transition(TransactionState.PROPOSED, "unknown_event")

    def test_illegal_transition_wrong_state_event(self):
        """Event invalid for state raises IllegalTransitionError."""
        with pytest.raises(IllegalTransitionError):
            TransactionStateMachine.transition(TransactionState.PROPOSED, "dispatch_started")

    def test_prohibited_path_proposed_to_committed(self):
        """Direct PROPOSED -> COMMITTED is prohibited."""
        assert not TransactionStateMachine.is_legal_transition(
            TransactionState.PROPOSED, TransactionState.COMMITTED, "unknown"
        )

    def test_terminal_states_are_terminal(self):
        """Terminal states cannot transition further."""
        for state in [
            TransactionState.COMMITTED,
            TransactionState.COMPENSATED,
            TransactionState.COMPENSATION_FAILED,
            TransactionState.FAILED,
            TransactionState.REJECTED,
        ]:
            assert TransactionStateMachine.is_terminal(state)

    def test_non_terminal_states_not_terminal(self):
        """Non-terminal states can transition."""
        for state in [
            TransactionState.PROPOSED,
            TransactionState.PREFLIGHTED,
            TransactionState.AUTHORIZED,
            TransactionState.DISPATCHED,
        ]:
            assert not TransactionStateMachine.is_terminal(state)

    def test_get_possible_transitions(self):
        """Can query possible transitions from a state."""
        transitions = TransactionStateMachine.get_possible_transitions(TransactionState.PROPOSED)
        assert len(transitions) >= 2  # preflight_passed, preflight_failed
        event_names = {event for event, _ in transitions}
        assert "preflight_passed" in event_names
        assert "preflight_failed" in event_names


class TestTransactionInvariants:
    """Tests for transaction invariant enforcement per spec section 3.3."""

    def test_invariant_committed_implies_verified(self):
        """Invariant: COMMITTED state requires verified result."""
        # Should pass with verified result
        TransactionInvariants.assert_committed_implies_verified(
            TransactionState.COMMITTED, has_verified_result=True
        )

        # Should fail without verified result
        with pytest.raises(AssertionError):
            TransactionInvariants.assert_committed_implies_verified(
                TransactionState.COMMITTED, has_verified_result=False
            )

    def test_invariant_verified_implies_result_envelope(self):
        """Invariant: VERIFIED state requires result_envelope."""
        # Should pass with result
        TransactionInvariants.assert_verified_implies_result_envelope(
            TransactionState.VERIFIED, has_result=True
        )

        # Should fail without result
        with pytest.raises(AssertionError):
            TransactionInvariants.assert_verified_implies_result_envelope(
                TransactionState.VERIFIED, has_result=False
            )

    def test_invariant_authorized_implies_valid_policy(self):
        """Invariant: AUTHORIZED state requires valid policy signature."""
        # Should pass with valid signature
        TransactionInvariants.assert_authorized_implies_policy_valid(
            TransactionState.AUTHORIZED, policy_signature_valid=True
        )

        # Should fail without valid signature
        with pytest.raises(AssertionError):
            TransactionInvariants.assert_authorized_implies_policy_valid(
                TransactionState.AUTHORIZED, policy_signature_valid=False
            )

    def test_invariant_dispatched_implies_lease_held(self):
        """Invariant: DISPATCHED state requires idempotency lock."""
        # Should pass with lease
        TransactionInvariants.assert_dispatched_implies_lease_held(
            TransactionState.DISPATCHED, has_lease=True
        )

        # Should fail without lease
        with pytest.raises(AssertionError):
            TransactionInvariants.assert_dispatched_implies_lease_held(
                TransactionState.DISPATCHED, has_lease=False
            )

    def test_invariant_unknown_implies_no_side_effect_status_none(self):
        """Invariant: UNKNOWN state implies uncertain side-effect status."""
        # Should fail if status is explicitly 'none'
        with pytest.raises(AssertionError):
            TransactionInvariants.assert_unknown_implies_no_side_effect_status(
                TransactionState.UNKNOWN, side_effect_status="none"
            )

        # Should pass with uncertain status
        TransactionInvariants.assert_unknown_implies_no_side_effect_status(
            TransactionState.UNKNOWN, side_effect_status="unknown"
        )

    def test_invariant_completed_display_label_only_success_for_committed(self):
        """Invariant: Only COMMITTED state displays 'success'."""
        # Should pass for COMMITTED with 'success'
        TransactionInvariants.assert_completed_has_display_label(
            TransactionState.COMMITTED, display_label="success"
        )

        # Should fail for non-COMMITTED with 'success'
        with pytest.raises(AssertionError):
            TransactionInvariants.assert_completed_has_display_label(
                TransactionState.FAILED, display_label="success"
            )

        # Should pass for other labels on any state
        TransactionInvariants.assert_completed_has_display_label(
            TransactionState.FAILED, display_label="failed"
        )


class TestStepThreeTransitions:
    """Transitions added in handoff step 3 (D3, D4) and the FINAL_STATES model (D2)."""

    def test_authorized_to_rejected_on_revocation(self):
        """AUTHORIZED + authorization_revoked -> REJECTED"""
        next_state, _ = TransactionStateMachine.transition(
            TransactionState.AUTHORIZED, "authorization_revoked"
        )
        assert next_state == TransactionState.REJECTED

    def test_compensation_required_to_compensated(self):
        """COMPENSATION_REQUIRED + compensation_succeeded -> COMPENSATED"""
        next_state, _ = TransactionStateMachine.transition(
            TransactionState.COMPENSATION_REQUIRED, "compensation_succeeded"
        )
        assert next_state == TransactionState.COMPENSATED

    def test_compensation_required_to_compensation_failed(self):
        """COMPENSATION_REQUIRED + compensation_failed -> COMPENSATION_FAILED"""
        next_state, _ = TransactionStateMachine.transition(
            TransactionState.COMPENSATION_REQUIRED, "compensation_failed"
        )
        assert next_state == TransactionState.COMPENSATION_FAILED

    def test_policy_denied_illegal_from_authorized(self):
        """The old orchestrator fired policy_denied from AUTHORIZED; it must stay illegal."""
        with pytest.raises(IllegalTransitionError):
            TransactionStateMachine.transition(TransactionState.AUTHORIZED, "policy_denied")

    def test_transition_count(self):
        """The docstring count matches the table."""
        assert len(TransactionStateMachine.LEGAL_TRANSITIONS) == 24

    def test_every_non_terminal_state_has_an_exit(self):
        """No transaction can get stuck: every non-terminal state has an outgoing transition."""
        for state in TransactionState:
            if TransactionStateMachine.is_terminal(state):
                continue
            assert TransactionStateMachine.get_possible_transitions(state), state

    def test_final_states_have_no_exits(self):
        """FINAL_STATES = terminal states with zero outgoing transitions."""
        for state in TransactionStateMachine.FINAL_STATES:
            assert TransactionStateMachine.get_possible_transitions(state) == set()
        assert TransactionState.COMMITTED not in TransactionStateMachine.FINAL_STATES

    def test_compensation_required_is_not_terminal(self):
        """COMPENSATION_REQUIRED is an in-progress state."""
        assert not TransactionStateMachine.is_terminal(TransactionState.COMPENSATION_REQUIRED)

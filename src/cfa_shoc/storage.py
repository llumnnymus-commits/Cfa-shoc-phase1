"""
CFA-SHOC Storage Layer

PostgreSQL schema and ORM models for transaction truth and audit ledger.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Integer, String, Text, TypeDecorator, create_engine, Index
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .domain import utcnow

Base = declarative_base()


class UTCDateTime(TypeDecorator):
    """
    Stores datetimes as naive UTC and returns them as timezone-aware UTC.

    Naive values passed in are taken to be UTC already. This keeps SQLite (which drops
    timezone information) and PostgreSQL consistent: store UTC, compare UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class LeaseConflictError(Exception):
    """Raised when an unexpired dispatch lease already exists for the transaction."""


class ToolTransactionORM(Base):
    """ORM model for tool_transactions table."""
    __tablename__ = "tool_transactions"

    transaction_id = Column(String(36), primary_key=True)
    proposal_id = Column(String(36), nullable=False)
    episode_id = Column(String(36), nullable=False)
    task_node_id = Column(String(36), nullable=False)
    tool_id = Column(String(255), nullable=False)
    operation = Column(String(255), nullable=False)
    connector_id = Column(String(255))
    principal_id = Column(String(255), nullable=False)
    tenant_id = Column(String(255), nullable=False)
    ledger_hash = Column(String(256), nullable=False)
    contract_hash = Column(String(256), nullable=False)
    input_payload = Column(JSON, nullable=False)
    input_schema_hash = Column(String(256), nullable=False)
    idempotency_key = Column(String(256), nullable=False)
    risk_class = Column(String(50), nullable=False)
    side_effect_class = Column(String(50), nullable=False)
    rollback_strategy = Column(String(50), nullable=False)
    state = Column(String(50), nullable=False, default="PROPOSED")
    external_operation_id = Column(String(255))
    result_envelope = Column(JSON)
    policy_decision = Column(JSON)
    verification = Column(JSON)
    created_at = Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = Column(UTCDateTime, nullable=False, default=utcnow)
    deadline_at = Column(UTCDateTime)
    version = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_transaction_state", "state"),
        Index("ix_transaction_idempotency", "tenant_id", "idempotency_key"),
        Index("ix_transaction_episode", "episode_id"),
        Index("ix_transaction_external_op", "external_operation_id"),
    )


class TransactionEventORM(Base):
    """ORM model for transaction_events append-only audit table."""
    __tablename__ = "transaction_events"

    event_id = Column(String(36), primary_key=True)
    transaction_id = Column(String(36), ForeignKey("tool_transactions.transaction_id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    from_state = Column(String(50))
    to_state = Column(String(50))
    actor_type = Column(String(50), nullable=False)
    actor_id = Column(String(255), nullable=False)
    payload = Column(JSON, nullable=False)
    prior_hash = Column(String(256))
    event_hash = Column(String(256), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_event_transaction", "transaction_id"),
        Index("ix_event_type", "event_type"),
        Index("ix_event_created", "created_at"),
    )


class CommitDecisionORM(Base):
    """ORM model for commit_decisions table."""
    __tablename__ = "commit_decisions"

    decision_id = Column(String(36), primary_key=True)
    transaction_id = Column(String(36), ForeignKey("tool_transactions.transaction_id"), nullable=False)
    subject_type = Column(String(100), nullable=False)
    subject_ref = Column(String(255), nullable=False)
    contract_hash = Column(String(256), nullable=False)
    ledger_hash = Column(String(256), nullable=False)
    evidence_refs = Column(JSON, nullable=False)
    checks = Column(JSON, nullable=False)
    decision = Column(String(50), nullable=False)  # APPROVE, DENY
    signature = Column(Text, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    approver_type = Column(String(50), nullable=False)
    approver_id = Column(String(255), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_decision_transaction", "transaction_id"),
        Index("ix_decision_expires", "expires_at"),
    )


class DispatchLeaseORM(Base):
    """ORM model for short-lived dispatch leases."""
    __tablename__ = "dispatch_leases"

    lease_id = Column(String(36), primary_key=True)
    transaction_id = Column(String(36), ForeignKey("tool_transactions.transaction_id"), nullable=False)
    owner_id = Column(String(255), nullable=False)
    lease_token = Column(String(36), nullable=False, unique=True)
    acquired_at = Column(UTCDateTime, nullable=False, default=utcnow)
    expires_at = Column(UTCDateTime, nullable=False)

    __table_args__ = (
        Index("ix_lease_transaction", "transaction_id"),
        Index("ix_lease_expires", "expires_at"),
    )


class PolicyDecisionORM(Base):
    """ORM model for policy decisions cache."""
    __tablename__ = "policy_decisions"

    decision_id = Column(String(36), primary_key=True)
    approved = Column(Integer, nullable=False)  # boolean as int
    reason = Column(Text, nullable=False)
    subject_type = Column(String(100), nullable=False)
    subject_ref = Column(String(255), nullable=False)
    contract_hash = Column(String(256), nullable=False)
    ledger_hash = Column(String(256), nullable=False)
    evidence_refs = Column(JSON, nullable=False)
    checks = Column(JSON, nullable=False)
    signature = Column(Text, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    approver_type = Column(String(50), nullable=False)
    approver_id = Column(String(255), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_policy_subject", "subject_type", "subject_ref"),
        Index("ix_policy_expires", "expires_at"),
    )


class StorageManager:
    """Manages database connections and operations."""

    def __init__(self, database_url: str = "sqlite:///:memory:"):
        """Initialize storage manager."""
        self.database_url = database_url
        self.engine = create_engine(database_url)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def init_db(self) -> None:
        """Initialize all tables."""
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Session:
        """Get a new database session."""
        return self.SessionLocal()

    def save_transaction(self, session: Session, tx_data: Dict[str, Any]) -> ToolTransactionORM:
        """Save or update a tool transaction."""
        existing = self._upsert_transaction(session, tx_data)
        session.commit()
        return existing

    def _upsert_transaction(self, session: Session, tx_data: Dict[str, Any]) -> ToolTransactionORM:
        """Insert or update a transaction row without committing."""
        existing = session.query(ToolTransactionORM).filter(
            ToolTransactionORM.transaction_id == tx_data["transaction_id"]
        ).first()

        if existing:
            for key, value in tx_data.items():
                if key not in ("transaction_id", "created_at"):
                    setattr(existing, key, value)
            existing.updated_at = utcnow()
            existing.version += 1
        else:
            existing = ToolTransactionORM(**tx_data)
            session.add(existing)
        session.flush()
        return existing

    def record_transition(
        self, session: Session, tx_data: Dict[str, Any], event_data: Dict[str, Any]
    ) -> TransactionEventORM:
        """
        Persist the transaction's new state and append its audit event in ONE database
        transaction. Either both are stored or neither is.
        """
        try:
            self._upsert_transaction(session, tx_data)
            event = TransactionEventORM(**event_data)
            session.add(event)
            session.commit()
            return event
        except Exception:
            session.rollback()
            raise

    def find_by_idempotency_key(
        self, session: Session, tenant_id: str, idempotency_key: str, states: Optional[list] = None
    ) -> list[ToolTransactionORM]:
        """Transactions of a tenant with this idempotency key (optionally in given states)."""
        query = session.query(ToolTransactionORM).filter(
            ToolTransactionORM.tenant_id == tenant_id,
            ToolTransactionORM.idempotency_key == idempotency_key,
        )
        if states is not None:
            query = query.filter(ToolTransactionORM.state.in_(list(states)))
        return query.order_by(ToolTransactionORM.created_at).all()

    def find_in_flight(
        self, session: Session, tenant_id: str, tool_id: str, states: list
    ) -> list[ToolTransactionORM]:
        """Transactions of a tenant on a tool that are in one of the given states."""
        return session.query(ToolTransactionORM).filter(
            ToolTransactionORM.tenant_id == tenant_id,
            ToolTransactionORM.tool_id == tool_id,
            ToolTransactionORM.state.in_(list(states)),
        ).all()

    def get_transaction(self, session: Session, transaction_id: str) -> Optional[ToolTransactionORM]:
        """Retrieve a transaction by ID."""
        return session.query(ToolTransactionORM).filter(
            ToolTransactionORM.transaction_id == transaction_id
        ).first()

    def append_event(self, session: Session, event_data: Dict[str, Any]) -> TransactionEventORM:
        """Append an audit event (append-only)."""
        event = TransactionEventORM(**event_data)
        session.add(event)
        session.commit()
        return event

    def get_events_for_transaction(
        self, session: Session, transaction_id: str
    ) -> list[TransactionEventORM]:
        """Get all events for a transaction."""
        return session.query(TransactionEventORM).filter(
            TransactionEventORM.transaction_id == transaction_id
        ).order_by(TransactionEventORM.created_at).all()

    def save_commit_decision(self, session: Session, decision_data: Dict[str, Any]) -> CommitDecisionORM:
        """Save a commit decision."""
        decision = CommitDecisionORM(**decision_data)
        session.add(decision)
        session.commit()
        return decision

    def acquire_lease(self, session: Session, lease_data: Dict[str, Any]) -> DispatchLeaseORM:
        """
        Acquire a dispatch lease.

        Raises:
            LeaseConflictError: an unexpired lease for the same transaction exists
        """
        now = utcnow()
        held = session.query(DispatchLeaseORM).filter(
            DispatchLeaseORM.transaction_id == lease_data["transaction_id"],
            DispatchLeaseORM.expires_at > now,
        ).first()
        if held is not None:
            raise LeaseConflictError(
                f"Transaction {lease_data['transaction_id']} already leased by {held.owner_id}"
            )
        lease = DispatchLeaseORM(**lease_data)
        session.add(lease)
        session.commit()
        return lease

    def release_lease(self, session: Session, lease_token: str) -> bool:
        """Release a dispatch lease."""
        lease = session.query(DispatchLeaseORM).filter(
            DispatchLeaseORM.lease_token == lease_token
        ).first()
        if lease:
            session.delete(lease)
            session.commit()
            return True
        return False

    def find_reconcilable_transactions(
        self, session: Session, limit: int = 100
    ) -> list[ToolTransactionORM]:
        """Find UNKNOWN, PENDING_EXTERNAL, and expired-lease transactions."""
        from sqlalchemy import or_
        return session.query(ToolTransactionORM).filter(
            or_(
                ToolTransactionORM.state == "UNKNOWN",
                ToolTransactionORM.state == "PENDING_EXTERNAL",
            )
        ).limit(limit).all()

    def close(self) -> None:
        """Close database connections."""
        self.engine.dispose()

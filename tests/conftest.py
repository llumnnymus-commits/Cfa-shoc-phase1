"""
Pytest fixtures and configuration for CFA-SHOC Phase 1 tests.
"""

import pytest
from src.storage import StorageManager


@pytest.fixture(scope="session")
def test_db_file(tmp_path_factory):
    """Create a temporary database file for session-scoped tests."""
    fn = tmp_path_factory.mktemp("data") / "test.db"
    return fn


@pytest.fixture
def in_memory_db():
    """In-memory SQLite database for unit tests."""
    storage = StorageManager("sqlite:///:memory:")
    storage.init_db()
    return storage


@pytest.fixture
def file_db(tmp_path):
    """File-based SQLite database for integration tests."""
    db_path = tmp_path / "test.db"
    storage = StorageManager(f"sqlite:///{db_path}")
    storage.init_db()
    return storage

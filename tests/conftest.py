import pytest

from interleave.clock import FakeClock
from interleave.models import Request
from interleave.store import OperationStore, connect


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "test.db"))


@pytest.fixture
def store(conn, clock):
    return OperationStore(conn, clock)


@pytest.fixture
def base_request():
    return Request(
        principal="agent-a",
        tool="open_pr",
        repo="demo/repo",
        branch="fix-bug",
        title="Fix bug in payments module",
        body="Details of the fix.",
    )

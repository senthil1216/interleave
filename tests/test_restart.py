from interleave.adapter import FakeDownstreamAdapter
from interleave.clock import RealClock
from interleave.gateway import Gateway
from interleave.models import State
from interleave.policy import AllowAll
from interleave.reconcile import reconcile
from interleave.store import OperationStore, connect


def test_approval_survives_restart(tmp_path, base_request):
    db_path = str(tmp_path / "restart.db")
    conn = connect(db_path)
    store = OperationStore(conn, RealClock())
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, RealClock(), AllowAll(), downstream)
    op = gateway.submit(base_request)
    gateway.approve(op.id)
    conn.close()  # no teardown -- this is the "crash"

    conn2 = connect(db_path)
    store2 = OperationStore(conn2, RealClock())
    reconcile(store2)
    op2 = store2.get_by_id(op.id)
    assert op2.state == State.APPROVED

    gateway2 = Gateway(store2, RealClock(), AllowAll(), FakeDownstreamAdapter(store2, fault="none"))
    op3 = gateway2.run_to_completion(op.id)
    assert op3.state == State.SUCCEEDED


def test_reconcile_adopts_orphaned_effects(tmp_path, base_request):
    db_path = str(tmp_path / "crash.db")
    conn = connect(db_path)
    store = OperationStore(conn, RealClock())
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, RealClock(), AllowAll(), downstream)
    op = gateway.submit(base_request)
    gateway.approve(op.id)

    # Simulate a crash *after* the effect applied but before the attempt was marked
    # finished: same steps revalidate_and_start_attempt + downstream.call take, minus
    # the bookkeeping that would normally follow. attempts.outcome is left NULL with a
    # ledger row already present -- a genuinely orphaned intent record.
    attempt_no = store.next_attempt_no(op.id)
    store.cas(op.id, (State.APPROVED,), State.EXECUTING)
    store.start_attempt(op.id, attempt_no, op.normalized_intent)
    downstream.call(op.principal, op.tool, op.repo, op.branch, op.normalized_intent)
    conn.close()

    conn2 = connect(db_path)
    store2 = OperationStore(conn2, RealClock())
    adopted = reconcile(store2)
    assert op.id in adopted
    op2 = store2.get_by_id(op.id)
    assert op2.state == State.SUCCEEDED

    effects = store2.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert effects == 1  # reconcile adopted it, did not re-execute

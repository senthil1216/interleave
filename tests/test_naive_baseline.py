import random

from interleave.adapter import FakeDownstreamAdapter
from interleave.clock import RealClock
from interleave.naive import NaiveBaseline
from interleave.retrygen import generate_attempts
from interleave.store import OperationStore, connect


def test_naive_baseline_actually_fails(tmp_path, base_request):
    db_path = str(tmp_path / "naive.db")
    conn = connect(db_path)
    store = OperationStore(conn, RealClock())
    downstream = FakeDownstreamAdapter(store, fault="lost-response")
    naive = NaiveBaseline(conn, RealClock(), downstream)

    rng = random.Random(5)
    attempts = generate_attempts("wording-variant", base_request, rng, n=2)
    applied = sum(1 for a in attempts if naive.submit_and_run(a, auto_approve=True).applied)

    assert naive.prompt_count() == 2
    assert applied == 2

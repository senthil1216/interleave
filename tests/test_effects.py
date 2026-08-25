import json
import random

from interleave.adapter import FakeDownstreamAdapter
from interleave.gateway import Gateway
from interleave.models import State
from interleave.policy import AllowAll
from interleave.retrygen import generate_attempts


def test_at_most_one_side_effect_on_lost_response_retry(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="lost-response")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    rng = random.Random(4)
    attempts = generate_attempts("wording-variant", base_request, rng, n=2)

    op = gateway.submit(attempts[0])
    gateway.approve(op.id)
    op = gateway.run_to_completion(op.id)
    assert op.state == State.SUCCEEDED

    op2 = gateway.submit(attempts[1])
    assert op2.id == op.id

    effects = store.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE normalized_intent=?", (op.normalized_intent,)
    ).fetchone()[0]
    assert effects == 1


def test_envelope_violating_undermerge_is_alarmed_not_silent(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    downstream.call(base_request.principal, base_request.tool, base_request.repo, base_request.branch, "key-a")
    downstream.call(base_request.principal, base_request.tool, base_request.repo, base_request.branch, "key-b")

    alarms = [
        json.loads(detail)
        for (_, _, event_type, detail) in store.audit_trail()
        if event_type == "near_miss_alarmed"
    ]
    assert len(alarms) == 1
    assert alarms[0]["key"] == "key-b"

    # detection only, never blocking: both writes still landed in the ledger
    applied = store.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert applied == 2

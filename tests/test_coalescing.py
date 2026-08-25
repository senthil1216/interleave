import random
from dataclasses import replace

from interleave.adapter import FakeDownstreamAdapter
from interleave.gateway import Gateway
from interleave.models import State
from interleave.naive import content_hash_key
from interleave.policy import AllowAll
from interleave.retrygen import generate_attempts, lost_uuid_key


def test_semantic_key_coalesces_vs_lost_uuid_and_content_hash(store, clock, base_request):
    rng = random.Random(1)
    attempts = generate_attempts("wording-variant", base_request, rng, n=2)

    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    ops = [gateway.submit(a) for a in attempts]
    assert ops[0].id == ops[1].id

    assert len({lost_uuid_key(a) for a in attempts}) == 2
    assert len({content_hash_key(a) for a in attempts}) == 2


def test_cross_principal_collision_never_joins(store, clock, base_request):
    b = replace(base_request, principal="agent-b")
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    op_a = gateway.submit(base_request)
    op_b = gateway.submit(b)
    assert op_a.id != op_b.id
    assert op_a.state == State.PENDING_APPROVAL
    assert op_b.state == State.PENDING_APPROVAL


def test_frozen_payload_is_canonical_not_latest_variant(store, clock, base_request):
    rng = random.Random(2)
    attempts = generate_attempts("wording-variant", base_request, rng, n=3)
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    ops = [gateway.submit(a) for a in attempts]
    op = ops[-1]
    assert op.canonical_payload["title"] == attempts[0].title
    variant_count = store.conn.execute(
        "SELECT COUNT(*) FROM variants WHERE operation_id=?", (op.id,)
    ).fetchone()[0]
    assert variant_count == 2

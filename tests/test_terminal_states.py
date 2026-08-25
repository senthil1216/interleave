from dataclasses import replace

import pytest

from interleave.adapter import FakeDownstreamAdapter
from interleave.gateway import Gateway
from interleave.models import State
from interleave.policy import AllowAll
from interleave.store import ConflictError


def test_terminal_states_immutable(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    op = gateway.submit(base_request)
    gateway.approve(op.id)
    op = gateway.run_to_completion(op.id)
    assert op.state == State.SUCCEEDED

    late_approve_ok = gateway.approvals.approve(op.id)
    assert late_approve_ok is False

    op2 = gateway.run_to_completion(op.id)
    assert op2.state == State.SUCCEEDED
    effects = store.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert effects == 1


def test_key_reuse_with_different_intent_is_conflict_no_new_op(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    gateway.submit(base_request)

    different_target = replace(base_request, target_ref="different-base-sha")
    with pytest.raises(ConflictError):
        gateway.submit(different_target)

    count = store.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    assert count == 1

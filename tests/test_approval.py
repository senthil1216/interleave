import random

from interleave.adapter import FakeDownstreamAdapter
from interleave.gateway import Gateway
from interleave.models import State
from interleave.policy import AllowAll
from interleave.retrygen import generate_attempts


def test_no_duplicate_prompts_within_live_approval(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="lost-response")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    rng = random.Random(3)
    attempts = generate_attempts("wording-variant", base_request, rng, n=2)

    prompts = 0
    last_op = None
    for a in attempts:
        op = gateway.submit(a)
        if op.state == State.PENDING_APPROVAL:
            prompts += 1
            op = gateway.approve(op.id)
        if op.state == State.APPROVED:
            op = gateway.run_to_completion(op.id)
        last_op = op

    assert prompts == 1
    assert last_op.state == State.SUCCEEDED


def test_no_execution_without_live_approval(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    op = gateway.submit(base_request)
    op2, attempt_id = gateway.revalidate_and_start_attempt(op.id)
    assert attempt_id is None
    assert op2.state == State.PENDING_APPROVAL


def test_approve_revoke_retry_is_denied(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream)
    op = gateway.submit(base_request)
    gateway.approve(op.id)
    gateway.revoke(op.id)
    op2 = gateway.run_to_completion(op.id)
    assert op2.state == State.DENIED


def test_reapproval_after_expiry_is_fresh_authority(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream, approval_ttl_seconds=1.0)
    op = gateway.submit(base_request)
    op = gateway.approve(op.id)
    assert op.state == State.APPROVED
    first_epoch = op.approval_epoch

    clock.advance(2.0)  # past the 1s TTL
    op2, attempt_id = gateway.revalidate_and_start_attempt(op.id)
    assert attempt_id is None
    assert op2.state == State.EXPIRED

    reapproved = gateway.approvals.approve(op.id)  # the human is asked again, says yes
    assert reapproved is True
    op3 = store.get_by_id(op.id)
    assert op3.state == State.APPROVED
    assert op3.approval_epoch == first_epoch + 1

    op4 = gateway.run_to_completion(op.id)
    assert op4.state == State.SUCCEEDED

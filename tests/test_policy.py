from interleave.adapter import FakeDownstreamAdapter
from interleave.gateway import Gateway
from interleave.models import State
from interleave.policy import DenyAfter, DenyAlways


def test_policy_deny_at_intake_is_reachable(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, DenyAlways(), downstream)
    op = gateway.submit(base_request)
    assert op.state == State.DENIED


def test_policy_revalidated_live_not_just_approval(store, clock, base_request):
    downstream = FakeDownstreamAdapter(store, fault="none")
    policy = DenyAfter(clock, deny_at=10.0)
    gateway = Gateway(store, clock, policy, downstream)

    op = gateway.submit(base_request)
    assert op.state == State.PENDING_APPROVAL
    op = gateway.approve(op.id)
    assert op.state == State.APPROVED  # approval itself succeeded, policy still allowed

    clock.advance(20.0)  # past deny_at, well inside the approval TTL
    op2 = gateway.run_to_completion(op.id)
    assert op2.state == State.DENIED

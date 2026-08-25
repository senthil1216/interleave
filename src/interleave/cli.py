from __future__ import annotations

import argparse
import os
import random
import sys

from interleave.adapter import FakeDownstreamAdapter
from interleave.clock import RealClock
from interleave.gateway import Gateway
from interleave.models import Request
from interleave.naive import NaiveBaseline, content_hash_key
from interleave.policy import AllowAll
from interleave.reconcile import reconcile
from interleave.retrygen import generate_attempts, lost_uuid_key
from interleave.store import OperationStore, connect
from interleave.timeline import render_terminal

DEFAULT_APPROVAL_TTL = 900.0

DEMO_REQUEST = Request(
    principal="agent-a",
    tool="open_pr",
    repo="demo/repo",
    branch="fix-flaky-test",
    title="Fix flaky test in payments module",
    body="The retry assertion was racing the mock clock; pinned it.",
)


FAULT_HELP = (
    "Fault to inject at the downstream call: none, 503 (deferred, then succeeds on retry), "
    "lost-response (applied but the ack is lost, forcing a retry), permanent-failure, "
    "or crash (this process exits for real via os._exit(1) right after the intent record "
    "is written, before the downstream is ever called)."
)
AT_HELP = (
    "Named injection point along the pipeline. Currently DISPLAY-ONLY: the fault always "
    "fires at the downstream call regardless of this value. Accepted so the CLI's "
    "vocabulary matches the fault taxonomy named in the design docs; only one of the "
    "eight named points is actually wired up yet. Safe to leave at the default."
)
SEED_HELP = (
    "Seeds the random suffix appended to the second (resampled) attempt's title/body. "
    "Affects only that wording, for reproducible output -- it has no effect on which "
    "fault fires or any approval/coalescing logic."
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="interleave")
    p.add_argument(
        "--db",
        default=None,
        help="SQLite file to use. Must come BEFORE the subcommand (it's a global flag, "
        "e.g. `interleave --db x.db run ...`, not `interleave run --db x.db ...`). Omit "
        "for a fresh, throwaway one each run (the default for one-shot commands like "
        "compare); pass explicitly when you want separate invocations to share "
        "the same operation, e.g. approve/execute against a run still in flight, or "
        "crash recovery -- and in that case, point every command in the sequence at the "
        "same file explicitly; reusing a file across a *different* demo scenario, or "
        "re-running one that already reached a terminal state, will just no-op.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Submit two demo attempts and run them to completion.")
    run.add_argument("--fault", default="none", help=FAULT_HELP)
    run.add_argument("--at", default="downstream-ack", help=AT_HELP)
    run.add_argument("--seed", type=int, default=17, help=SEED_HELP)
    run.add_argument(
        "--naive",
        action="store_true",
        help="Run the unguarded naive baseline instead of the gateway.",
    )
    run.add_argument(
        "--principal",
        default="agent-a",
        help="Caller identity. Two different principals never coalesce or share an "
        "approval, even for the identical repo/branch -- not demonstrated by `run` "
        "itself (which only submits as one principal at a time); see the "
        "cross-principal test in tests/test_coalescing.py.",
    )
    run.add_argument(
        "--approval-ttl",
        type=float,
        default=DEFAULT_APPROVAL_TTL,
        help=f"Seconds an approval stays live before it expires (default {DEFAULT_APPROVAL_TTL:.0f}s). "
        "Lower this to demo expiry without waiting on the real default.",
    )
    approve_group = run.add_mutually_exclusive_group()
    approve_group.add_argument(
        "--auto-approve",
        action="store_true",
        help="Approve automatically instead of blocking on an interactive [y/N] prompt.",
    )
    approve_group.add_argument(
        "--auto-deny",
        action="store_true",
        help="Deny automatically instead of blocking on an interactive [y/N] prompt.",
    )

    submit = sub.add_parser("submit", help="Create an operation only -- no approval, no execution.")
    submit.add_argument("--principal", default="agent-a", help="Caller identity.")

    approve = sub.add_parser("approve", help="Approve a persisted operation from a second invocation.")
    approve.add_argument("op_id", type=int, help="Operation id, as printed by submit/run.")
    approve.add_argument(
        "--approval-ttl",
        type=float,
        default=DEFAULT_APPROVAL_TTL,
        help=f"Seconds this approval stays live (default {DEFAULT_APPROVAL_TTL:.0f}s).",
    )

    revoke = sub.add_parser(
        "revoke", help="Revoke a live approval -- how the approve-then-revoke-then-retry invariant is demoed live."
    )
    revoke.add_argument("op_id", type=int, help="Operation id, as printed by submit/run.")

    execute = sub.add_parser("execute", help="Re-run the retry loop against a persisted, already-approved operation.")
    execute.add_argument("op_id", type=int, help="Operation id, as printed by submit/run.")

    compare = sub.add_parser("compare", help="Run the identical scenario through the gateway and the naive baseline, side by side.")
    compare.add_argument("--fault", default="lost-response", help=FAULT_HELP)
    compare.add_argument("--at", default="downstream-ack", help=AT_HELP)
    compare.add_argument("--seed", type=int, default=17, help=SEED_HELP)
    compare.add_argument(
        "--auto-approve",
        action="store_true",
        default=True,
        help="Always true for this command (kept only for symmetry with `run`'s flag) -- "
        "compare exists specifically to run both implementations to completion "
        "unattended, so there's no interactive or --auto-deny mode here.",
    )

    return p


def _reconciled_startup(conn) -> OperationStore:
    store = OperationStore(conn, RealClock())
    adopted = reconcile(store)
    if adopted:
        print(f"reconciled {len(adopted)} orphaned operation(s) from a prior crash: {adopted}", file=sys.stderr)
    return store


def _demo_request(principal: str) -> Request:
    return Request(**{**DEMO_REQUEST.to_payload(), "principal": principal})


def _decide_approval(args, gateway, op) -> None:
    if args.auto_deny:
        gateway.approvals.deny(op.id)
    elif args.auto_approve:
        gateway.approve(op.id)
    else:
        answer = input(f"Approve operation {op.id} ({op.repo}@{op.branch})? [y/N] ")
        if answer.strip().lower().startswith("y"):
            gateway.approve(op.id)
        else:
            gateway.approvals.deny(op.id)


def cmd_run(args) -> int:
    conn = connect(args.db)
    store = _reconciled_startup(conn)

    if args.fault == "crash":
        return _cmd_run_crash(args, store)
    if args.naive:
        return _cmd_run_naive(args, store)

    clock = RealClock()
    downstream = FakeDownstreamAdapter(store, fault=args.fault)
    gateway = Gateway(store, clock, AllowAll(), downstream, approval_ttl_seconds=args.approval_ttl)

    rng = random.Random(args.seed)
    request = _demo_request(args.principal)
    attempts = generate_attempts("wording-variant", request, rng, n=2)

    op = None
    for i, attempt in enumerate(attempts):
        op = gateway.submit(attempt)
        if op.state.value == "pending_approval":
            _decide_approval(args, gateway, op)
        op = store.get_by_id(op.id)
        if op.state.value == "approved":
            op = gateway.run_to_completion(op.id)
        print(f"attempt {i + 1}: operation {op.id} -> {op.state.value}")

    print()
    print(render_terminal(store, op.id))
    return 0


def _cmd_run_naive(args, store) -> int:
    """--naive: the same attempts, run through the unguarded baseline instead of the
    gateway -- this is the other half of the side-by-side, runnable standalone."""
    clock = RealClock()
    downstream = FakeDownstreamAdapter(store, fault=args.fault)
    naive = NaiveBaseline(store.conn, clock, downstream)

    rng = random.Random(args.seed)
    request = _demo_request(args.principal)
    attempts = generate_attempts("wording-variant", request, rng, n=2)

    for i, attempt in enumerate(attempts):
        result = naive.submit_and_run(attempt, auto_approve=not args.auto_deny)
        print(
            f"attempt {i + 1}: naive operation {result.operation_id} "
            f"prompted={result.prompted} applied={result.applied}"
        )
    print(f"\ntotal: {naive.prompt_count()} prompt(s)")
    print()
    print(render_terminal(naive.store))
    return 0


def _cmd_run_crash(args, store) -> int:
    """--fault crash: a real os._exit(1) right after the intent record is written, before
    the downstream is ever called -- not a Python exception, an actual abrupt process exit.
    The next `interleave` invocation against the same --db reconciles it automatically."""
    clock = RealClock()
    downstream = FakeDownstreamAdapter(store, fault="none")
    gateway = Gateway(store, clock, AllowAll(), downstream, approval_ttl_seconds=args.approval_ttl)

    request = _demo_request(args.principal)
    op = gateway.submit(request)
    if op.state.value != "pending_approval":
        print(f"operation {op.id} -> {op.state.value} (nothing to crash mid-execution)")
        return 0

    _decide_approval(args, gateway, op)
    op = store.get_by_id(op.id)
    if op.state.value != "approved":
        print(f"operation {op.id} -> {op.state.value} (never approved, nothing to crash)")
        return 0

    op, attempt_id = gateway.revalidate_and_start_attempt(op.id)
    print(f"operation {op.id} -> executing, attempt intent recorded (attempt_id={attempt_id})")
    print("crashing now, before the downstream call -- re-run `interleave run` against the "
          "same --db to watch the reconciler recover it")
    sys.stdout.flush()
    os._exit(1)


def cmd_submit(args) -> int:
    conn = connect(args.db)
    store = _reconciled_startup(conn)
    gateway = Gateway(store, RealClock(), AllowAll(), FakeDownstreamAdapter(store))
    op = gateway.submit(_demo_request(args.principal))
    print(f"operation {op.id} -> {op.state.value}")
    return 0


def cmd_approve(args) -> int:
    conn = connect(args.db)
    store = _reconciled_startup(conn)
    gateway = Gateway(store, RealClock(), AllowAll(), FakeDownstreamAdapter(store), approval_ttl_seconds=args.approval_ttl)
    op_before = store.get_by_id(args.op_id)
    ok = gateway.approvals.approve(args.op_id)
    if ok:
        label = "re-approved (fresh authority)" if op_before.state.value == "expired" else "approved"
        print(label)
    else:
        print(f"no-op (state is {op_before.state.value}, not pending_approval or expired)")
    return 0 if ok else 1


def cmd_revoke(args) -> int:
    conn = connect(args.db)
    store = _reconciled_startup(conn)
    gateway = Gateway(store, RealClock(), AllowAll(), FakeDownstreamAdapter(store))
    ok = gateway.approvals.revoke(args.op_id)
    print("revoked" if ok else "no-op (not approved)")
    return 0 if ok else 1


def cmd_execute(args) -> int:
    conn = connect(args.db)
    store = _reconciled_startup(conn)
    gateway = Gateway(store, RealClock(), AllowAll(), FakeDownstreamAdapter(store, fault="none"))
    op = gateway.run_to_completion(args.op_id)
    print(f"operation {op.id} -> {op.state.value}")
    print()
    print(render_terminal(store, op.id))
    return 0


def _run_comparison(db_path: str, fault: str, seed: int) -> dict:
    conn = connect(db_path)
    store = _reconciled_startup(conn)
    clock = RealClock()
    rng = random.Random(seed)
    request = DEMO_REQUEST
    attempts = generate_attempts("wording-variant", request, rng, n=2)

    gw_downstream = FakeDownstreamAdapter(store, fault=fault)
    gateway = Gateway(store, clock, AllowAll(), gw_downstream)
    gw_prompts = 0
    gw_op_ids = set()
    last_op = None
    for attempt in attempts:
        op = gateway.submit(attempt)
        gw_op_ids.add(op.id)
        if op.state.value == "pending_approval":
            gw_prompts += 1
            op = gateway.approve(op.id)
        if op.state.value == "approved":
            op = gateway.run_to_completion(op.id)
        last_op = op
    gw_effects = 1 if last_op and last_op.state.value == "succeeded" else 0
    gw_timeline = render_terminal(store, last_op.id) if last_op else ""

    naive_conn = connect(db_path.replace(".db", ".naive.db") if db_path != ":memory:" else ":memory:")
    naive_store_clock = RealClock()
    naive_downstream = FakeDownstreamAdapter(OperationStore(naive_conn, naive_store_clock), fault=fault)
    naive = NaiveBaseline(naive_conn, naive_store_clock, naive_downstream)
    naive_effects = 0
    for attempt in attempts:
        result = naive.submit_and_run(attempt, auto_approve=True)
        if result.applied:
            naive_effects += 1
    naive_timeline = render_terminal(naive.store)

    semantic_ops = len(gw_op_ids)
    lost_uuid_ops = len({lost_uuid_key(a) for a in attempts})
    content_hash_ops = len({content_hash_key(a) for a in attempts})
    claim1_evidence = (
        "Claim 1 evidence -- same two attempts, three key schemes:\n"
        f"  semantic key      (principal, tool, repo, branch) -> {semantic_ops} operation(s)  [gateway's actual key]\n"
        f"  lost-UUID key     (fresh mint each attempt)        -> {lost_uuid_ops} operation(s)  [primary baseline]\n"
        f"  content-hash key  (sha256 of full payload)         -> {content_hash_ops} operation(s)  [secondary baseline]"
    )

    return {
        "gateway_prompts": gw_prompts,
        "gateway_effects": gw_effects,
        "naive_prompts": naive.prompt_count(),
        "naive_effects": naive_effects,
        "gateway_timeline": gw_timeline,
        "naive_detail": naive_timeline,
        "claim1_evidence": claim1_evidence,
    }


def cmd_compare(args) -> int:
    result = _run_comparison(args.db, args.fault, args.seed)
    print(f"gateway: {result['gateway_prompts']} prompt(s), {result['gateway_effects']} effect(s)")
    print(f"naive:   {result['naive_prompts']} prompt(s), {result['naive_effects']} effect(s)")
    print()
    print(result["claim1_evidence"])
    print()
    print("gateway timeline:")
    print(result["gateway_timeline"])
    print()
    print("naive timeline:")
    print(result["naive_detail"])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.db is None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", prefix="interleave-", delete=False) as f:
            args.db = f.name
    handlers = {
        "run": cmd_run,
        "submit": cmd_submit,
        "approve": cmd_approve,
        "revoke": cmd_revoke,
        "execute": cmd_execute,
        "compare": cmd_compare,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

# Instructions

This project runs in a sandboxed Docker container — no local Python needed at all, only Docker.

## Before you start: what you're about to run

Picture an AI agent that needs a human's approval before it takes some real action — say, opening
a pull request. Sometimes the call to actually perform that action fails partway through and gets
retried. Two things can go wrong when that happens: the action can end up happening **twice** (the
human approved once, but it happened twice), or the retry can sneak through **after** the approval
should no longer count.

This project contains two competing implementations of that approval step: **the gateway**, and
**the naive baseline**. Rather than describe the difference up front, the next few runs show it to
you directly — one implementation at a time, then both together.

One thing that trips people up before we start: several commands below read `docker run --rm
interleave interleave ...` — `interleave` **twice**. The first is the **image name** (what you
built); the second is the **command being run inside the container** (this project's own CLI,
which happens to also be named `interleave`).

## The fixture every run below uses

`run`/`compare` don't take a custom request — they always submit the same scripted one:
opening a pull request (`tool=open_pr`) to `demo/repo` on branch `fix-flaky-test`, as principal
`agent-a`, and always as **two attempts**, never one or three. The title and body get reworded on
the second attempt (that's the "resampled retry" from the setup above); the repo, branch, and tool
never change. This is a fixed fixture, not something you configure — it's what makes every output
block in this file exactly reproducible, and it's why `demo/repo@fix-flaky-test` and `agent-a`
show up in every run without ever being explained as an input.

**Three different things below are all called "attempt," and they're independent counters — worth
naming once, up front, since the word repeats but the meaning doesn't:**

- `attempt 1: operation 1 -> succeeded` / `attempt 2: ...` — the CLI's own top-level print, once
  per *external* submission from the fixture above. Always exactly 2, every run.
- `execute_attempted {'attempt_no': N}` — the gateway's (or naive's) own internal counter for how
  many times it has called the fake backend for one operation. Resets per operation, independent
  of how many external attempts there were.
- `variant_recorded {'variant_no': N}` — how many differently-worded external attempts have been
  recorded against one operation. Also independent of `attempt_no` — see the gateway timeline
  walkthrough below for a worked example of the two diverging in the same run.

## Prerequisite: the Docker daemon has to actually be running

`docker build`/`docker run` need a running daemon, not just the `docker` CLI installed. If a
command below fails with something like `Cannot connect to the Docker daemon` or a Unix socket
error, that's this, not a bug in the project — start it first (`colima start`, or launch Docker
Desktop) and retry.

## How to install (build the image)

From the project root (where this file and `Dockerfile` live):

```
docker build -t interleave .
```

**What this does:** pulls `python:3.11-slim`, copies `pyproject.toml`/`src`/`tests` into the
image, runs `pip install -e ".[dev]"` inside it, then creates and switches to a non-root user for
everything that runs afterward. Nothing runs yet — this just builds the image.

**Output:** several build steps, ending in:
```
Successfully built <image id>
Successfully tagged interleave:latest
```

## How to run tests

```
docker run --rm interleave
```

**What this does:** starts a fresh, throwaway container (`--rm` deletes it on exit) from the image
and runs its default command, `pytest -q`, inside it. This runs 17 automated checks against the
gateway and the naive baseline — things like "two attempts at the same action never produce two
approval prompts on the gateway" and "the naive baseline actually does produce two prompts, rather
than that being assumed." You'll see several of these claims demonstrated live, in the runs below.

**Output:**
```
.................                                                        [100%]
17 passed in 0.13s
```

## Run: the gateway handling a retry

**What this run is about:** here's the scenario every run below uses. An agent submits a request —
in this simulation, opening a pull request. The first attempt hits a scripted failure:
`--fault lost-response` means the fake backend actually completes the action, but the confirmation
gets lost on the way back, so the caller can't tell whether it worked. The agent retries — with
slightly different wording than the first attempt, because a real agent doesn't resend the exact
same text when it retries; it generates new text, now that the earlier failure is part of what
it's reacting to. `--seed 17` only controls which random reworded text gets generated for that
retry, so the output below is exactly reproducible — it has no effect on the fault or the approval
logic. No `--db` flag or mounted volume needed yet either — this whole run happens inside one
throwaway container.

This first run sends both attempts to **the gateway** only.

**Command:**
```
docker run --rm interleave interleave run --fault lost-response --seed 17 --auto-approve
```

**What the output says:**
```
attempt 1: operation 1 -> succeeded
attempt 2: operation 1 -> succeeded

[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.000s] op=1     approval_requested {'epoch': 0}
[+  0.000s] op=1     approval_granted {'epoch': 1, 'fresh_authority': False}
[+  0.000s] op=1     execute_attempted {'attempt_no': 1}
[+  0.001s] op=1     effect_lost_response
[+  0.001s] op=1     execute_attempted {'attempt_no': 2}
[+  0.001s] op=1     effect_applied
[+  0.001s] op=1     operation_succeeded
[+  0.001s] op=1     operation_joined {'variant_no': 1}
[+  0.001s] op=1     variant_recorded {'variant_no': 1}
```

Both attempts report `operation 1` — the gateway treated them as the same underlying action, not
two. Reading the timeline:

- **`operation_created`** — attempt 1 arrives and the gateway opens a new "operation" (its
  internal name for one tracked action-plus-its-approval).
- **`approval_requested` / `approval_granted`** — a human is asked once and approves.
- **`execute_attempted (attempt_no: 1)`** — right before acting, the gateway double-checks the
  approval is still valid, then calls the fake backend, which reports `effect_lost_response` —
  the injected fault.
- **`execute_attempted (attempt_no: 2)`** — the gateway retries on its own and this time succeeds.
- **`operation_joined` / `variant_recorded`** — this is where the second, differently-worded
  attempt lands: the gateway recognizes it as the *same* action under different wording, so it's
  filed against the already-succeeded operation as a recorded *variant* instead of starting a new
  one — its exact wording is kept for the record, but it never overwrites the text that was
  actually approved. (`variant_no` here is a different counter than `attempt_no` above it — that
  one counts execution retries against the backend, this one counts alternate wordings recorded
  for the operation.)

Net for this run: **one** approval prompt, **one** applied effect, for two attempts. Keep that
number in mind for the next run.

## Run: the naive baseline handling the identical retry

**What this run is about:** the exact same scenario — same fault, same seed, same two attempts —
sent to **the naive baseline** instead, printed using the *same event names* as the gateway's
timeline above, so the two are easy to put side by side. The difference between the two
implementations is entirely in how they decide whether an incoming attempt is "the same action as
before" or "a new one," and this run shows what happens when that decision is made carelessly: by
treating every retry as potentially new, rather than recognizing it.

**Command:**
```
docker run --rm interleave interleave run --naive --fault lost-response --seed 17 --auto-approve
```

**What the output says:**
```
attempt 1: naive operation 1 prompted=True applied=True
attempt 2: naive operation 2 prompted=True applied=True

total: 2 prompt(s)

[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.000s] op=1     approval_requested
[+  0.000s] op=1     approval_granted
[+  0.000s] op=1     execute_attempted {'attempt_no': 1}
[+  0.000s] op=1     effect_lost_response
[+  0.001s] op=1     operation_succeeded
[+  0.001s] op=2     operation_created {'principal': 'agent-a'}
[+  0.001s] op=2     approval_requested
[+  0.001s] op=2     approval_granted
[+  0.001s] op=2     execute_attempted {'attempt_no': 1}
[+  0.001s] op=-     near_miss_alarmed {'principal': 'agent-a', 'repo': 'demo/repo', 'branch': 'fix-flaky-test', 'key': '...'}
[+  0.001s] op=2     effect_applied
[+  0.001s] op=2     operation_succeeded
```

Same event names as the gateway's timeline, on purpose — line the two up and the divergence is
visible event-by-event, not just in the summary numbers:

- **Two `operation_created` events**, `op=1` and `op=2` — the second attempt never joins the
  first. Compare against the gateway's timeline in the *previous* run above: one
  `operation_created` and one `operation_joined`, not two of the former. `operation_joined` never
  appears anywhere in naive's own timeline (neither here nor in `compare`'s naive column below) —
  that event doesn't exist for naive, on purpose, since it never recognizes a retry as a repeat.
- **Each operation gets its own `approval_requested`/`approval_granted`** — two prompts, because
  nothing recognized the second attempt as a repeat of the first.
- **`op=1` hits the same injected fault as the gateway did** — `effect_lost_response` — but then
  immediately reports `operation_succeeded` anyway. This is a second, separate bug from the
  coalescing one: the naive baseline doesn't distinguish "unknown outcome" from "success," so a
  lost response — which the gateway correctly treated as "must retry" — gets trusted as done here.
- **`near_miss_alarmed` fires before `op=2`'s effect is applied** — the ledger watchdog notices a
  second write landing on the same `{principal, repo, branch}` under a different key, within the
  window, and flags it visibly in the audit trail, even though it can't and doesn't block it (see
  `README.md`'s Contract section for why — it can't tell an envelope violation from a legitimate
  second operation).
- **Net:** two operations, two prompts, two applied effects — one of which shouldn't have counted
  as applied at all.

## Run: compare — put both side by side

**What this run is about:** you've now run both halves separately and already know the numbers
differ — one prompt/one effect vs. two/two. `compare` runs the identical scenario through both
implementations in a single command instead of two, does that counting for you automatically, and
adds one thing neither separate run above shows on its own. (`--at downstream-ack` below is
currently display-only — it's accepted and shown in the output, but the fault always fires at the
downstream call regardless of its value; `interleave run --help` says the same thing.)

**Command:**
```
docker run --rm interleave interleave compare --fault lost-response --at downstream-ack --seed 17
```

**What the output says:**
```
gateway: 1 prompt(s), 1 effect(s)
naive:   2 prompt(s), 2 effect(s)

Claim 1 evidence -- same two attempts, three key schemes:
  semantic key      (principal, tool, repo, branch) -> 1 operation(s)  [gateway's actual key]
  lost-UUID key     (fresh mint each attempt)        -> 2 operation(s)  [primary baseline]
  content-hash key  (sha256 of full payload)         -> 2 operation(s)  [secondary baseline]

gateway timeline:
[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.001s] op=1     approval_requested {'epoch': 0}
[+  0.001s] op=1     approval_granted {'epoch': 1, 'fresh_authority': False}
[+  0.001s] op=1     execute_attempted {'attempt_no': 1}
[+  0.002s] op=1     effect_lost_response
[+  0.002s] op=1     execute_attempted {'attempt_no': 2}
[+  0.003s] op=1     effect_applied
[+  0.003s] op=1     operation_succeeded
[+  0.004s] op=1     operation_joined {'variant_no': 1}
[+  0.004s] op=1     variant_recorded {'variant_no': 1}

naive timeline:
[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.000s] op=1     approval_requested
[+  0.000s] op=1     approval_granted
[+  0.000s] op=1     execute_attempted {'attempt_no': 1}
[+  0.000s] op=1     effect_lost_response
[+  0.001s] op=1     operation_succeeded
[+  0.001s] op=2     operation_created {'principal': 'agent-a'}
[+  0.001s] op=2     approval_requested
[+  0.001s] op=2     approval_granted
[+  0.001s] op=2     execute_attempted {'attempt_no': 1}
[+  0.001s] op=-     near_miss_alarmed {'principal': 'agent-a', 'repo': 'demo/repo', 'branch': 'fix-flaky-test', 'key': '...'}
[+  0.001s] op=2     effect_applied
[+  0.001s] op=2     operation_succeeded
```

The top two lines match exactly what you already saw running each side on its own — same result,
just counted and printed together instead of read off two separate runs. Both full timelines are
printed together too, in the same event vocabulary, so you can scroll between them and see exactly
where they diverge — one `operation_created` vs. two, one approval cycle vs. two, and the extra
`near_miss_alarmed` / silently-wrong `operation_succeeded` that only show up on the naive side (see
the previous run's walkthrough for what those two mean).

The new part is the "Claim 1 evidence" block. It answers a fair objection: *couldn't you fix the
naive baseline's problem more simply* — say, by having the caller mint a fresh unique ID for each
request, or just using a different hash? The block runs the identical two attempts through both of
those ideas: a freshly-minted ID each time (`lost-UUID key`) and a hash of the full text
(`content-hash key`, what the naive baseline itself already does). Both still produce 2 operations
— neither one recognizes the reworded retry as the same request. Only identifying the request by
its *meaning* — which repo, which branch, which kind of action, wording aside — gets it down to 1.
That's the actual mechanism the gateway uses, not just "the gateway is more careful" as an
assertion. Still no `--db` or mounted volume needed here — this whole comparison happens inside
one throwaway container.

## Everything below needs a mounted volume and an explicit `--db`

The runs so far have each been one self-contained container. The two scenarios below are
different: they're *two or more separate* container runs that need to act on the *same*
underlying state — the way a real system would have one process record "approved, about to act"
and a possibly-different process pick that back up later. A bare `docker run --rm` throws away its
entire filesystem when the container exits, so from here on every command mounts a folder on your
machine into the container and points `--db` at a file inside it, so state actually survives
between commands:

```
mkdir -p ./interleave-data
```

*(Verified against Docker on macOS via colima, where volume mounts default to bind-mounting only
the home directory, not `/tmp` — hence a project-local directory here rather than `/tmp`. Native
Linux Docker doesn't have that restriction, but the same mounted-volume approach works there
unchanged.)*

Two things worth knowing before running these: `--db` is a *global* flag, so it goes **before**
the subcommand — `interleave --db /data/x.db run ...`, not `interleave run --db /data/x.db ...`
(the latter fails with "unrecognized arguments"). And each sequence below is written assuming a
**fresh** `--db` file — re-running `crash.db` or `exp.db` a second time after either one already
reached a terminal state (`succeeded`) won't replay the scenario, it'll just print that state and
no-op. If you want to run one of these more than once, use a new filename or `rm` the old one
first.

## Run: crash and recovery — a real process death, not a simulation

**What this run is about:** `--fault crash` makes the container process actually die — a real
`os._exit(1)`, not a caught error — right after it's durably written down "I'm about to act on
this operation" but *before* it has actually called the backend. The question this answers: does
the system remember that half-finished intent and recover safely on the next run, or does it just
lose track of it?

One thing to know before running this: the *second* command below is not a dedicated "resume" or
"recover" command — it's the exact same two-attempt `run` as every other run in this walkthrough.
Recovery happens automatically, as a side effect of *any* `interleave` command starting up against
a `--db` file that has unfinished business in it, before whatever that command normally does.

**Command:**
```
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/crash.db run --fault crash --seed 17 --auto-approve
echo "exit code: $?"
```

**What the output says:**
```
operation 1 -> executing, attempt intent recorded (attempt_id=1)
crashing now, before the downstream call -- re-run `interleave run` against the same --db to watch the reconciler recover it
exit code: 1
```

Now run it again — a fresh container, same mounted `--db` file:

**Command:**
```
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/crash.db run --fault none --seed 99 --auto-approve
```

**What the output says** (captured after a real ~3 second gap between the two commands — your own
timestamps will differ; that's expected, see below):
```
attempt 1: operation 1 -> succeeded
attempt 2: operation 1 -> succeeded

[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.000s] op=1     approval_requested {'epoch': 0}
[+  0.001s] op=1     approval_granted {'epoch': 1, 'fresh_authority': False}
[+  0.001s] op=1     execute_attempted {'attempt_no': 1}
[+  3.174s] op=1     reconciled_orphan {'attempt_id': 1, 'resolution': 'retryable'}
[+  3.174s] op=1     operation_joined {'variant_no': 1}
[+  3.174s] op=1     variant_recorded {'variant_no': 1}
[+  3.174s] op=1     execute_attempted {'attempt_no': 2}
[+  3.175s] op=1     effect_applied
[+  3.176s] op=1     operation_succeeded
[+  3.176s] op=1     operation_joined {'variant_no': 2}
[+  3.176s] op=1     variant_recorded {'variant_no': 2}
```

Three things here that look like bugs on first read, and aren't:

- **The multi-second jump between `execute_attempted {'attempt_no': 1}` and `reconciled_orphan` is
  real elapsed wall-clock time, not a hang.** This timeline is one merged audit log spanning *two
  separate container runs* — every timestamp is relative to the very first event ever written to
  this `--db` file (back in the crashed process), not to when the current command started. The gap
  is exactly how long you took between running the two commands above; a slower typist gets a
  bigger number, not a broken one.
- **The second command isn't a "resume."** `reconciled_orphan` fires first, as a side effect of
  *any* command starting up against this `--db` file — it finds the orphaned attempt left by the
  crash, sees no matching ledger entry (the crash happened before the backend was ever called),
  and resets the operation from `executing` back to `approved`, marked `resolution: retryable`.
  Only *after* that does `run` submit its own two fresh attempts, same as always: the first joins
  the now-recovered operation (`operation_joined {'variant_no': 1}`) and triggers the retry that
  actually succeeds (`execute_attempted {'attempt_no': 2}`); the second joins again
  (`variant_no: 2`). Nothing here is special-cased crash-recovery logic — reconcile-on-startup and
  submit-two-attempts are each doing exactly what they always do; it's the sequence that recovers.
- **Re-running the crash command a third time against this same file won't crash anything.** The
  operation is now `succeeded` — a terminal state — so `interleave --db /data/crash.db run --fault
  crash ...` again will just print `operation 1 -> succeeded (nothing to crash mid-execution)` and
  exit `0`. To watch the crash happen again, use a new `--db` file (or delete `crash.db` first).

Net: exactly one applied effect (`effect_applied` appears once) across both container runs, not
zero and not two — that's the actual claim. Everything above is explaining why the output looks
the way it does, not evidence that something's broken.

## Run: an approval expiring, then being re-granted as fresh authority

**What this run is about:** an approval isn't meant to stay valid forever. If it lapses before the
action happens, this project's position is that asking the human again and getting a fresh "yes"
is *not* the same thing as the earlier approval somehow still counting — asking again is required,
not optional. This sequence — five separate container runs, same mounted `--db` file so each one
builds on the last — makes that concrete: approve, let the approval lapse, watch it get rejected
as expired, ask again, and only then does the action succeed.

**Command (run these five, in order):**
```
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/exp.db submit
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/exp.db approve 1 --approval-ttl 1
sleep 2
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/exp.db execute 1
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/exp.db approve 1
docker run --rm -v "$(pwd)/interleave-data:/data" interleave interleave --db /data/exp.db execute 1
```

**What the output says** (the four short status lines, in order, are `submit`, `approve`,
`execute`, `approve`, `execute` — but `execute` also prints the full timeline for the operation so
far, same as `run` does):
```
operation 1 -> pending_approval
approved
operation 1 -> expired

[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.000s] op=1     approval_requested {'epoch': 0}
[+  0.046s] op=1     approval_granted {'epoch': 1, 'fresh_authority': False}
[+  2.124s] op=1     approval_expired
[+  2.124s] op=1     operation_expired
---
re-approved (fresh authority)
operation 1 -> succeeded

[+  0.000s] op=1     operation_created {'principal': 'agent-a'}
[+  0.000s] op=1     approval_requested {'epoch': 0}
[+  0.046s] op=1     approval_granted {'epoch': 1, 'fresh_authority': False}
[+  2.124s] op=1     approval_expired
[+  2.124s] op=1     operation_expired
[+  2.171s] op=1     approval_granted {'epoch': 2, 'fresh_authority': True}
[+  2.217s] op=1     execute_attempted {'attempt_no': 1}
[+  2.218s] op=1     effect_applied
[+  2.218s] op=1     operation_succeeded
```
(The `---` above marks where this write-up split the two `execute` calls apart for readability;
it's not something the CLI prints.)

- `--approval-ttl 1` sets a 1-second window for the approval, purely so you don't have to wait on
  the real 900-second default to see it lapse.
- The first `execute` shows the mechanism directly: `approval_granted {'epoch': 1, ...}` happened,
  then enough wall-clock time passed (`+2.124s`, past the 1-second TTL) that the *next* thing to
  touch this operation finds it stale and transitions it straight to `approval_expired` /
  `operation_expired` — before ever attempting the backend call. The system won't act on a stale
  "yes."
- The second `approve` grants a genuinely new one: `approval_granted {'epoch': 2,
  'fresh_authority': True}` — the `fresh_authority` flag (only ever `True` here, never on a first
  approval) is exactly what distinguishes this from a duplicate prompt. The final `execute`
  succeeds because of *that* grant, not because the first one was ever still good — the full
  timeline preserves both epochs so that distinction is auditable after the fact, not just
  asserted.

## What this walkthrough doesn't cover

The CLI surface is bigger than what's demoed above — worth knowing it's there rather than
discovering it only via `--help`:

- **`interleave revoke <op-id>`** — revokes a live approval. Not walked through here, but it's
  half the thesis (the "stale authority" failure mode from the opening description): approve,
  then revoke, then a retry arrives — it's denied, not replayed. See `interleave revoke --help`
  and the `test_approve_revoke_retry_is_denied` test in `tests/test_approval.py`.
- **`--fault 503`** and **`--fault permanent-failure`** — two fault types besides `lost-response`
  and `crash`. `503` defers the first call and succeeds on retry (no ambiguity about whether it
  applied, unlike `lost-response`); `permanent-failure` always fails, no retry recovers it. Try
  either in place of `lost-response` in any `run`/`compare` command above.
- **Cross-principal scoping** — `--principal` is accepted on `run`/`submit`, but every run in this
  walkthrough only ever uses one (`agent-a`). The claim that two *different* principals targeting
  the same repo/branch never share an approval, even though neither request errors, is real and
  tested (`tests/test_coalescing.py::test_cross_principal_collision_never_joins`) but isn't
  demonstrated by any command here — see `README.md`'s "What makes the problem interesting"
  section for the reasoning.

`README.md` has the full design rationale and the complete 14-row invariants table; `pytest -q`
(the first thing this file has you run) is what actually proves all of it, including the three
items above.

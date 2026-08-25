# Interleave

A reliability drill for agent tool-call approval.

> Human approval of an agent action only means something if the action happens exactly once, and
> if a retry cannot slip past the approval. Both of those are reliability properties, not security
> ones.

## Why this project is needed

AI agents increasingly take real-world actions — issuing a refund, deploying a service, messaging
a customer — behind a human approval gate. The promise of that gate is precise: *this one action,
with these parameters, will happen because a person said yes.*

Two quiet failures break that promise without ever tripping a security control:

- **Duplication.** A person reviewed and approved one refund, one deploy, one customer-wide
  message — and three went out. The retry machinery, not the human and not the policy, turned one
  yes into three effects.
- **Stale authority.** Approval is granted, the call fails, a retry fires later — after the
  approval expired, after the policy changed, or after the human revoked. The retry replays an
  approved request as an unapproved action.

In both cases no control was bypassed and no policy was violated — the authorization audit passes
— yet oversight failed anyway. That is the worst class of safety control: one that produces
confidence without producing safety, and discourages anyone from looking closer. This project
makes both failures happen on demand, reproducibly, so they can be studied instead of assumed
away.

## What makes the problem interesting

*Isn't this just idempotency keys and TOCTOU?* Yes — no new primitive. What's different is a
precondition those tools assume: the caller can reproduce a stable identity. Agents break that
by default.

- **Replay vs resample.** A normal client retries the same bytes, so a UUID or a content hash
  works. An agent retries by generating again: the old ID is gone and the wording shifted, so
  both classical keys miss. Identity has to be derived as `(principal, tool, normalized intent)`.
  (Same problem at a tool boundary in a delegation chain; a multi-hop fixture is not built.)
- **That key is approximate.** Two different intents can look the same (over-merge — blocked by
  conflict + principal scoping) or one intent can look like two (under-merge — cannot be blocked,
  only alarmed). Outside "wording changes, target doesn't," coalescing is undecidable; this
  project doesn't pretend otherwise.
- **Approval-then-execute is ordinary TOCTOU**, not agent-specific. Agents just hit it more.
  Don't trust `{approved: true}` on the retry; revalidate live state at execute time.
- **Lost-response is a third outcome** (applied, ack gone), not success or failure. That's where
  naive retry doubles the effect.

## What this project is

Not a gateway — an instrument. Pick a fault (`503`, lost-response, crash, permanent-failure) and
hit run. Two implementations execute that exact scenario side by side: the naive baseline records
two simulated pull-request side effects and prompts the human twice; the gateway records one.
`--seed` only makes the resampled retry's wording reproducible — it does not choose the fault,
and it is not printed back; you pass the same value in again. `--at` is accepted so the CLI's
vocabulary matches the eight named injection points; only `downstream-ack` is wired (see Scope).

## Architecture

Both variants below run against the identical `--fault` and `--seed`. The gateway is what's
being demonstrated; the naive baseline exists to give the invariants something to fail against.

```
                         ┌─────────────────────────┐
 Agent A ─┐              │      Fault Picker        │
 Agent B ─┼── requests ─▶│  --fault lost-response    │
 Agent C ─┘              │  --at downstream-ack      │
                         │  --seed 17                │
                         └──────────┬───────────────┘
                                    │ fault fires at the downstream call
                                    ▼
        ┌───────────────────────────────────────────────────────┐
        │                        GATEWAY                         │
        │                                                        │
        │  1. Policy check ── deny ─────────────────▶ denied     │
        │        │ allow                                         │
        │        ▼                                                │
        │  2. Normalize request  {principal, tool, repo, branch}   │
        │        │ (wording excluded, payload frozen — claim 1)    │
        │        ▼                                                │
        │  3. get_or_create(principal, tool, intent)             │
        │     Operation Store — SQLite                            │
        │     UNIQUE(principal, tool, intent), CAS                 │
        │     (no process mutex — coalescing test can fail)        │
        │        │                                                 │
        │        ├── same principal, matching ──▶ join existing op │
        │        ├── same key, different semantics ──▶ conflict     │
        │        ├── different principal ──▶ independent operation  │
        │        │     (impossible to collide — principal is in     │
        │        │     the key; no join, no conflict — claim 1)     │
        │        ▼                                                 │
        │  4. pending_approval  (durable state, not a boolean)     │
        │        │ approved                                        │
        │        ▼                                                │
        │  5. REVALIDATE approval + policy, live, per attempt       │
        │     — claim 2: never trust a field carried in the         │
        │       retry payload —                                     │
        │        │                                                  │
        │        ▼  (5+6 share one transaction — TOCTOU gap          │
        │            closed locally, not just narrowed)              │
        │  6. executing ── attempt (budget vs. deadline, not a       │
        │     fixed retry count) ──▶ write intent record first       │
        └────────┬─────────────────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────┐
        │   Fake Downstream Adapter    │
        │  success | 503-then-success  │
        │  lost-response (applied,     │
        │    ack never returned)       │
        │  permanent failure           │
        │                              │
        │  Append-only ledger, keyed   │
        │  by normalized intent ───────┼──▶ at most one applied effect
        └────────┬────────────────────┘     regardless of retries
                 │
                 ▼
     terminal state: succeeded / denied / expired / failed  (immutable)
                 │
                 ▼
        ┌──────────────────────────┐
        │   Audit trail / timeline   │      On startup against --db:
        │  every transition legible  │◀───  Reconciler: adopted (ledger
        │  without reading source    │      already has the effect — do
        └──────────────────────────┘      not re-execute) or retryable
                                            (crash was before the call —
                                            reset to approved).

 ── side by side ──
        ┌───────────────────────────────────────────────────────┐
        │              NAIVE BASELINE (same --fault / --seed)     │
        │  no coalescing · unguarded retry · approval trusted      │
        │  from payload · does not consult the ledger              │
        │  → duplicate ops, duplicate prompts, duplicate effects   │
        │    (adapter still records writes: near-miss can fire)    │
        └───────────────────────────────────────────────────────┘
```

Reading order:

- **Fault Picker** — `--fault` chooses the injected failure (`crash` is a real `os._exit(1)` at
  the CLI, not an adapter outcome); `--seed` only reproduces the resampled title/body suffix.
  Same `--fault` + `--seed` in both variants means the divergence is attributable to the
  machinery, not the inputs. `--at` names an injection point but is currently display-only
  (see Scope).
- **Gateway** — policy → normalize → `get_or_create` → durable approval → revalidate and execute
  *inside one transaction*. Agent A/B/C in the diagram are distinct principals, deliberately —
  two agents whose requests normalize onto the same repo/branch must never share one approval,
  even though neither request errors.
- **Fake Downstream Adapter** — simulates GitHub-style PR creation with four outcomes (success,
  503-then-success, lost-response, permanent failure) and keeps an append-only ledger of applied
  effects keyed by normalized intent, plus a near-miss watchdog that flags a second write to the
  same `{principal, repo, branch}` within a short window. Shared by both implementations; the
  naive baseline just never *consults* it before acting.
- **Reconciler** — every command against a `--db` file runs this on startup, looking for
  attempt-intent records with no outcome. Two resolutions: **`adopted`** — the ledger already
  has the effect, so mark succeeded and do not re-execute (the mechanism test); **`retryable`**
  — the crash was *before* the downstream call, so reset to `approved` and let the command that
  just started complete it (the CLI `--fault crash` demo). Recovery is not a separate command.
- **Naive baseline** — same interface, no guards: content-hash keying, unguarded retry, approval
  trusted from the payload, lost-response treated as success. Duplicate effects are the point;
  `near_miss_alarmed` on its timeline is the watchdog firing, not the naive baseline protecting
  anything. It exists so the invariants have something real to fail against.

The two bottom boxes are the hero visual: identical `--fault` / `--seed`, divergent outcomes.
One-line spec so the two axes being demonstrated don't muddy each other: the top-level split is
always naive vs. gateway; the lost-UUID/content-hash key comparisons that demonstrate claim 1 are
a `Claim 1 evidence` block *inside* `compare` output, not a third top-level comparison.

## Contract

For each logical protected action: at most one downstream side effect, at most one human approval
prompt *while an approval is live* — re-prompting after expiry or revocation is fresh authority,
not a duplicate — and a complete audit trail, across process restart.

**Invariant ownership**, stated explicitly because the ledger could otherwise look like it's doing
the gateway's job for it: the gateway owns the *oversight* invariants — one live approval, one
prompt per epoch, revalidation, audit. The ledger models a *downstream contract* — idempotent-by-key,
or at least queryable for what already happened. A gateway cannot conjure effect-once over a
downstream that doesn't cooperate (real GitHub has no idempotency header on PR creation); it can
only bound attempts and reconcile against whatever the downstream is willing to report. That
limitation is a first-class result of this project, not a shortcut it's hiding.

*Non-claims:* no exactly-once delivery across networks, no multi-node consensus, no distributed
locks, no real GitHub calls, no new distributed-systems primitive.

Every invariant below is a test that can actually fail — not a sanity check — and each maps
to a named `pytest` function under `tests/` (one row is covered by two tests; see Status):

| Invariant | Test |
|---|---|
| Semantic key coalesces content-varying retries (claim 1) | `test_semantic_key_coalesces_vs_lost_uuid_and_content_hash` — two attempts, same principal, same `{tool, repo, branch}`, different title/body wording — one operation. Same two requests replayed with a lost UUID (primary) and a content hash (secondary), shown side by side producing two operations |
| Cross-principal collision never joins | `test_cross_principal_collision_never_joins` — two different principals normalize onto the same intent — two independent operations, two approvals, neither errors, never merged into one |
| Approved payload is the frozen canonical, not the latest variant | `test_frozen_payload_is_canonical_not_latest_variant` — coalesce three wording variants; assert the approval and the executed payload both match attempt 1, others recorded as variants |
| Envelope-violating under-merge is alarmed, not silent | `test_envelope_violating_undermerge_is_alarmed_not_silent` — two writes whose keys differ but land on the same `{principal, repo, branch}` within the window — audit trail shows a near-miss alarm; both writes still land |
| One operation per key under concurrency | `test_one_operation_per_key_under_concurrency` — 3 concurrent creates, no mutex; unique violation caught and joined |
| No duplicate prompts within a live approval | `test_no_duplicate_prompts_within_live_approval` — naive baseline emits 2+ for one live approval; gateway asserts exactly 1. Re-prompt after expiry is a *second* test, not counted as a duplicate: `test_reapproval_after_expiry_is_fresh_authority` |
| At most one side effect | `test_at_most_one_side_effect_on_lost_response_retry` — lost-response fault, then retry |
| Approval survives restart | `test_approval_survives_restart` — drop the process during `approved`, reopen the same file, execute |
| No execution without live approval | `test_no_execution_without_live_approval` — per-attempt revalidation asserted on the retry path |
| Policy revalidated live, not just approval | `test_policy_revalidated_live_not_just_approval` — policy flips to deny between approval and execution — attempt denied even though the approval itself is still valid |
| Policy deny at intake is reachable | `test_policy_deny_at_intake_is_reachable` — policy denies a fresh request — operation terminates `denied` before approval is ever requested |
| Approve → revoke → retry is denied | `test_approve_revoke_retry_is_denied` — revoke after approval, then retry arrives — denied, not replayed |
| Terminal states immutable | `test_terminal_states_immutable` — late approval and post-success replay both rejected |
| Key reuse with different intent | `test_key_reuse_with_different_intent_is_conflict_no_new_op` — explicit conflict, no new operation |

Plus two mechanism checks that aren't separate claims but back the ones above:
`test_reconcile_adopts_orphaned_effects` (the `adopted` resolution — crash *after* the effect
applied) and `test_naive_baseline_actually_fails` (the naive baseline actually fails the way
it's claimed to, rather than being assumed to). The CLI `--fault crash` demo is the other
reconcile resolution (`retryable` — crash *before* the downstream call); it is walked through
in `INSTRUCTIONS.md`, not an 18th test.

## CLI

```
interleave [--db PATH] run     --fault lost-response --at downstream-ack --seed 17 [--naive] [--principal agent-a]
                                [--auto-approve | --auto-deny] [--approval-ttl SECONDS]
interleave [--db PATH] submit   [--principal agent-a]                    # create, no approval
interleave [--db PATH] approve <op-id>   [--approval-ttl SECONDS]        # second terminal, same --db
interleave [--db PATH] revoke  <op-id>                                   # how revoke-then-retry gets demoed live
interleave [--db PATH] execute <op-id>                                   # re-run the retry loop on a persisted op
interleave [--db PATH] compare --fault lost-response --at downstream-ack --seed 17
                                                                         # gateway + naive, side by side
```

`--db` is a *global* flag and must come **before** the subcommand (`interleave --db x.db run ...`,
not `interleave run --db x.db ...`). Omit it for a throwaway file (the default for one-shot
`run`/`compare`); pass the same path on every command in a multi-invocation sequence. Re-running
a file that already reached a terminal state no-ops — use a fresh filename, or `rm` it first.

`run` blocks on an interactive approval prompt by default; `--auto-approve`/`--auto-deny` bypass
it for scripted use. `--seed` only controls the random suffix on the second attempt's title/body.
`--at` is currently display-only (see Scope). `--fault crash` triggers a real `os._exit(1)` right
after the intent record is written, before the downstream is ever called. Recovery isn't a
separate command: the reconciler runs on startup of *whatever* you invoke next against the same
`--db` (`retryable` if the call never happened, `adopted` if the ledger already has the effect),
then that command does its normal work. The follow-up must **not** pass `--fault crash` again —
that path sees the now-`approved` operation and exits without executing. Use `--fault none`
(the default). Python 3.11+, stdlib for everything except `pytest` (dev) — `python -m
interleave ...` works with no install step; the evaluation path is Docker — see
**[`INSTRUCTIONS.md`](./INSTRUCTIONS.md)**.

## How to run

See **[`INSTRUCTIONS.md`](./INSTRUCTIONS.md)** for build, tests, and every demo command.

## Scope

**Deliberately cut, not built:** a fuzzed/random-schedule discovery mode that searches for unknown
bad interleavings instead of replaying a picked one (the honest reason claim 4 — "the rare
interleaving is the product" — stays framing, not a demonstrated claim); multi-arch verification;
a rich scenario picker; a browser UI (the CLI comparison already tells the whole story); real
GitHub calls; multi-node consensus or any new distributed-systems primitive. A `render` subcommand
that wrote the comparison to a static HTML file existed at one point (built for a GitHub-Pages-hosted
demo that this project no longer uses, now that it's submitted as a zip evaluated in a sandboxed
Docker container) and was removed along with `docs/`, since a second, mostly-duplicate way to see
the same output wasn't earning its keep once the reason for it went away.

**Simplified during implementation, not silently dropped:** the fault taxonomy names eight
injection points along the pipeline (`pre-policy` through `pre-reconcile`), but only one —
`downstream-ack`, where the fake adapter's own fault logic fires — is actually wired up; `--at` is
accepted and validated for the rest. The multi-agent delegation argument above is analysis, not a
separate fixture — `--principal` models one hop.

## Status

Core implementation done and green: SQLite-backed gateway (principal-scoped keys, frozen payload,
atomic revalidate+execute, ledger + near-miss watchdog, crash-safe reconciler), the naive baseline,
the full CLI, and all 14 invariants as passing `pytest` tests (17/17: 15 tests across those 14
rows — re-approval after expiry is a second test under "no duplicate prompts" — plus 2 mechanism
checks). Submitted as a zip with a Dockerfile rather than a hosted demo — see `INSTRUCTIONS.md`.

## Design rationale

**Why this problem and this approach.** Systems & reliability, because the interesting
failure here isn't a control being bypassed — it's a control that *appears* to hold (the
authorization audit passes) while silently not meaning what everyone assumes it means. That's a
reliability failure wearing a security failure's clothes, and it seemed like a sharper thing to
demonstrate than a more conventional distributed-systems primitive. The approach — two
implementations run against the identical `--fault` and `--seed`, side by side — was chosen over
building one "correct" system and asserting it works, because the contrast *is* the evidence: a
reviewer can see the naive baseline actually fail, not take a claim of correctness on faith.

**What's non-obvious.** The separation of claim 1 (agents specifically break the classical
idempotency assumption) from claim 2 (TOCTOU on approval, which is general and would bite a
deterministic caller too) — bundling them is the imprecision most likely to cost credibility with
a reviewer who knows this space. The two-sided error story for semantic keys — over-merge is
prevented structurally, under-merge can only be detected, never blocked, because the system can't
distinguish an envelope violation from a legitimate second operation. And the delegation
generalization: the identity problem doesn't require a single agent calling a tool directly, it
requires *some* reasoning layer mediating a retry, which multi-agent delegation chains have just
as much as single-agent tool calls do.

**Key tradeoffs.** Depth over breadth: three demonstrated claims with real tests beat four where
one (the rare-interleaving-discovery claim) would have been asserted, not shown — so it was cut
rather than left as an unsupported fourth claim. Coverage over completeness on the fault taxonomy:
one fully-wired injection point with a real test suite beats eight partially-stubbed ones. A CLI
comparison over a browser UI: the side-by-side terminal output carries the whole argument, and a
browser control (or a static HTML render of it, tried and then cut — see Scope) would have been
polish spent on the least load-bearing part of the demo. Zip + Docker over a hosted demo: no
dependency on the repo's visibility or any hosting availability.

**How I'd extend it with more time.** Build the actual discovery mode for the cut fourth claim —
randomized fault/timing schedules searching for bad interleavings instead of a curated menu of
known ones, with the seed that found something interesting becoming the reproducible regression
test. Wire up the remaining seven named injection points so faults can land anywhere in the
pipeline, not just at the downstream call. Turn the delegation argument from analysis into an
actual two-hop fixture (a simulated A1 regenerating its instruction to a simulated A2) instead of
prose. Add a real GitHub adapter behind a feature flag so the fake downstream's assumptions get
checked against the real API's actual idempotency behavior (or lack of it).

**Time spent.** *(fill in actual hours before submitting)*

---

A personal project exploring approval reliability for agentic tool calls.

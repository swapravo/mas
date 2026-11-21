# Enterprise layer: notes (Step 0)

Written after reading `harness.py`, `tasks.py`, `celery_app.py`, `config.py`, `schema.sql`, `seed.py`,
`docker-compose.yml`, `docs/*.md`, `tools.py`, `redaction.py`, `prompts.py`, `service.py` (skimmed) and
`tests/code-shipper-pack/` (orchestrator prompt, agents, simulation files). Everything below was checked in the
code, not taken from the task description.

## What I will reuse

| Existing piece | Where it lives | How the Enterprise layer uses it |
|---|---|---|
| Agent loop (one JSON action per turn, validate, guard, run, feed back) | `harness.Agent` | Every department agent is a `harness.Agent`. No new loop. |
| Tool registry + JSON-schema argument check | `harness.ToolRegistry`, `validate_args` | Department tools are registered the same way. The **handoff envelope** is also checked with `validate_args`. |
| Guards (hooks that can deny a call) | `harness.allow_only`, `max_calls`, the `Guard` type | Used inside each agent run. The cross-department allow-list is enforced one level out, in the gateway (see below). |
| `ScriptedLLM` (callable form) | `harness.ScriptedLLM` | Department "models" are scripted policies. I wrap them to add virtual latency and a seeded failure rate. |
| `LLMUnavailable` | `harness` | The failure the wrapper raises. Departments treat it as retryable, like `tasks.run_case` does. |
| Checkpoints / event sink | `Agent(checkpoint=, on_event=)` | `on_event` feeds the audit log. Checkpoints are written for the orchestrator's task state (database), not per agent step. |
| Draft, then approve | `tools.draft_reminder`, `message_draft`, `docs/how-build-plan.md` section 5 | Collections never sends: its reply is a draft that goes to the approval queue. |
| Idempotency key per write | `message_draft.idempotency_key UNIQUE` | Same idea, one key per task step (`task_id:step`), kept in a key-value store (Redis `SET NX` shape). |
| RLS with a role per actor, deny by default | `schema.sql` (`property_scope` policies, `mas_*` roles, owner bypasses RLS) | `enterprise/schema.sql`: one role per department, a policy on the owning department. Same pattern, different key. |
| Insert-only audit log | `audit_log` + triggers | Same triggers on `ent_audit`. Every gateway call, allowed or refused, is written. |
| Redis + Celery, `acks_late`, retry with backoff | `celery_app.py`, `tasks.py` | The live demo uses the same Celery settings with **one queue per department**. |
| Names and properties | `seed.PROPERTIES`, `seed.RESIDENTS` | Workload wraps them (see below). |
| `redaction.redact` | `redaction.py` | Resident text is redacted before an agent sees it. |
| Orchestrator rules | `tests/code-shipper-pack/orchestrator/orchestrator_prompt.md` | Used as the **specification** for arbitration: legal/security non-negotiables win, money escalates with a decision card, record every overruled position, `decided_by` is `rule`, `human` (or `negotiation`), retry a failed agent then escalate if it owns a non-negotiable domain. That pack is LLM prompts; my orchestrators are plain code. |

## What I will not reuse, and why

- **`tools.build_registry` and the real Collections tools.** They need Postgres 18 (`uuidv7()` in `schema.sql`), a
  property-scoped RLS session, and the repo clock. This sandbox has Postgres 16 and the tests must run with no
  database. So the Enterprise Collections department is a small scripted agent on the same harness, following the same
  rules (reads, drafts, never sends). The existing Collections agent is untouched. **Assumption: this counts as
  "reuse where practical".**
- **`tasks.py` / `celery_app.py` as-is.** They have one default queue and a 09:00-21:00 window tied to Collections. I add
  `enterprise/celery_app.py` beside them and do not edit them.
- **`seed.py` as a DB seeder.** Importing it pulls in `database` (psycopg) and it needs a live database. Workload imports
  only its name lists, with a small fallback if the import fails.

## Design decisions made now

1. **Deterministic by construction.** Everything runs on a virtual clock (a small discrete-event kernel). No wall clock,
   no `hash()`, seeded `random.Random` per component. This is what makes p50/p95, "who noticed" and "same seed, same
   result" testable offline.
2. **Transport is swappable.** Orchestrated modes use per-department queues (named `dept.<name>`, like the Celery
   queues). Choreography uses an `EventLog` interface with an in-memory backend and a real Redis Streams backend
   (`XADD / XREADGROUP / XACK`). Offline tests use `fakeredis` and assert both backends give identical results.
3. **Store is swappable.** `MemoryStore` emulates the RLS rule (deny by default, owner bypasses) so tests need no
   Postgres. `PgStore` runs the real policies. Boundary tests run on both when Postgres is reachable.
4. **Data model: one table, `ent_record(dept, kind, key, payload)`**, with RLS `dept = this role's department`. Teaching
   value: one rule, easy to see.
5. **The gateway is the only door for tools.** Tools run with the role of the department that owns them. The caller's
   allow-list, and the on-behalf-of scope (the person a request is really for), are checked in the gateway.
6. **Choreography still needs a case owner.** Collections holds the join and runs the arbitration there. That is a
   finding to show students, not a flaw I am hiding.
7. **Envelope has extra fields beyond the 11 required** (`step`, `kind`, `payload`) because a handoff must say what to
   do. The 11 required fields are all present and validated. `budget` is the task's total allowance of agent calls; spend is counted in the key-value store so
   every mode enforces it the same way.

## Assumptions I could not verify

- **"Existing tests still pass"** means: baseline is 16 passed, 41 skipped (the skipped tests need Postgres 18).
  Before I `pip install`-ed `psycopg` the three DB test files failed at collection; that is pre-existing, not caused by me.
- `uuidv7()` is Postgres 18 only. I have not run `schema.sql` (the 18 one) here. My own `enterprise/schema.sql` avoids it
  and is tested on Postgres 16.
- Real Celery workers and real Redis: available in this sandbox (Redis 7 on port 6380, Postgres 16). I could not confirm
  that they exist in the class environment beyond `docker-compose.yml`.
- A scripted "gullible" model stands in for prompt injection. A real model might resist the text. The point of the demo is
  the blast radius when it does not.
- Rupees vs paise: the existing schema stores paise. Enterprise uses rupees throughout; no conversion is needed unless the
  two are joined later.

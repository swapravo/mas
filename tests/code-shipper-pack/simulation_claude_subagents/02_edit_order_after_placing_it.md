# Simulation 02: Edit order after placing it

Agents on this task: Frontend, Backend, Database, QA (from the problem file).
Not on this task: Security, Infra. See Open risks.

## Request

Feature request: Let customers change the delivery address or add and remove items after they place an order. Payment is taken at checkout. Orders are pushed to the warehouse system every 10 minutes. Warehouse staff can edit the same order from their own screen (for example to substitute out-of-stock items). Today customers phone support to make changes.

hard_constraints: API host 30s cut; small team, 2-week deadline; GDPR applies; no real personal data in tests; budget-conscious.

## DIG

One-sentence restatement: Customers can self-serve edit the address and line items of an already-paid order, safely alongside warehouse edits and the 10-minute warehouse push.

hidden_problem: An order is not one record with one owner. A customer, a warehouse employee and a 10-minute batch job can all change the same order, and the warehouse may already have a copy. If the customer edit lands after the push, or collides with a warehouse substitution, the warehouse ships something different from what the customer was told. The customer paid a fixed amount, so any item change also makes the paid total wrong, and a silent overwrite (last write wins) loses a substitution or a customer change without anyone noticing. A secondary problem: an old address is personal data, so edit history must be kept for dispute purposes but not forever.

Checks run: state change (yes, order state moves while edited), two actors (yes: customer and warehouse, plus the batch job), scale (unknown, small team, assume modest), sensitive data (address, name: GDPR), retries and duplicates (double submit of an edit would double-charge or double-add), who else is affected (warehouse staff, support, finance).

assumptions:
1. Order states exist roughly as: placed, sent_to_warehouse, being_picked, shipped, cancelled. Real names to be confirmed by Backend/Database.
2. Our system is the source of truth until the order is pushed; after the push the warehouse system holds a copy and may change it.
3. The push is one-way (us to warehouse) and there is a way to send an update or cancel for an already-pushed order. Unconfirmed.
4. Warehouse staff edits are written back into our database (or we can read them). Unconfirmed.
5. Payment provider supports partial refund and an extra charge on the saved method. Unconfirmed; money flows are out of the team's authority to decide.
6. Customers are logged in and can only see their own orders; an auth layer exists.
7. Support currently handles edits by phone, so there is an existing manual path to fall back on.
8. Test environments can be seeded with synthetic data only.

## Proposals

Each written from the role only, without reference to the others.

### Frontend

```json
{
  "position": "Show an Edit order screen only while the order is editable, with a draft-then-confirm flow: the customer changes address or items, sees a summary including any price difference, then taps one Confirm button that disables itself while saving. After save, show the server's returned full order, and if the server says the order is locked or changed, show a clear message with a 'contact support' fallback.",
  "reason": "The user must always see a state (saving, saved, locked, conflict, error) and must never see an order that differs from what the server holds. Double-clicks and slow or offline connections are the likely failure when the API host cuts at 30s.",
  "cost_to_others": "Backend must return the full updated order in every edit response, with a version number and an editable flag, plus machine-readable error codes. Backend must also give a status endpoint if saving is async. This adds response fields and error codes to the contract.",
  "non_negotiables": [
    "[correctness] No silent failure: every edit ends in a visible state (saved, locked, conflict, failed, still processing).",
    "[correctness] Screen only shows data returned by the server after save; no optimistic display of an edit the server has not confirmed.",
    "[correctness] Confirm button is single-fire: double-click or retry sends the same idempotency key, never two edits.",
    "[safety/legal] Never display another customer's order or raw server errors; address is only shown to its owner."
  ],
  "acceptable_fallbacks": [
    "Address-only edit in release 1, item edits in release 2 if time is short.",
    "Poll a status endpoint every few seconds instead of live updates.",
    "Replace the editable screen with a 'Request a change' form that creates a support ticket for locked orders."
  ],
  "assumptions": [
    "There is an existing order detail screen and customer login.",
    "A product catalog endpoint exists that returns price and availability.",
    "Mobile small screens must be supported (tap targets, keyboard, screen reader labels)."
  ],
  "questions": [
    "Backend: what are the error codes for locked, version conflict, out of stock, price changed? What is the max items per order?",
    "Backend: how fresh is the 'editable' flag; can it be stale by the time the user taps Confirm?",
    "Orchestrator/human: if the new total is higher or lower, what does the customer see and when are they charged or refunded?",
    "Backend: how long can an edit take; will it ever exceed the 30s host cut?"
  ],
  "confidence": 0.75
}
```

### Backend

```json
{
  "position": "Add a PATCH /orders/{id} edit endpoint that checks order state server-side: edits are allowed only while the order has not been claimed for the warehouse push. Require an Idempotency-Key and an If-Match version, apply the change in one transaction, and mark the order 'locked' atomically when the push job claims it. Anything after that goes through a 'change request' sent to warehouse or support, not a direct edit.",
  "reason": "Two actors plus a batch job can touch the same order. State rules must live on the server, writes must be safe to repeat, and the push job needs a claim so it never sends a half-edited order. The warehouse system is the source of truth after the push, so we must not pretend to overwrite its copy.",
  "cost_to_others": "Frontend must handle 409 conflict and 423 locked responses and send an idempotency key and version. Database needs a version column, a lock/claim state and an idempotency table. QA gets more state combinations to test. Price differences require a payment step that is a side effect with partial-failure risk.",
  "non_negotiables": [
    "[correctness] State rules enforced on the server: edit allowed only in states placed (unclaimed); never trust the client.",
    "[correctness] Every edit is idempotent: the same Idempotency-Key returns the same result and never double-applies or double-charges.",
    "[correctness] Optimistic version check: a stale version returns 409, never a silent overwrite of a warehouse edit.",
    "[correctness] Push job claims orders atomically so an order is never pushed and edited simultaneously.",
    "[correctness] Errors are explicit; never an empty success. A payment step that fails halfway must roll the order back to its previous contents or mark it for compensation.",
    "[constraint] Any step that can exceed 30s (payment adjustment, warehouse recall) runs as an async job with a status endpoint; the edit request returns 202 quickly."
  ],
  "acceptable_fallbacks": [
    "Edit window closes when the order is created plus a fixed time (for example 5 minutes) if claim-tracking proves too complex for 2 weeks.",
    "Address-only edits allowed after the push through a warehouse update message; item edits only before the push.",
    "No direct edit after push; create a support ticket with the requested change pre-filled."
  ],
  "assumptions": [
    "We control the push job code and can add a claim step.",
    "The warehouse system has some way to receive an update or cancel for a pushed order (unconfirmed).",
    "Payment provider supports idempotent refund and extra-charge calls.",
    "Warehouse staff edits are visible to our system, either through a write-back or a read endpoint."
  ],
  "questions": [
    "Human: when the total changes, do we refund automatically, charge extra, or block edits that raise the price? Money decision.",
    "Database: can the order table take a version column and a lock state without downtime?",
    "Security: who may edit (owner only, support on behalf?), and what must be logged?",
    "Infra: how is the push job scheduled and can two runs overlap? What is the expected order volume?",
    "Frontend: can the screen cope with 202 plus polling?"
  ],
  "confidence": 0.7
}
```

### Database

```json
{
  "position": "Keep the orders table as the current truth, add an integer version column, a nullable locked_at/claimed_by marker, and an append-only order_revision table that stores each edit (who, when, before/after JSON, source: customer or warehouse). Add an idempotency_keys table with a unique constraint and a 24-hour expiry, and a check constraint that blocks line-item changes when the order is not in an editable state.",
  "reason": "Integrity must hold even if code has a bug: unique keys stop double-applies, the version column stops lost updates, and the revision table gives support and finance a record of what changed and why. Edits and warehouse writes touch the same rows, so we need clear concurrency control.",
  "cost_to_others": "Backend must write the revision row in the same transaction as the edit. Migration adds columns and tables, so Backend and QA must work against the new schema. The revision table adds storage that needs a retention job, which is extra work in a 2-week window.",
  "non_negotiables": [
    "[correctness] Integrity in the database: unique constraint on (order_id, idempotency_key); foreign keys from revisions and items to orders; check on valid state values.",
    "[correctness] Version column compared in the UPDATE (WHERE version = :v) so a stale write affects zero rows; never rely on app-level checks alone.",
    "[correctness] Edit and revision row in one transaction; both succeed or neither does.",
    "[safety/legal] Revision rows holding old addresses have a retention limit and are erasable or anonymisable on a GDPR request, while keeping order totals for accounting.",
    "[constraint] Every migration is backward compatible, safe to run while live, and has a rollback script; no full table scan at request rate (index on order_id, state, and the push job's claim query)."
  ],
  "acceptable_fallbacks": [
    "Store revisions as a JSON column on the order row in release 1 and split to a table later.",
    "Skip the revision table and rely on a generic audit log if one already exists.",
    "Row lock (SELECT ... FOR UPDATE) instead of version column if the ORM makes versioning hard, accepting more lock waiting."
  ],
  "assumptions": [
    "A relational database with transactions is in use.",
    "Order volume is modest (low thousands per day); the push job queries by state and updated_at.",
    "No read replica is used for the edit read-before-write path.",
    "Existing orders can get a default version = 1 without rewriting the table."
  ],
  "questions": [
    "Security/human: how long must old addresses and revisions be kept, and for what purpose (disputes, accounting)?",
    "Backend: does the warehouse write back into our database, or is its copy separate? Which fields does it own?",
    "Infra: expected volume and table size, and migration window?",
    "Backend: is there an existing audit log we should reuse?"
  ],
  "confidence": 0.7
}
```

### QA

```json
{
  "position": "Test strategy is built around the hidden problem: a set of race and duplicate tests that fail if an edit and the warehouse push or warehouse edit collide, plus a contract test per interface and a kill-switch test. All test data is synthetic. I will mark the change not shippable if any listed must-pass test fails.",
  "reason": "The risk is an order shipped differently from what the customer was told, or a customer charged twice. These cannot be found by a happy-path test, so each gets a test that deterministically fails if the problem returns.",
  "cost_to_others": "Backend must expose a way to trigger the push job on demand and to inject a clock or delay so races are repeatable. Frontend needs stable test ids and error states. Database needs seed and reset scripts. A feature flag is required so rollback can be tested.",
  "non_negotiables": [
    "[correctness] Duplicate: sending the same edit twice with the same Idempotency-Key results in one change and one payment adjustment.",
    "[correctness] Push race: an edit that arrives while the push job has claimed the order is rejected with a locked error, and the warehouse receives exactly the contents that were claimed, never a mix.",
    "[correctness] Concurrent edit: a customer edit with a stale version after a warehouse substitution returns 409 and the substitution is still present.",
    "[correctness] Stale screen: the order shown after save equals the server's order; a late response from an earlier request never overwrites a newer one.",
    "[correctness] Total: after any item edit, paid total, refund or extra charge and items agree to the cent; a failed payment step leaves the original order unchanged.",
    "[safety/legal] Identity: customer A cannot read or edit customer B's order (IDOR test); no real personal data exists in any fixture, log or screenshot.",
    "[constraint] Slow path: an edit whose payment step takes over 30s still returns a quick 202 and ends in a visible final state.",
    "[correctness] Rollback: turning the feature flag off hides the screen, rejects new edits with a clear message, and leaves existing edited orders intact; a migration rollback is tested on a copy."
  ],
  "acceptable_fallbacks": [
    "Race tests at the service level with a fake clock if end-to-end timing is too flaky.",
    "Manual exploratory pass for accessibility if automated tools are not in place, with automated tests for the rest.",
    "Reduced scope (address-only) with the same must-pass tests for that scope."
  ],
  "assumptions": [
    "A staging environment and a fake or sandbox payment and warehouse endpoint exist.",
    "Unicode and long addresses, time-zone handling for the push window, and empty carts must be covered.",
    "Order states and error codes will be fixed in the contract before tests are written."
  ],
  "questions": [
    "Backend: exact state names, error codes and the claim mechanism; can the push job be triggered in tests?",
    "Backend/human: what happens to the total when items change? I need the exact rule to write the money tests.",
    "Frontend: what states does the edit screen expose for test hooks?",
    "Database: can we seed and reset synthetic orders, and what is the rollback procedure?"
  ],
  "confidence": 0.8
}
```

## Conflict map

Dials are listed where options differ. "Agreed" means all four align.

| # | Dial | Frontend | Backend | Database | QA | Status |
|---|------|----------|---------|----------|----|--------|
| 1 | When an order is editable | Whenever server says editable flag is true | Only until the push job claims it; later changes become change requests | Check constraint on state; claim marker column | Test that edit during claim is rejected | Mostly agreed; detail in conflict (D1) |
| 2 | Collision with warehouse edits | Show conflict message, offer reload | 409 on stale version, no overwrite | Version column in UPDATE WHERE | Warehouse substitution must survive | Agreed on mechanism; who wins is open (D2) |
| 3 | Price difference on item edit | Show summary and ask to confirm | Payment adjustment as async side effect; asks human | Not stated | Money tests need an exact rule | CONFLICT / unresolved (D3) |
| 4 | Save is sync vs async | Wants a result on the screen without a second call; accepts polling as fallback | Async job + 202 for anything possibly over 30s | Not stated | Requires 202 under slow path | Conflict, minor (D4) |
| 5 | Edit history storage | Not stated | Wants compensation and audit | Separate revision table (fallback JSON column) | Not stated | Conflict only on form (D5) |
| 6 | History retention and GDPR | Never show other user's data | Asks Security for logging rules | Retention limit and erasable revisions are a non-negotiable | Test data is synthetic | Needs human input (D6) |
| 7 | Idempotency storage | Sends one key per Confirm | Requires header, same result on repeat | Unique constraint, 24h expiry | Duplicate test | Agreed; TTL detail (D7) |
| 8 | Scope and cut | Address-only fallback | Address-only after push, or fixed window fallback | JSON column fallback | Reduced scope with same tests | Agreed fallbacks overlap (D8) |
| 9 | Rollback / kill switch | Hide screen when flag off | Not stated | Migration rollback required | Flag required, tested | Agreed, gap to fill (D9) |
| 10 | Response payload | Full order, version, editable flag, error codes | Returns result / 202 | Not stated | Contract test per interface | Agreed (no conflict) |

### Resolutions

Resolution order used: (a) security/legal/data-protection non-negotiables, (b) the requirement, (c) hard constraints, (d) smallest change that meets all stated non-negotiables, (e) easiest to undo. Negotiation round (max one) and escalation if still tied.

**D1. When an order is editable. Decided by rule (d).**
Frontend's "editable flag" and Backend's "until claimed" are compatible, since the flag is derived from the server claim state. Choice: the edit is allowed only while the order is in an unclaimed state; the push job claims atomically; the response always carries `editable` and `version`. After the claim, the customer gets a "request a change" path to support (the current phone route, now captured as a ticket). Why: the smallest change that satisfies Backend's claim, QA's push-race test and Frontend's always-a-state rule. Overruled: Backend's fixed 5-minute window fallback (not needed unless claim work slips).

**D2. Collision with warehouse edits. Decided by rule (b) and (d).**
Mechanism is agreed (version check in the UPDATE, 409 on stale). The open point is who wins: the requirement is customer self-service, but warehouse substitutions reflect physical stock reality. Choice: warehouse edits win; a customer edit against an out-of-date version gets 409, the screen shows the new order and asks the customer to redo the change. Why: a warehouse substitution cannot be undone by a customer click, and rejecting the stale edit is the easiest to undo. Overruled: nobody; Frontend's reload prompt is the way the 409 is shown. Open point: whether warehouse edits are visible to our system at all (assumption 4). Backend must confirm before build; if not, escalate (see Escalations E3).

**D3. Price difference on item edit. ESCALATE (E1).**
Involves money. No rule in the order settles it. Backend, Frontend and QA all depend on the answer.

**D4. Sync vs async save. Decided by rule (c).**
Hard constraint: 30s API host cut. Choice: edits that change only the address or that need no payment adjustment complete synchronously and return 200 with the full order; any edit needing a payment adjustment returns 202 plus a status endpoint, and Frontend polls. Why: the constraint decides it, and Frontend explicitly accepts polling as a fallback; the full-order payload is preserved on 200 and on the final status response. Overruled: Frontend's preference for always having the result on the first response.

**D5. Edit history storage. Decided by rule (d) and (e), with the GDPR link to D6.**
Choice: a separate append-only `order_revision` table (not JSON column), written in the same transaction as the edit, with a `purge_after` date column. Why: the retention and erasure non-negotiable (D6) is much easier on a separate table than inside a JSON column on the order row, and it is a small addition. Overruled: Database's JSON-column fallback (kept as a fallback if the schedule slips, but then it needs a documented erasure query).

**D6. History retention and GDPR. ESCALATE (E2).**
Personal data retention period is a legal and data-protection decision. Rule (a) says the Database non-negotiable (retention and erasure) wins over convenience, but the actual period is not ours to guess.

**D7. Idempotency storage. Decided by rule (d).**
Choice: unique (order_id, idempotency_key) in an `idempotency_keys` table, keyed response stored, 24h expiry, cleaned by a daily job. Frontend generates one key per Confirm press and reuses it on retry. Why: all four agree; 24h is Database's value and nobody objected.

**D8. Scope and cut. Decided by rule (c) and (d).**
With a 2-week deadline and a small team, release 1 is: address edits plus item add/remove while unclaimed, with the shared must-pass tests. Address-only is the agreed fallback of three agents if Day 5 shows the payment-adjustment work (D3) not decided or not built. Why: the requirement names both edits; the fallback is pre-agreed so nobody has to renegotiate.

**D9. Rollback / kill switch. Decided by rule (e).**
Choice: one server-side feature flag controlling the endpoint and the screen; migration rollback script for each migration; QA tests both. Gap: Backend did not mention the flag, so the flag is added to the Backend contract. Overruled: nobody.

**D10.** No conflict.

## Escalations

### E1. Decision card: price difference after an item edit (money)

Context: payment is taken at checkout. Adding items raises the total; removing items lowers it. Changing money after the fact touches the payment provider and finance.

Options:
- A. Refund automatically on removal; charge the saved method for the extra on addition (async job, confirm screen shows the amount first). Best for customers, most work and most risk (failed extra charge, partial refund).
- B. Refund automatically on removal; block additions that raise the total (customer places a new order instead). Smaller and safer.
- C. Items can be removed or swapped for same-or-lower value only, refunds handled by support for 2 weeks. Cheapest, least self-service.
- D. Address-only edits in release 1; item edits in release 2.

Impact: A adds payment failure handling and QA money tests; B and C reduce it; D removes it from the 2-week scope.
Recommendation from Orchestrator: B, with D as fallback if payment refund work is not done by Day 5. Reason: smallest change that satisfies the requirement, no new unpaid-charge risk, and easy to extend to A later.
Needed from human: pick A, B, C or D; confirm whether the payment provider supports partial refunds. Do not build item edits until answered.

### E2. Decision card: retention of old addresses and edit history (personal data)

Context: edit history keeps previous delivery addresses. GDPR applies.

Options:
- A. Keep revisions 90 days after delivery (for disputes), then anonymise the address fields; keep totals.
- B. Keep revisions as long as the order is kept for accounting, anonymise on erasure request.
- C. Do not store old addresses at all, only a "changed" marker and timestamp.

Impact: A needs a scheduled purge job (small); B needs an erasure query (small) but longer storage of personal data; C is simplest, but support can't see what the old address was.
Recommendation from Orchestrator: A or C; C if no one needs old addresses for disputes. This is a legal question for the data-protection owner, not the team.
Needed from human: pick a period or option, and name who confirms legal basis.

### E3. Decision card: do warehouse edits reach our system? (irreversible risk of overwriting stock decisions)

Context: warehouse staff edit the same order on their own screen. If their copy is separate and we never see the edit, our version check cannot protect their substitutions.
Needed from human/Backend: confirm how warehouse edits flow back and which fields they own. If they do not flow back, the choice is: (A) block customer edits once the order is pushed (the plan above already does) and make the warehouse the owner of the order after the push; or (B) build a write-back first (outside the 2-week scope). Orchestrator recommendation: A. Not a guess: confirm the fact.

### Missing agent

Security is not on this task although GDPR applies and personal data is involved. Orchestrator rule: a missing agent that owns a non-negotiable domain stops the work and escalates. Request to human: either add Security for a short review of the contract (who may edit, logging of changes, retention in E2), or confirm in writing that the human accepts that risk. Planning can continue; build of the endpoint and revision table should wait for that answer.

## Overruled positions

| Position | Who held it | Overruled by | Why |
|----------|-------------|--------------|-----|
| Fixed 5-minute edit window instead of claim | Backend (fallback) | Rule (d), D1 | Claim is a smaller change that matches the true push time. Kept as fallback if claim work slips. |
| Always return the finished order on the first response | Frontend (preference) | Rule (c), D4 | The 30s host cut means payment-adjusting edits must be async. |
| JSON revision column on the order row | Database (fallback) | Rule (a) link, D5 | A separate table makes retention and erasure easier. Fallback if schedule slips, with an erasure query. |
| Customer wins on collision | None stated (implicit in requirement) | Rule (b)/(d), D2 | Warehouse substitution reflects real stock, so the stale customer edit is rejected, not forced through. |
| Item edits that raise the total in release 1 | None (option A of E1) | Waiting on human, E1 | Not decided: money. Recommendation B. |

## Contract (summary for building after escalations)

- PATCH /orders/{id}: headers Idempotency-Key, If-Match (version). Body: address and/or items add/remove. Responses: 200 with full order (version, editable, items, totals), 202 with status URL, 409 version_conflict, 423 order_locked, 422 out_of_stock or invalid, 403/404 not your order, 429 rate limited. Errors always have a code and a plain message; no raw server errors.
- GET /orders/{id}/edit-status/{key}: queued, done, failed (with the same error shape).
- Owners: Backend owns state, claim and payment steps; Database owns orders, order_revision, idempotency_keys; Frontend owns the edit screen states; QA owns the must-pass test list and the not-shippable call.
- Feature flag: server-side, default off.

## Checks (one per hidden problem)

1. Duplicate: same Idempotency-Key sent twice yields one revision row and one payment adjustment.
2. Push race: edit arrives after the claim returns 423, and the pushed payload equals the claimed contents.
3. Stale value: customer edit with an old version after a warehouse substitution returns 409 and the substitution remains.
4. Late response: a slow earlier response never replaces a newer screen state.
5. Wrong total: after item edit, totals and refund/charge agree to the cent; a failed payment step leaves the order unchanged.
6. Wrong identity: customer A editing customer B's order returns 404/403.
7. GDPR: revisions older than the retention date are anonymised; erasure removes the old addresses; fixtures contain only synthetic data.
8. Rollback: flag off rejects new edits cleanly; migration rollback restores the old schema on a copy.

## Open risks

- Security agent absent while personal data is in scope (see Escalations).
- Assumption 3 and 4 (warehouse update path and write-back) are unconfirmed and could change the design.
- Payment provider capabilities unconfirmed.
- Infra not consulted: push job overlap, volume, and scheduling are unknown.
- 2-week deadline: if E1 or E2 stay unanswered past Day 3, fall back to address-only (D8).

# Simulation 04: Abandoned-cart email

Agents on this task: Backend, Database, Security, QA (from the problem file).
Each proposal below was written from that role's prompt only, before the conflict map.

## Request

feature_request: Send a reminder email 2 hours after a user leaves items in their cart without buying. Carts exist for logged-in users and for anonymous browsers (cookie). Users can buy on the website or in the app. Emails go through a third-party provider that bills per email. Customers are in India, the UK and the US. Some users have unsubscribed from marketing emails; there is a separate setting for order emails.

hard_constraints: API host 30s cut; small team, 2-week deadline; GDPR applies; no real personal data in tests; budget-conscious.

## DIG

Restated: after 2 hours of cart inactivity with no purchase on any channel, email the owner once, but only if they are allowed to be emailed.

hidden_problem: "Left the cart without buying" is a guess made from a clock, and the clock is read from one place while the truth lives in several. The user may have bought in the app, on another device or via a merged cart; they may have opted out; the cart may belong to an anonymous cookie with no known email. A timer-based sender can therefore email the wrong person, email someone who opted out, email someone who already bought, or email the same person twice. Every wrong send costs money and may break consent law.

assumptions:
1. Anonymous carts have no verified email, so they cannot be emailed unless an email was captured with consent.
2. Orders (web and app) land in one order store that is the source of truth for "bought".
3. The provider call can be slow or fail and the API host cuts requests at 30s, so sending must be a background job.
4. Marketing and order email preferences are stored per user as separate flags.
5. A cart reminder is treated as marketing (strictest reading) until a human says otherwise.
6. Provider idempotency-key support is unknown. Ask Backend/Infra.
7. Jobs can run twice (retries, two workers).
8. Users span three time zones; "2 hours" is elapsed time, not local clock time.

## Proposals

### Backend
```json
{
  "position": "Run a background sweeper every few minutes that claims carts idle for at least 2h, re-checks orders, consent and cart state on the server at send time, then enqueues one send job per claim. The send job calls the provider with a timeout well under 30s and an idempotency key built from cart_id and abandonment episode.",
  "reason": "The hidden problem is that 'abandoned' is derived, so every condition must be re-checked at the moment of sending, not when the timer was set. Sending is slow and can fail halfway, so it must be async and safe to repeat.",
  "cost_to_others": "Database must add an idle-cart index and a claim/send record. Security must expose consent checks as a server call. QA must test a time-driven job.",
  "non_negotiables": [
    "correctness: re-check 'no order on any channel' and 'cart unchanged' at send time against the order store",
    "correctness: one reminder per abandonment episode, enforced by a claim plus idempotency key",
    "correctness: provider errors are recorded and surfaced, never turned into a silent success",
    "constraint: no provider call inside a user-facing request (30s host cut)"
  ],
  "acceptable_fallbacks": [
    "Per-cart delayed job instead of a sweeper, if the queue supports cancel",
    "At-most-once sending if the provider has no idempotency key",
    "Skip anonymous carts entirely in version 1"
  ],
  "assumptions": [
    "A job queue and scheduler already exist",
    "Orders from web and app write to the same order store",
    "A cart has an updated_at that changes on any edit from any channel"
  ],
  "questions": [
    "Security: which preference flag gates this email, and who may trigger sends?",
    "Database: can the sweeper query be cheap at current cart volume?",
    "Infra/human: does the provider support idempotency keys, and what is the send rate limit?",
    "Human: do we want a minimum cart value to avoid paying for tiny carts?"
  ],
  "confidence": 0.8
}
```

### Database
```json
{
  "position": "Add a cart_reminders table with a UNIQUE constraint on (cart_id, episode_start) and a status column (claimed, sent, failed, skipped) plus reason. Add a partial index on carts(last_activity_at) for open carts so the sweeper never scans the whole table. Migration is additive and has a rollback.",
  "reason": "The duplicate-send risk must be blocked by the database, not only by code, because two workers can run at once. A time-range query over all carts at job rate is a full scan unless indexed.",
  "cost_to_others": "Backend must write a claim row in a transaction before sending. QA needs a seeded database for concurrency tests. Security's retention rule must fit the history I want to keep.",
  "non_negotiables": [
    "correctness: duplicate prevention enforced by a unique constraint, not only app code",
    "correctness: no full table scan by the sweeper; use an index on open carts",
    "correctness: 'bought' is read from the order store (primary), not a replica or cached copy, at send time",
    "constraint: migration is backward compatible, runs while live, and has a tested rollback"
  ],
  "acceptable_fallbacks": [
    "Keep claim state in Redis with a TTL if the table is rejected, with a unique-send log written afterwards",
    "Keep reminder history 12 months for audit; shorter if Security requires"
  ],
  "assumptions": [
    "Carts have a last-activity timestamp and a status column",
    "Cart volume is in the hundreds of thousands, not billions",
    "Logged-in and anonymous carts share one table with a nullable user_id"
  ],
  "questions": [
    "Backend: how is an abandonment episode identified when a cart is edited again after a send?",
    "Security: how long may send history be kept, and what must be erased when a user asks?",
    "Infra: cart count, growth and whether a read replica is used for the sweeper?"
  ],
  "confidence": 0.75
}
```

### Security
```json
{
  "position": "Send only to users with a known, verified email who currently have marketing email enabled, checked at send time. Never email anonymous cookie carts. Each email carries a working one-click unsubscribe, contains no more personal data than first name and item names, and links back to the cart through a signed, expiring link that does not log the user in.",
  "reason": "A reminder aimed at driving a purchase is marketing under GDPR/UK PECR. Sending without consent is a legal breach, and a guessable cart link could expose someone else's cart or let an attacker enumerate carts.",
  "cost_to_others": "Backend must read the marketing flag at send time, even if it delays a job. Database must store a consent snapshot and a suppression record. Fewer emails go out, which lowers recovered revenue.",
  "non_negotiables": [
    "safety/legal: no email to a user whose marketing flag is off at send time; the order-email flag does NOT authorise this email",
    "safety/legal: no email to anonymous or unverified addresses",
    "safety/legal: every email has a working unsubscribe, and unsubscribe takes effect before the next send",
    "safety/legal: resume link is signed, random, expiring (7 days max) and read-only until the user logs in",
    "safety/legal: logs and test data contain no raw email addresses; mask or hash",
    "safety/legal: erasure requests delete or anonymise reminder history"
  ],
  "acceptable_fallbacks": [
    "If counsel confirms existing customers fall under the 'soft opt-in', allow them with a clear opt-out, but only for the UK; keep EU/other jurisdictions on strict consent",
    "Keep only a hashed suppression record after erasure so the unsubscribe stays honoured"
  ],
  "assumptions": [
    "Marketing and order preferences are separate stored flags",
    "The provider is a data processor under a data-processing agreement",
    "Emails are not sent to minors"
  ],
  "questions": [
    "Human (legal): is a cart reminder marketing or transactional for each of India, UK and US? I am not sure; I am treating it as marketing until told otherwise.",
    "Backend: which fields go to the provider in the payload?",
    "Database: where are consent changes and unsubscribes recorded, with timestamps?"
  ],
  "confidence": 0.7
}
```

### QA
```json
{
  "position": "Test strategy: one failing-if-it-returns test per hidden problem (duplicate, already-bought, opted-out, stale/late, anonymous, timezone), run with a fake clock, a stub provider and synthetic users. Add a kill-switch test and one smoke test against the provider sandbox using allow-listed fake addresses.",
  "reason": "The risks are timing and identity risks, which only show up with controlled time, parallel workers and failing providers. Real addresses or live sending in tests would break the no-personal-data rule and cost money.",
  "cost_to_others": "Backend must inject a clock and a provider client so they can be faked. Database needs a repeatable seed set. Security's consent check must be callable from tests.",
  "non_negotiables": [
    "correctness: two workers claiming the same cart at once produce exactly one send",
    "correctness: user buys in the app at 1h59m; no email is sent",
    "correctness: user opts out between enqueue and send; no email is sent",
    "correctness: anonymous cart produces no email",
    "correctness: provider timeout or error yields a recorded failure and no duplicate on retry",
    "correctness: feature flag off means zero sends, verified in the test suite",
    "safety/legal: no real personal data in any fixture, log or snapshot",
    "correctness: 2h is measured in elapsed UTC time; users in India, UK and US all get it at +2h, across DST changes"
  ],
  "acceptable_fallbacks": [
    "Skip the live provider sandbox smoke test if it costs money, and rely on a contract test against a recorded stub",
    "Run the timezone test with three fixed zones rather than all"
  ],
  "assumptions": [
    "The clock and the provider client can be injected",
    "A staging environment and a provider sandbox exist"
  ],
  "questions": [
    "Backend: what are the exact error shapes and retry rules?",
    "Database: what is the shape of the reminder record I assert against?",
    "Security: what exactly is logged, so I can assert no personal data appears?"
  ],
  "confidence": 0.8
}
```

## Conflict map

Format: dial, each agent's option, status, resolution.

1. **Consent gate (which flag allows the email)**
   - Backend: eligible if not unsubscribed from marketing; asks Security which flag.
   - Database: stores whatever flag is chosen; wants a snapshot.
   - Security: marketing flag only, order flag does not count, checked at send time.
   - QA: test for opt-out between enqueue and send.
   - Status: mild conflict (Backend deferred, Security firm). RESOLVED by rule (a): Security's legal non-negotiable wins. Marketing flag, checked at send time. See Escalation E1 for the legal classification.

2. **Anonymous carts**
   - Backend: fallback to skip them in v1 (main position is silent on them).
   - Security: never email; no verified address.
   - QA: anonymous cart produces no email.
   - Database: nullable user_id, no position.
   - Status: agreed. Skip anonymous carts. No email capture feature in this scope.

3. **Trigger design (sweeper vs per-cart delayed job)**
   - Backend: sweeper every few minutes (fallback: delayed job).
   - Database: indexed sweeper query on open carts.
   - Status: agreed (sweeper), with the partial index required.

4. **Duplicate prevention (where enforced)**
   - Backend: claim plus idempotency key in app/queue.
   - Database: UNIQUE(cart_id, episode_start) in the database; Redis claim only as fallback.
   - QA: exactly one send under two workers.
   - Status: conflict on layer. RESOLVED by rule (d), smallest change satisfying every non-negotiable: both. The DB unique constraint is the guard; the idempotency key is the second layer. Database's Redis fallback is dropped (weaker).

5. **Failure and retry semantics when the provider is ambiguous (timeout)**
   - Backend: retry with idempotency key; fallback at-most-once if unsupported.
   - Database: row status failed/sent; wants no blind retry that could double-bill.
   - QA: no duplicate on retry.
   - Status: conflict only if the provider lacks idempotency keys, which is unknown. RESOLVED by rule (e) (easiest to undo): at-most-once by default; an ambiguous timeout is marked "unknown" and not auto-retried. Switch to retry only after the provider's idempotency support is confirmed.

6. **Definition of "abandoned" and the episode**
   - Backend: updated_at changes on any edit from any channel; re-check orders at send time.
   - Database: asks how an episode is identified; reads "bought" from the primary store.
   - QA: app purchase at 1h59m means no email.
   - Status: agreed in substance. Decision: episode = a new cart state after any change or any order; clock resets on any cart activity on any channel; "bought" read from the primary order store at send time, never a replica.

7. **Resume link contents**
   - Backend: (implied) deep link back to the cart.
   - Security: signed, random, expiring (max 7 days), read-only, no auto-login.
   - Status: conflict only on strictness. RESOLVED by rule (a): Security's version.

8. **Retention of send history**
   - Database: 12 months for audit (fallback: shorter if Security requires).
   - Security: minimal; erase or anonymise on request; hashed suppression record only.
   - Status: conflict. RESOLVED by rule (a): Security wins on data protection. Provisional 90 days for raw rows then anonymise; hashed suppression kept to honour unsubscribe. The exact period involves personal data, so it is flagged to E3.

9. **Test depth vs cost (provider sandbox)**
   - QA: one sandbox smoke test with fake addresses (fallback: recorded stub contract test).
   - Constraint: budget-conscious; no real personal data.
   - Status: conflict is small. RESOLVED by rule (c) then (d): stub contract test in CI (free); a single sandbox smoke run before release, only to allow-listed synthetic addresses, only if the sandbox is free. If the sandbox bills per email, drop it.

10. **Minimum cart value and daily spend cap**
    - Backend asks whether to skip small carts; the provider bills per email.
    - No agent can settle this and no rule decides it. Money and a users-vs-cost trade-off. ESCALATE (E2).

11. **Legal classification of the reminder (marketing vs transactional per jurisdiction)**
    - Security is not sure. A wrong guess is a legal breach. ESCALATE (E1). Interim: treat as marketing.

12. **Timing in time zones**
    - QA: 2h elapsed UTC; test India/UK/US and DST.
    - Backend: no conflicting position.
    - Status: agreed. No local quiet-hours in v1 (not a stated requirement; raise as an open risk).

Agreed with no conflict: async job only, no provider call in a request (30s cut); migration additive with rollback; consent checked at send time; logs mask email addresses; feature flag/kill switch with a test; synthetic test data only.

## Escalations

### E1. Is the cart reminder marketing or transactional?
- Why escalated: personal data and legal obligation; Security is not sure.
- Options:
  - A. Treat as marketing everywhere: send only with marketing opt-in. Safest; fewest emails.
  - B. UK soft opt-in for existing customers with a clear opt-out; strict consent elsewhere. More sends; needs counsel sign-off.
  - C. Treat as transactional (uses order-email flag). Not recommended; likely not compliant.
- Recommendation: A for launch. Move to B only after written legal advice.
- Needed from you: legal confirmation per country (India, UK, US/EU).
- Until answered: A is applied. This is the reversible, stricter choice.

### E2. Spend limits for per-email billing
- Why escalated: money and a users-vs-cost trade-off.
- Options:
  - A. Send for every eligible cart, no limit. Max recovery, unbounded cost.
  - B. Minimum cart value plus a daily send cap, with an alert at 80%.
  - C. Pilot on a percentage of users first.
- Recommendation: B plus C (pilot) for the first two weeks.
- Needed from you: the cart value threshold, the daily cap and the provider's price per email.
- Until answered: ship with the cap and threshold as config, default set to send nothing above the cap (fail closed).

### E3. Retention period for send history (smaller card)
- Why escalated: personal data.
- Options: 90 days then anonymise (Security) vs 12 months (Database).
- Recommendation: 90 days, with hashed suppression kept indefinitely.
- Needed from you: any legal or finance requirement to keep records longer.
- Until answered: 90 days is applied.

No negotiation round was needed; all conflicts settled by rule or escalated.

## Overruled positions

- Database, 12-month send history: overruled by rule (a); Security's minimal retention and erasure win. Provisional 90 days, see E3.
- Database, Redis-with-TTL claim as an alternative: dropped under rule (d); the unique constraint is the guard.
- Backend, retry on timeout with idempotency key as the default: overruled by rule (e); at-most-once until the provider's support is confirmed.
- Backend, "eligible if not unsubscribed from marketing" without a send-time recheck rule: tightened by Security under rule (a); recheck at send time is required.
- Security, soft-opt-in fallback for UK existing customers: not applied; pending E1.
- QA, live provider sandbox smoke test: conditional; dropped if it bills, rule (c).
- Backend, open question on skipping small carts: not decided; held in E2.
- No agent failed or was missing; Security was present, so no stop-and-escalate was triggered.

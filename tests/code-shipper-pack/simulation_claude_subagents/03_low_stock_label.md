# Simulation 03: Low-stock label

## Request
Feature request: Show "Only N left!" on product pages when stock drops below 10.
Facts given: ERP owns stock and syncs to our DB every 15 min; product pages are cached at the CDN for 5 min; popular items sell out within minutes in sales; Marketing wants it live for the next sale.
Agents: Frontend, Backend, Database, Infra.
hard_constraints: API host 30s cut; small team, 2-week deadline; GDPR applies; no real personal data in tests; budget-conscious.

## DIG (Orchestrator)
One-sentence restatement: show a scarcity label on product pages whenever the stock figure is below 10.

hidden_problem: The number on screen can be up to about 20 minutes old (15 min sync + 5 min CDN), while hot items sell out in minutes, so the label can say "Only 3 left!" for an item that is already sold out. That is a wrong, urgency-creating claim to shoppers (possible consumer-law exposure, not only a UX bug), and it is worst exactly during the sale Marketing cares about. A second risk: the label gets treated as stock truth at checkout.

assumptions:
1. The ERP is the only source of truth for stock; our DB holds a copy.
2. Checkout/cart already checks stock somewhere; not verified (ask Backend).
3. Stock is per product (not per variant/warehouse); not verified.
4. The label involves no personal data; logs and tests must stay free of customer data anyway (GDPR, no real personal data in tests).
5. Product pages are many, sale traffic is spiky; the API host cuts requests at 30s.
6. We cannot change the ERP sync interval (ERP is owned elsewhere); to be confirmed.
7. Orders placed on our site since the last sync are known to us.

## Proposals

### Frontend
{
  "position": "Strip the number from the cached page. After load, fetch a tiny stock-label endpoint and render the label only when the response is fresh; otherwise show nothing or a vague 'Low stock'. Reserve space for the label so the page does not jump.",
  "reason": "A cached page can show a number that does not match reality; the screen must never show data that does not match what the user asked for. The user must always see a defined state (loading, hidden, error).",
  "cost_to_others": "Backend must supply an as_of time and a state value. Infra must serve one extra small call per product page view. Marketing loses the number whenever data is too old.",
  "non_negotiables": [
    "correctness: never display a count older than an agreed max age; fall back to vague wording or hide",
    "correctness: out-of-order or late responses must not overwrite a newer one (ignore stale responses, cancel on navigation)",
    "safety/legal: no silent failure; on error the label is hidden and the page still works, no raw error text",
    "constraint: label is readable by screen readers (polite live region) and meets contrast"
  ],
  "acceptable_fallbacks": [
    "Inline value in the page HTML plus one client refresh, if the inline value carries as_of and is hidden when older than max age",
    "Vague 'Selling fast' wording instead of a number"
  ],
  "assumptions": ["Pages can run client-side JS after load", "Add-to-cart can show a 'sold out' error state"],
  "questions": [
    "Backend: what does the response contain, and what is the error shape?",
    "Backend/Infra: how fresh can the number be, in seconds?",
    "Human: is a vague label acceptable when data is old?"
  ],
  "confidence": 0.8
}

### Backend
{
  "position": "Add a read endpoint GET /products/{id}/stock-label that returns {state: in_stock|low|out, remaining, as_of, max_age_s}. The server applies the '< 10' rule from config. Compute remaining as the last ERP figure minus units our own orders took since that sync. Checkout still re-checks real stock and never trusts this label.",
  "reason": "State rules belong on the server. A copy of data from another system is never trusted, so we correct it with what we know locally and say how old it is. The label is display only.",
  "cost_to_others": "Database needs a cheap way to know units sold since sync. Frontend must handle three states plus age. Infra must size an endpoint that sees every product-page view.",
  "non_negotiables": [
    "correctness: checkout validates stock server-side; the label is never used to allow or deny a purchase",
    "correctness: errors are explicit (503 with code), never an empty success or a made-up number",
    "correctness: the threshold rule lives on the server in config, not in the client",
    "constraint: endpoint responds well inside the 30s host cut (target under 300 ms, no ERP call in the request path)"
  ],
  "acceptable_fallbacks": [
    "No local overlay: serve the last ERP figure with as_of, and let Frontend hide it when too old",
    "No-cache or short cache on the endpoint, whichever Infra can afford"
  ],
  "assumptions": [
    "Orders are written to our own DB with product and quantity",
    "Stock is per product",
    "The ERP is only reached by the sync job, not live from requests"
  ],
  "questions": [
    "DB: can 'units ordered since synced_at' be read in one indexed lookup?",
    "Infra: edge cache TTL we can afford on this endpoint?",
    "Human: do cancelled or unpaid orders return stock to the label count?"
  ],
  "confidence": 0.75
}

### Database
{
  "position": "Add a stock_snapshot table (product_id PK, erp_qty, synced_at, sold_since_sync) with a CHECK that quantities are not negative. The sync upserts only if the incoming synced_at is newer, and resets sold_since_sync in the same transaction. Orders increment sold_since_sync atomically.",
  "reason": "Integrity belongs in the database. A single-row read by primary key avoids scanning the orders table at request rate. The newer-wins guard stops an old sync batch overwriting fresher data, and running it twice is harmless.",
  "cost_to_others": "Backend must update the counter inside the order transaction (hot row contention on popular items). Infra must schedule the migration before the sale.",
  "non_negotiables": [
    "correctness: no full scan of the orders table on the label path; read by primary key only",
    "correctness: sync upsert is idempotent and ignores older batches",
    "correctness: CHECK constraint so the available figure cannot go negative; clamp at 0 on read",
    "constraint: migration is backward compatible, safe while live, and has a tested rollback"
  ],
  "acceptable_fallbacks": [
    "No overlay: store only erp_qty and synced_at (cheapest, simplest migration)",
    "Overlay via an indexed (product_id, created_at) query on orders if write-contention on the counter is a concern, limited to the last 20 minutes"
  ],
  "assumptions": [
    "The current table already has one row per product and the sync writes to it",
    "Order rows are available in the same database",
    "Test data will be synthetic only"
  ],
  "questions": [
    "Backend: is the order write already one transaction per order?",
    "Infra: peak orders per second on a single hot product?",
    "Backend: does the ERP figure already net out reserved or open orders?"
  ],
  "confidence": 0.7
}

### Infra
{
  "position": "Keep the 5-minute CDN cache on pages. Serve the label from its own endpoint with a short edge TTL (30 s) and request coalescing, behind a feature flag with a staged rollout. Do not purge pages per order. Add metrics for label age and endpoint latency, with an alert if the age grows past the limit.",
  "reason": "It stays on the current platform and budget, handles sale spikes through the edge cache, and can be switched off instantly. Purging on every order costs money and risks purge storms.",
  "cost_to_others": "Up to 30 s extra staleness on top of the data age, which Frontend and Backend must include in max_age. The label endpoint must send cache headers Backend must set.",
  "non_negotiables": [
    "constraint: stay inside the current host and budget; no new paid service for this feature",
    "constraint: every release can be rolled back with a flag, no deploy needed",
    "constraint: the feature is observable (latency, error rate, data age, request count) with a correlation id; logs contain no customer data",
    "correctness: sale-time load test before go-live (synthetic traffic only)"
  ],
  "acceptable_fallbacks": [
    "Edge TTL of 10 to 60 s",
    "If the endpoint struggles under load, fall back to the cached-page number with 'Low stock' wording only"
  ],
  "assumptions": [
    "The CDN supports per-path TTL and request coalescing",
    "Sale peak is a multiple of normal page traffic",
    "The 15-min sync job already runs reliably and is monitored"
  ],
  "questions": [
    "Backend: expected peak requests per second on the endpoint?",
    "DB: cost of the single-row lookup at that rate?",
    "Human: who can tell us the sync job is late or failed (ERP owner)?"
  ],
  "confidence": 0.8
}

## Conflict map

| # | Dial | Frontend | Backend | Database | Infra | Status |
|---|------|----------|---------|----------|-------|--------|
| 1 | Where the number comes from | Separate fetch; inline only as fallback with as_of | Separate endpoint | Neutral | Separate endpoint | Agreed (separate endpoint); Frontend's inline fallback overruled |
| 2 | Local overlay (ERP qty minus our orders) vs raw ERP figure | Neutral; wants max age | Overlay preferred, raw acceptable | Overlay with counter preferred, raw acceptable | Neutral, worries only about extra load | CONFLICT (cost/complexity vs truthfulness) |
| 3 | What to show when data is too old | Hide or vague wording | Return state and as_of, client decides | Neutral | Vague wording as fallback under load | Partly agreed; wording and max age in CONFLICT with the requirement ("Only N left!") |
| 4 | Edge cache TTL on the label endpoint | Wants as fresh as possible | No-cache or short | Neutral | 30 s (10-60 acceptable) | CONFLICT (freshness vs cost) |
| 5 | How the overlay is stored | Neutral | Counter updated in order transaction | Counter (or indexed 20-min query as fallback) | Neutral | CONFLICT inside the overlay option (counter vs indexed query); only matters if dial 2 says yes |
| 6 | Who applies the "< 10" rule | Server | Server (config) | Neutral | Neutral | Agreed: server |
| 7 | Sync upsert safety | Neutral | Agrees | Newer-wins, idempotent | Neutral | Agreed |
| 8 | Release | Neutral | Neutral | Migration with rollback | Feature flag, staged rollout | Agreed (both) |
| 9 | Observability | Neutral | Neutral | Neutral | Metrics for age, latency, alert | Agreed |
| 10 | Checkout re-checks stock; label never gates a purchase | Agrees | Non-negotiable | Agrees | Agrees | Agreed |

### Resolutions
- Dial 1: decided_by rule (b, the requirement, then d, smallest change). A number baked into a cached page can be 20+ min old, so it cannot satisfy "Only N left" truthfully. The separate endpoint is also the smallest change (one route, no purge logic). Overruled: Frontend's inline-number fallback (a preference, not a non-negotiable).
- Dial 3: decided_by rule (a/b). Frontend's correctness non-negotiable (never show a count older than max age) is upheld as policy. The exact max age and the vague wording are a user-vs-business trade-off, so they go into Escalation 1.
- Dial 4: decided_by rule (c, hard constraints: budget-conscious, host limits). 30 s edge TTL with coalescing. The 30 s is added into the declared max_age. Overruled: Backend's no-cache and Frontend's "as fresh as possible" (preferences; neither is a non-negotiable).
- Dial 5: not decided yet; follows Escalation 1. If the overlay is approved, default to DB's counter row (read by primary key, fits Database's no-scan non-negotiable); the indexed-query option is the fallback if Infra reports hot-row contention in the load test.
- Dial 2: no rule settles it. Overlay = more code, a migration and a write in the order path (rule d favours raw), but raw leaves a window where the label contradicts reality (rule b favours overlay, and consumer-protection risk is in rule a territory). ESCALATE.
- Dials 6-10: agreed, no resolution needed.

## Escalations

### Decision card 1: How accurate must the label be?
- Question: During a sale, may the label show a number that is out of date, and if so for how long?
- Why it is yours: it trades shopper trust and possible false-urgency (consumer-protection) exposure against cost and delivery time. No rule in our order settles it.
- Option A, raw ERP figure only (cheapest, about 3-4 days less work): label can be up to ~20 min old plus 30 s cache. Number shown only if the data is younger than a max age (suggest 20 min); otherwise "Low stock". During a fast sale it will often fall back to the vague wording.
- Option B, local overlay (recommended by Backend and Database): ERP figure minus what our own orders took since the last sync. Number stays accurate for on-site sales, so "Only N left!" works as Marketing expects. Costs an extra migration, an order-path write and about 3-4 extra days of the 2-week budget. Does not cover sales through other channels, if any.
- Option C, ship A first for the sale, B after: lowest risk to the deadline, but Marketing gets mostly vague wording in the first sale.
- Also needed: (1) the max age you are happy to show a number at; (2) approval of the vague fallback wording ("Low stock" or "Selling fast"); (3) legal/compliance view on showing a possibly outdated scarcity number to EU shoppers; (4) whether cancelled or unpaid orders should return stock to the label count.
- Orchestrator recommendation: B, with a flag so we can drop to A. Waiting for the answer. Not guessing.

### Decision card 2 (question to a human, not blocking build): ERP sync
- Can the ERP owner provide a push/webhook on stock change or a shorter sync interval for the sale window? If not, the plan above stands. If yes, max age drops and the fallback wording appears less often. No money is committed until the ERP owner replies.

## Overruled positions
- Frontend: inline number in cached page as fallback (preference). Overruled by dial 1: stale by up to 20+ min.
- Backend: no-cache on the label endpoint (preference). Overruled by dial 4: budget and host constraints; 30 s edge TTL used and included in max_age.
- Frontend: "as fresh as possible" (preference). Overruled by dial 4 for the same reason.
- Infra: "no overlay, raw only" as an implied cost-saver is NOT decided; held open in Escalation 1.
- Database: indexed 20-min query on orders as fallback is held in reserve, not chosen.
- Not overruled, kept as non-negotiables: Frontend never shows a count older than max age and no silent failure; Backend checkout re-check and explicit errors; Database no full scans, idempotent sync, migration rollback; Infra flag rollback, observability, budget.

## Checks (hidden problem tests)
1. Stale value: seed a product at 5, order 5 units in tests (synthetic customers), do not sync; label must read out/hidden, not "Only 5 left" (overlay on), or show "Low stock" once age exceeds max age (overlay off).
2. Late/out-of-order: delay response A, return response B first; screen must keep B.
3. Duplicate sync: replay the same and an older ERP batch; stored row unchanged.
4. Late response / host cut: endpoint p99 under load stays well below 30 s; with endpoint failing, the page renders and the label is hidden, no raw error.
5. Checkout never trusts label: order of an item shown "Only 3 left" while real stock is 0 is rejected with an explicit error.

## Open risks
- Hot-row contention on sold_since_sync for popular items (watch in load test).
- Sales through other channels not covered by the overlay.
- Per-variant stock or multi-warehouse (assumption 3) would change the model.
- Legal view on scarcity messaging pending.
- Sync job failure makes every number old; covered by max-age fallback and alert.

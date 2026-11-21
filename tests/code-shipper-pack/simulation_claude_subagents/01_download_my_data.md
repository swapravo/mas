# Simulation 01: Download my data

Agents: Frontend, Backend, Security (from the problem file). Orchestrator: this run.
Each proposal below was written from that role's prompt only, before reading the others.

## Request

**feature_request:** Add a "Download my data" button on the profile page that gives a user a copy of everything held on them (profile, addresses, orders, support chats). Facts from the problem: orders table about 3M rows; power users have 2,000+ orders; API host cuts requests at 30s; files would sit in the same bucket as product images; support chats include staff-written messages; about 20% of users are in the EU.

**hard_constraints:** API host 30s cut; small team, 2-week deadline; GDPR applies; no real personal data in tests; budget-conscious.

## DIG

**One-sentence restatement:** Let a signed-in user export all their personal data from the profile page.

**hidden_problem:** This is a legal data-access export, not a convenience download. It is slow (cannot finish in 30s), so it must be a background job, and its output is a file full of one person's personal data. If that file lands in the shared product-image bucket (usually public or CDN-served) it can be read by anyone with the link, so the real problem is producing a large, sensitive file asynchronously and delivering it only to the verified owner, once, for a short time. Related risks: staff-written chat text may contain other people's data or internal notes; a double click or retry could create duplicate exports (duplicate copies of personal data); a stale export could show data that no longer matches what we hold; a hijacked session could pull a full data dump.

**assumptions:**
1. The profile page already requires a signed-in session; identity today is session-only (not confirmed).
2. A job queue and worker exist or can be added cheaply (to confirm with Backend).
3. The product-image bucket is public or CDN-fronted (to confirm with Security).
4. Staff messages are stored in the same chat table, with a sender role field (to confirm).
5. The GDPR one-month response window is met by an automated export; no manual review step.
6. Non-EU users get the same feature (one code path).
7. Tests use synthetic data only.
8. Orders for a user are readable by user_id with an index (to confirm with a DB owner; no DB agent on this task, so flagged as an open risk).

## Proposals

### Frontend

```json
{
  "position": "Clicking the button starts an export and immediately shows a clear state: 'Preparing your data', then 'Ready, download' or 'Failed, try again'. The button is disabled while a request is in progress, and the page restores the state if the user reloads or leaves.",
  "reason": "The export cannot finish in one request, so the user needs honest feedback over minutes. Without it they will click again, think it is broken, or see nothing on failure.",
  "cost_to_others": "Backend must give a status endpoint that returns the current state on page load, plus an error code and message the screen can show. Security's re-auth step adds a screen I must build.",
  "non_negotiables": [
    "No silent failure: every state (idle, preparing, ready, failed, expired) is visible and has text. (correctness)",
    "Double click or second tab must not start a second export; UI shows the existing one. (correctness)",
    "The screen only shows the signed-in user's own export status; no raw server errors or file paths displayed. (safety/legal)"
  ],
  "acceptable_fallbacks": [
    "Poll the status endpoint every 5-10s instead of push updates.",
    "Email 'your export is ready' as a second channel, as long as the page also shows the state.",
    "Ask for password or code again before starting, if Security requires it."
  ],
  "assumptions": [
    "The profile page can call a status endpoint on load.",
    "A typical export takes under a few minutes.",
    "Users on slow connections can still download a file of tens of MB."
  ],
  "questions": [
    "Backend: what are the status values and error codes? How long does the file stay available?",
    "Backend: what is the expected file size for a power user with 2,000+ orders?",
    "Security: is re-authentication required, and what does the user see if it fails?",
    "Security: may the page show a download link, or must the download go through a signed request?"
  ],
  "confidence": 0.8
}
```

Hidden problem as seen by Frontend: the user will not know whether anything is happening for minutes, so a stale or missing status looks like a failure and invites repeat clicks.

### Backend

```json
{
  "position": "POST /exports creates an async job and returns 202 with an export id. A worker builds a ZIP of JSON files, streaming orders in pages of a few thousand rows so memory stays flat. GET /exports/{id} returns the status. Only one active export per user; a repeat call returns the existing job (idempotent).",
  "reason": "3M orders and 2,000+ orders per power user cannot finish within the 30s host cut, so a job with a status endpoint is the only safe shape. Idempotency avoids duplicate work and duplicate files.",
  "cost_to_others": "Frontend must handle a waiting state and polling. Security's expiry and access rules need a cleanup job and a signed-URL step, which adds work for a two-week deadline.",
  "non_negotiables": [
    "Work above the 30s cut runs as a background job, never inside the request. (constraint)",
    "Creating an export is idempotent: one active job per user, enforced on the server. (correctness)",
    "Errors are explicit: a failed job reports 'failed' with a code, never an empty or partial file reported as success. (correctness)",
    "The user id comes from the session on the server, never from the request body. (safety/legal)"
  ],
  "acceptable_fallbacks": [
    "Reuse the existing bucket under a separate prefix, if Security accepts private access rules on that prefix (cheapest option).",
    "Keep the file for 7 days to cut regeneration cost, or shorter if Security insists.",
    "Send an email when ready, link goes to the app, not the file."
  ],
  "assumptions": [
    "A worker or queue can be added without a new vendor.",
    "Orders are queryable by user_id with an index.",
    "Job failures can be retried safely because the job writes to a new file each attempt and only marks ready at the end.",
    "Chat messages have a sender field so staff vs user text can be told apart."
  ],
  "questions": [
    "Security: which chat fields and staff content may be included? Which expiry and rate limits?",
    "Infra/DB (not on this task, so asking the Orchestrator): is there an index on orders.user_id and a queue already?",
    "Frontend: is polling acceptable for status?"
  ],
  "confidence": 0.75
}
```

Hidden problem as seen by Backend: a half-finished job (crash, timeout, retry) can leave a partial file or two files for one request, and the status must never say 'ready' for a partial export.

### Security

```json
{
  "position": "Store exports in a separate private bucket (no public or CDN access, encrypted at rest), deliver only through a short-lived signed URL issued after the server checks the session owns the export, and delete the file on expiry (24h, 72h at most). Require recent re-authentication before an export starts, rate-limit to a small number per day, and write an audit log entry (user id, time, export id, no content).",
  "reason": "The file is a complete personal-data dump. In the product-image bucket it would likely be reachable by link and mixed with public assets, so one misconfigured rule leaks every export. GDPR access requests also require we give the data only to the verified data subject.",
  "cost_to_others": "Backend needs a second bucket, signed-URL issuing, an expiry cleanup job and an audit log. Frontend needs a re-auth step and an 'expired, request again' state. Small extra cost and time.",
  "non_negotiables": [
    "Exports must not be stored in the product-image bucket or any public/CDN-served location. (safety/legal)",
    "Only the owning, authenticated user can start, view status of, or download an export; checked on the server every call. (safety/legal)",
    "Download links and files expire (maximum 72h) and the file is actually deleted afterwards. (safety/legal)",
    "Logs and audit trail must not contain export content or personal data beyond ids. (safety/legal)",
    "Rate limit on export creation to resist scraping and a hijacked session pulling repeated dumps. (safety/legal)",
    "Staff-written chat content: what is included must be decided by a human (legal), not by default. (safety/legal, unsure, needs a human)"
  ],
  "acceptable_fallbacks": [
    "Same cloud account, same bucket only if it is private, has a dedicated prefix with a deny-public policy, encryption and a lifecycle delete rule (and Security verifies this); a separate bucket is still preferred.",
    "Skip full re-authentication if the session is fresh (for example signed in within 15 minutes), otherwise ask again.",
    "Deliver through an authenticated download endpoint that streams the file, instead of a signed URL."
  ],
  "assumptions": [
    "The product-image bucket is public or CDN-fronted.",
    "A password or second factor exists for re-auth.",
    "Chat text may contain other people's data (staff names, other customers' details) and internal notes.",
    "The EU one-month deadline applies; non-EU users get the same treatment for simplicity."
  ],
  "questions": [
    "Backend: exact fields in each file? Are there internal notes or flags in the chat table?",
    "Human (legal/DPO): do staff messages go in the export, and are staff names redacted? Are internal notes excluded?",
    "Infra: how are bucket access and logs configured? Do logs capture request bodies?",
    "Human: should the export retention or user-visible history count as a stored copy needing its own deletion rule?"
  ],
  "confidence": 0.85
}
```

Hidden problem as seen by Security: the export file is the most sensitive object in the system, and the shared bucket plus a guessable or long-lived link would turn one user's request into a public leak. Also, staff messages mix the user's data with staff and third-party data.

## Conflict map

| # | Dial | Frontend | Backend | Security | Status |
|---|------|----------|---------|----------|--------|
| 1 | Sync vs async | Waiting state, status shown | Async job, 202 + status endpoint | Not stated (no objection) | Agreed (async) |
| 2 | Status updates | Polling OK, optional email | Polling | Not stated | Agreed (polling) |
| 3 | One export per user, double click | Server and UI block repeats | Idempotent, one active job | Rate limit per day | Agreed in principle; see dial 8 for the limit number |
| 4 | Storage location | Not stated | Same bucket under separate prefix (fallback) | Separate private bucket; same bucket only with strict controls | CONFLICT (Backend vs Security) |
| 5 | Download delivery | Wants a simple link on the page | Signed URL or email link to app | Signed short-lived URL after server ownership check, or authenticated stream | Mostly agreed; email wording differs (see 6) |
| 6 | Email notification content | Email as second channel | Email, link goes to app | Not stated; implied no personal data and no file link in email | Minor CONFLICT (email must not carry a file link; settled below) |
| 7 | File retention / expiry | Wants enough time to download; says nothing on length | 7 days (cost) | 24h, max 72h, and real deletion | CONFLICT (Backend vs Security) |
| 8 | Rate limit | Wants to retry after a failure | One active job only | Small number per day | CONFLICT (Frontend vs Security, minor) |
| 9 | Re-authentication before start | Accepts as fallback, dislikes friction | Not stated | Required (or fresh session) | CONFLICT (Frontend preference vs Security) |
| 10 | Staff-written chat content | Not stated | Include chats; asks Security | Human decision; assumes may need redaction | ESCALATE |
| 11 | Partial or failed export | Show failed state | Failed code, never partial as ready | Not stated | Agreed |
| 12 | Logging / audit | Never show raw errors | Not stated | Audit log, ids only, no content | Agreed (no one opposes) |

### Resolution

**Dial 4 Storage location.** Rule (a): Security non-negotiable (not in a public/CDN bucket) wins. Choice: a separate private bucket (or, only if Security verifies the same-account private prefix with deny-public policy, encryption and lifecycle delete, that is a fallback for the team to confirm). Decided by rule. Overruled: Backend's "same bucket, separate prefix" as default (cheapest option).

**Dial 7 Expiry.** Rule (a): maximum 72h is a Security non-negotiable. Choice: 24h file and link life, with a one-click "request again" (which costs one regeneration, within budget). Decided by rule. Overruled: Backend's 7 days.

**Dial 9 Re-authentication.** Rule (a). Choice: require a session younger than 15 minutes, otherwise re-authenticate before starting an export (Security's own fallback, which also lowers friction for Frontend). Decided by rule. Overruled: Frontend's "no extra step" preference.

**Dial 8 Rate limit.** Rule (a) then (d). Choice: one active job per user (Backend) plus at most 3 export requests per user per 24h; a failed job does not count against the limit (smallest change that satisfies Security and gives Frontend its retry). Decided by rule. Overruled: Frontend's unlimited retries after failure.

**Dial 6 Email.** Rule (a). Choice: email says only "your export is ready, sign in to download" with no file link and no personal data. Decided by rule. Overruled: any plan to email a direct file link.

**Dial 5 Delivery.** Rule (d): Security's signed URL, short life, issued only after the server verifies the session owns the export; the page shows a "Download" button that calls the server for a fresh URL each click. Decided by rule; no real conflict left.

**Dials 1, 2, 3, 11, 12** are agreed. No resolution needed.

## Escalations

### Decision card E1: Staff-written messages in the export

- **Question:** Do we include staff-written support messages in the user's export? Do we show staff names? Do we exclude internal notes?
- **Why it escalates:** It involves personal data and a legal judgment no rule settles (Security is unsure; Backend wants them in; including internal notes or other people's data could itself be a breach).
- **Options:**
  - A. Include all messages in the conversation, staff names replaced by "Support agent", internal notes excluded. Pros: meets the user's request, protects staff and third parties. Cons: needs a redaction pass and a field to tell internal notes from replies (assumed to exist).
  - B. Include only messages the user wrote. Pros: simplest, safest. Cons: may not satisfy the access right, since replies often contain the user's data.
  - C. Include everything unredacted. Pros: simplest. Cons: leaks staff identity and possible third-party data; not recommended.
- **Orchestrator recommendation:** A, subject to legal/DPO confirmation.
- **Blocks:** the chat file only. Profile, addresses and orders can be built meanwhile.
- **Answer:** pending (human). Do not guess.

### Decision card E2: Retention of the export and a possible deletion-request conflict

- **Question:** Do we keep a record that an export was made (audit log: id, time) beyond the 24h file life, and for how long?
- **Why it escalates:** Personal-data retention with a legal rule; no rule in the resolution order decides the duration.
- **Options:** A. Keep audit entries 12 months (ids only). B. 90 days. C. Do not keep.
- **Orchestrator recommendation:** A or B, whichever legal states; default B until answered.
- **Answer:** pending (human).

(Money check: the second bucket and lifecycle rule cost little; not escalated. The 3-per-day limit is a user-versus-cost trade-off that Security's rule settles, so it is not escalated.)

## Overruled positions

| Dial | Who | Position | Overruled by | Reason |
|------|-----|----------|--------------|--------|
| 4 | Backend | Store in the existing bucket under a prefix | Security non-negotiable (rule a) | A shared, likely public bucket risks exposing every export. |
| 7 | Backend | Keep files 7 days | Security non-negotiable (rule a) | Expiry must be 72h or less; 24h chosen. |
| 9 | Frontend | No extra authentication step | Security non-negotiable (rule a) | A hijacked session must not be able to pull a full data dump. |
| 8 | Frontend | Unlimited retries | Security rate limit (rule a), softened by (d) | Failed jobs do not count, so honest retries still work. |
| 6 | Frontend/Backend | Email may link toward the file | Security (rule a) | Email carries no file link or personal data. |

## Contract (short form)

- POST /exports (session auth, recent-auth check) returns 202 {export_id, status}. If one is active, returns 200 with the existing one. Over limit: 429 {code: "RATE_LIMITED", retry_after}. Stale session: 401 {code: "REAUTH_REQUIRED"}.
- GET /exports/{id} returns {status: preparing|ready|failed|expired, expires_at, error_code?}. Other users' ids return 404.
- POST /exports/{id}/download-link returns {url} valid 5 minutes, only when status is ready.
- Files: ZIP of JSON, private bucket, deleted at 24h; job writes to a new key per attempt and flips to ready only after the final write.
- Owners: Backend owns the export record and files; Security owns the bucket policy, audit log and rate limit rules; Frontend owns UI states.

## Checks (each fails if the hidden problem returns)

1. Double click or two tabs: only one export record exists. (duplicate)
2. Download with another user's session or id: 404 and no URL. (leak)
3. Fetch the file from a public or CDN path and from an expired URL: both denied; file deleted after 24h. (exposure)
4. A job killed halfway never shows "ready" and leaves no downloadable partial file. (partial)
5. A synthetic user with 2,000+ orders exports without any single request over 30s. (timeout)
6. Export content then data change: new export reflects the change. (stale)
7. Logs and the email contain no personal data or file link. (log/email leak)
8. Chat file contains no internal notes or staff real names (after E1). (third-party leak)

## Open risks

- No Database or Infra agent on this task: index on orders.user_id, queue availability and bucket configuration are unverified assumptions.
- E1 and E2 are unanswered; chat export blocked until a human decides.
- Two-week deadline: the second bucket, expiry cleanup and audit log add scope; if time is short, ship profile, addresses and orders first.
- Backups and search indexes may hold copies of the exported data; not covered here.

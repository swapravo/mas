# Simulation 05: Sign in with Google

Agents on this task: Frontend, Backend, Security, QA (from the problem file).

## Request

feature_request: Add a "Sign in with Google" button to the login and signup screens. Today users sign up with email and password, and verifying the email is optional. About 40,000 existing users; some have a Google account with the same email as their existing account. Support wants fewer "forgot password" tickets.

hard_constraints: API host 30s cut; small team, 2-week deadline; GDPR applies; no real personal data in tests; budget-conscious.

## DIG

One-sentence restatement: let users log in or sign up through Google, and decide what happens when the Google email matches an existing password account.

hidden_problem: Email is not proof of identity here, because verification is optional. If Google sign-in simply "matches by email and merges", then (a) an attacker who earlier registered a victim's email with a password, never verified, keeps working password access after the real owner signs in with Google and the accounts merge (pre-hijacking), and (b) a Google identity whose email is not verified, or a recycled or changed email, can take over an existing account. Related: first-time sign-in can race (two tabs or a double click) and create two accounts for one person, and the callback does a call to Google that may hit the 30s host cut.

assumptions:
1. Existing accounts have email unique per account, but a large share are unverified (unknown number; ask Backend/DB).
2. We use Google's standard OpenID Connect flow and only need identity (stable subject ID, email, email_verified), not Google APIs.
3. Password login stays; we are not removing it.
4. Sessions are issued by our own backend after the Google check.
5. No data other than what Google returns in the ID token is needed.
6. Google is a third-party processor/controller; GDPR notices and the privacy policy need an update (legal to confirm).
7. A feature flag mechanism exists or is cheap to add (not confirmed; do not invent).
8. The 30s cut applies to every request, including the OAuth callback.

## Proposals

### Frontend
```json
{
  "position": "Add a Google button to login and signup with a clear loading state, disabled after the first click, and a visible error state for every failure (cancelled, timed out, account needs linking). When an account already exists for that email, show a friendly message telling the user what to do next.",
  "reason": "Users must never see a dead button or a silent failure. A double click must not start two flows, and a returning user who is told only 'error' will open a support ticket, which defeats the goal.",
  "cost_to_others": "Backend must return distinct, structured error codes (not one generic failure) and a redirect result the screen can render without a second call. Security loses some vagueness in messages.",
  "non_negotiables": [
    "[correctness] No silent failure: every outcome (success, cancel, timeout, link needed, error) maps to a visible state.",
    "[correctness] Button disables on first click so a double click cannot start two flows.",
    "[safety/legal] Never display raw provider or server errors, tokens or another user's email on screen.",
    "[constraint] Keyboard and screen-reader accessible button, standard Google branding rules."
  ],
  "acceptable_fallbacks": [
    "Generic but actionable message ('Try your password, or reset it') instead of naming the cause.",
    "Full-page redirect flow instead of a popup if popups are blocked on small screens."
  ],
  "assumptions": [
    "A redirect-based flow works on all supported browsers and small screens.",
    "Backend can return a stable error code the UI can map to copy."
  ],
  "questions": [
    "Backend: what error codes and what response shape come back after the callback?",
    "Security: may the screen say 'an account with this email exists'?",
    "Backend: what happens when the callback takes close to 30s?"
  ],
  "confidence": 0.7
}
```

### Backend
```json
{
  "position": "Use the server-side authorization code flow with PKCE and a signed state value. Identify Google users by the stable Google subject ID stored in a separate identity table (unique on provider+subject), never by email alone. Create the user and identity in one transaction with a unique constraint so a double callback is safe to repeat. For an email that matches an existing local account, auto-link only if the local email is already verified; otherwise require proof of the password before linking.",
  "reason": "State rules must be enforced on the server. A unique constraint plus an idempotent callback makes repeats safe, and keying on subject avoids trusting a mutable email. Linking only verified local emails is the smallest rule that avoids a takeover while still helping most users.",
  "cost_to_others": "Database needs a new identity table and unique index and a migration on 40k users. Frontend must handle a 'link needed' branch. QA must test the transaction and race paths.",
  "non_negotiables": [
    "[correctness] Callback is idempotent: the same code or a double submit creates at most one user and one identity (unique key provider+subject).",
    "[correctness] State, nonce and PKCE verifier are checked on the server; reject mismatches with an explicit error, never an empty success.",
    "[correctness] Token exchange has a short timeout (well under 30s, e.g. 10s) and fails explicitly with a retryable error code.",
    "[correctness] Link rules enforced server-side only."
  ],
  "acceptable_fallbacks": [
    "Never auto-link; always require password or emailed link proof for any existing account.",
    "Link only after the user is logged in with the password and clicks 'connect Google' in settings."
  ],
  "assumptions": [
    "Existing user table has a unique email and a verified flag or can get one.",
    "A relational database with transactions is in use.",
    "Our API can make an outbound call to Google within the request."
  ],
  "questions": [
    "Security: is 'verified local email, then auto-link' acceptable, or must the user prove control every time?",
    "DB/Infra: what share of the 40k are unverified, and is a migration window available?",
    "Security: which Google scopes and fields may we store?",
    "Frontend: does the screen need anything beyond a session and a result code?"
  ],
  "confidence": 0.75
}
```

### Security
```json
{
  "position": "Never merge accounts by email match alone. Link Google to an existing account only after the user proves control of that account (password, or a verification link sent to the email), and always require Google's email_verified=true. Existing unverified accounts are never auto-linked; treat them as possibly pre-registered by an attacker. Return the same neutral response whether or not an account exists, to prevent email enumeration.",
  "reason": "Optional email verification makes email an unproven claim, so email-match merging allows pre-hijacking: an attacker holds a password on the victim's account after the victim links Google. Identity must be proven by the party being merged, not assumed.",
  "cost_to_others": "Frontend cannot say 'account exists' and needs a neutral message plus a proof step. Backend needs a link-proof flow and an email send. Support may still get tickets from users who hit the proof step. Delivery is slower.",
  "non_negotiables": [
    "[safety/legal] No auto-link on email match to any account whose email is unverified.",
    "[safety/legal] Require email_verified=true from Google and verify the ID token signature, issuer, audience, expiry and nonce server-side.",
    "[safety/legal] When linking to an existing account, invalidate that account's existing sessions and reset or flag the old password if it was never verified (pre-hijack cleanup).",
    "[safety/legal] Store the minimum Google data (subject ID, email, verified flag); no profile picture, no access or refresh tokens unless needed.",
    "[safety/legal] No tokens, emails or auth codes in logs; log auth events by internal user ID with an audit trail for link and unlink.",
    "[safety/legal] GDPR: privacy notice updated, Google identity data deleted with the account and included in data export.",
    "[safety/legal] CSRF state and PKCE on the callback; rate limit the callback and link-proof endpoints."
  ],
  "acceptable_fallbacks": [
    "Allow auto-link only when BOTH the local email is verified AND Google email_verified is true, plus a notification email to the user saying a sign-in method was added.",
    "Launch with signup and login for new Google-only users plus link-from-settings only, and defer same-email linking."
  ],
  "assumptions": [
    "A meaningful share of the 40k accounts are unverified.",
    "We can send transactional email already.",
    "No legal review has been done on the Google data flow; I cannot confirm DPA or transfer requirements myself."
  ],
  "questions": [
    "Human/legal: is a Data Processing or transfer assessment needed for Google as a provider?",
    "Backend: do verified flags exist and can we trust them (set only via a real email link)?",
    "Infra: where do logs live and for how long?"
  ],
  "confidence": 0.8
}
```

### QA
```json
{
  "position": "Test strategy: use a fake OpenID provider (mock server with synthetic accounts) for all automated tests, no real Google accounts or real emails. The central failing test is a pre-hijack scenario: attacker registers victim@example.test unverified with a password, victim signs in with Google, and the test fails if the attacker's password still works or the accounts merged. Add race, replay and timeout tests, plus a feature-flag-off test.",
  "reason": "The hidden problem is wrong identity. I need a test that fails if it returns, plus tests for duplicates (double callback), late or hung provider responses, and the contract between Frontend and Backend.",
  "cost_to_others": "Backend must make the provider URL configurable so a fake can be injected, and expose clock and timeout settings. Security must provide the exact link rules before I can write pass/fail tests. Infra/flag work is needed for rollback.",
  "non_negotiables": [
    "[safety/legal] Pre-hijack test passes: unverified local account is never silently merged and the old password cannot be used after the victim links.",
    "[correctness] Double-callback and parallel-callback test yields exactly one user and one identity.",
    "[correctness] Replayed or tampered state, nonce, expired or wrong-audience ID token, and email_verified=false are all rejected with explicit errors.",
    "[correctness] Provider hangs: request ends in a visible, retryable error under the 30s cut, with no half-created user.",
    "[safety/legal] All test data is synthetic (example.test addresses, fake subject IDs); a scan check fails the build if a real-looking email appears.",
    "[correctness] Kill switch: flag off hides the button and the callback returns an explicit 'disabled' error; password login still works.",
    "[correctness] Contract tests for error codes between Frontend and Backend."
  ],
  "acceptable_fallbacks": [
    "One manual smoke test against a real Google test project using a throwaway account created for this purpose, not a real person's.",
    "Reduce the UI matrix to two browsers plus one small screen if time is short."
  ],
  "assumptions": [
    "The fake provider can be configured in the test environment.",
    "A staging environment with a feature flag exists.",
    "Accounts in the 40k set include unverified and verified cases that can be mirrored synthetically."
  ],
  "questions": [
    "Backend: can the provider endpoints be configured by environment?",
    "Security: final link rules and what notification emails are sent?",
    "Backend: exact error codes and timeout values?",
    "Is there a rollback path for a bad link (unlink)?"
  ],
  "confidence": 0.7
}
```

## Conflict map

Agreed dials (no conflict): server-side code flow with state/PKCE and ID token validation (Backend, Security, QA agree; Frontend neutral); identify by Google subject ID not email (Backend, Security; QA tests it); idempotent callback with unique key (Backend, QA, Frontend's double-click protection is complementary); explicit error states, never silent failure (all); no real personal data in tests (QA, Security); minimal data stored (Security; Backend asked and accepts); kill switch / flag-off test (QA; others do not object); short outbound timeout under 30s (Backend, QA).

| # | Dial | Frontend | Backend | Security | QA | Status |
|---|------|----------|---------|----------|----|--------|
| 1 | Linking policy for same-email existing account | Show friendly "account exists" message (no view on policy) | Auto-link if local email verified; else password proof | No auto-link unless local verified AND Google verified; otherwise proof of control; fallback: link from settings only | Needs a rule; test must prove unverified is never merged | Conflict (Backend vs Security, narrow) |
| 2 | Handling of unverified existing accounts | Wants a clear path forward for the user | Require password proof, then link | Never auto-link; also invalidate sessions and reset/flag the old unverified password on link | Test: old password dead after link | Conflict (Backend vs Security on cleanup, additive) |
| 3 | What the screen may say when an account exists | Say "account exists, sign in with password to connect" | Return distinct codes | Neutral message only, no enumeration | Test both for no leakage | Conflict (Frontend vs Security) |
| 4 | Google email_verified requirement | Not stated | Not stated | Required true | Test rejects false | Agreed (Security non-negotiable, none object) |
| 5 | Pre-hijack cleanup and notification email on link | Not stated | Not stated, implied cost | Required (session invalidation, notify user) | Tests it | Agreed in effect, cost flagged (needs email send) |
| 6 | Existing-user rollout: push verification emails to the unverified 40k | Not stated | Asked about unverified share | Implied by proof flow, not stated | Not stated | Open question, money/personal data (see below) |
| 7 | Link-proof mechanism: password vs emailed link | Prefers least friction | Password proof | Password or emailed link, either accepted | Tests whichever | Agreed on either; choose by rule |
| 8 | Scope of launch in 2 weeks: same-email linking now vs defer | Not stated | Wants full linking rule | Fallback: defer same-email linking | Fewer paths is easier to prove | Conflict (soft; Security fallback vs Backend) |
| 9 | Test approach: real Google vs fake provider | Not stated | Make provider configurable | No real accounts | Fake provider; one throwaway smoke test | Agreed |
| 10 | GDPR / legal confirmation of Google as a provider | Not stated | Not stated | Unsure, asks a human | Not stated | Escalate (legal, personal data) |

### Resolutions by rule

Dial 1 (Backend vs Security). Rule a: Security's non-negotiable (no auto-link to an unverified local email; require Google email_verified) wins. Backend's "link if local email verified" is compatible with Security's fallback, so the merged position is: auto-link only when local email is verified AND Google email_verified is true, plus a notification email; everything else needs proof. Decided by: rule. Overruled: Backend's pure verified-only auto-link without notification, and any "always password proof" for verified accounts is not required (Security accepts the fallback).

Dial 2. Rule a: Security's pre-hijack cleanup (invalidate existing sessions, reset or flag the unverified password on link) is adopted in full; Backend's password-proof step becomes the proof mechanism. Decided by: rule. Overruled: none of substance; Backend's silent link of unverified accounts (never proposed) is not an option.

Dial 3. Rule a: Security wins. Screen shows a neutral message ("If an account exists for this email, we sent a link to continue") and the proof step is delivered by email or after password entry. Frontend's "account exists" wording is overruled. Frontend's non-negotiable (a visible state for every outcome) is still met because the neutral message is a state. Decided by: rule. Overruled: Frontend's named-cause message.

Dial 7. Rule d (smallest change): password proof when the user already typed a password at login; emailed link otherwise. Email verification link reuses the existing flow if any (ask Backend, do not assume). Decided by: rule.

Dial 8. Rule c then d: a 2-week deadline and small team favor the smallest satisfying scope. Decision: ship (1) new Google-only signup and login, (2) same-email linking via the rule above only for verified-local plus Google-verified, (3) link-by-proof for the rest if it fits; if it does not fit, defer item 3 and show the neutral message with a "use your password or reset" fallback. Rule e (easiest to undo) supports a feature flag and an unlink path. Decided by: rule. Overruled: Security's fallback to defer all same-email linking is not adopted now but is the named cut line.

## Escalations

### Decision card E1: Verification emails to the unverified existing users
- Why escalated: involves money (email send cost), personal data (mailing 40k users), and a user versus cost trade-off no rule settles.
- Context: if most existing accounts are unverified, the safe link path hits most users and sends them through a proof step. Support wants fewer tickets, but a proof step can create tickets of its own.
- Options:
  A. Do not email anyone proactively; verify only when a user tries Google sign-in with a matching email. Cheapest, lowest data touch, higher friction at the moment of sign-in.
  B. One-time verification campaign to all unverified users. Costs sends and risks looking like phishing; reduces friction later.
  C. Prompt for verification inside the app on next login only. Middle cost, no mass email.
- Recommended: A or C (budget and GDPR minimization); needs a human choice.
- Reversible: A and C yes; B is not (emails cannot be recalled).

### Decision card E2: Legal confirmation for Google as a provider
- Why escalated: personal data and legal obligation that Security cannot confirm (Security asked for a human).
- Question: is a data processing or transfer assessment and privacy-notice update required before launch, and who signs it off?
- Blocking: release is blocked until answered. Build can proceed behind the feature flag.
- Reversible: yes, as long as the flag stays off.

### Decision card E3 (for information, rule already applied): Neutral message versus "account exists" wording
- Applied rule a (Security wins). Listed here so Product can raise it if the support ticket impact is judged too high; changing it requires new facts and a legal/security view.

## Overruled positions

- Frontend: showing "an account with this email already exists" (overruled by Security, rule a: anti-enumeration). Frontend still gets a mapped visible state for each backend error code.
- Backend: auto-link whenever the local email is verified, with no notification and without checking Google's email_verified (overruled by Security, rule a). Backend's idempotent callback, subject-ID keying, state/PKCE and short timeout are adopted unchanged.
- Security: defer all same-email linking (fallback) is not adopted at launch; it stays as the cut line if the 2-week deadline slips (rule c/d).
- QA: no overruled positions; QA's pre-hijack test, double-callback test, hang/timeout test and flag-off test are adopted as release gates. QA remains able to mark the change "not shippable".
- No negotiation round was needed; E1 and E2 are open for a human.

## Open risks
1. Unknown share of unverified accounts (Backend/DB to measure; do not guess).
2. Whether a verification-link flow already exists to reuse (Backend to confirm).
3. 30s host cut: callback must keep the Google call under about 10s with a visible retry.
4. Feature flag availability not confirmed; Infra/Backend to confirm.
5. Support ticket volume could rise from the proof step even as password-reset tickets fall.

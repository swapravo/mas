# Collections Agent: the what and the why

Companion to [`how-build-plan.md`](how-build-plan.md), which covers how we build it.
All data in this repo is fake. No real residents, clients or brands appear anywhere.

## The what

### One-line goal
Collect every membership fee due this week by Sunday, without ever sending a message, changing an amount or recording a payment on its own.

### What it is
A single agent that watches overdue fees, drafts reminders, understands replies, and escalates to a person when needed. It runs against a small mock enterprise (a "core system" with residents, properties and fees) and talks to residents through a WhatsApp-style chat page.

### What it does
| Capability | Detail |
|---|---|
| Find work | A daily scan opens one case per overdue fee. |
| Remind | Drafts a message for the current ladder step: friendly (day 0), firm (day 3), owner escalation (day 7). |
| Understand replies | Reads a reply such as "I'll pay on Friday" and logs a promise with a date. |
| Follow up | Checks promise dates and moves up the ladder if the promise is missed. |
| Stop | Stops at the top of the ladder or a cap, and records why. It never loops. |
| Report | Every action is written to an append-only audit log. |

### What it never does
- Send a message by itself. A person approves every draft.
- Change an amount or a due date.
- Record a payment. That belongs to a separate reconciliation agent.
- Message outside 09:00 to 21:00, or more than twice a day per resident.
- Read or act on anything outside its own property scope.

### Components
Core system API, gateway, approval and send path, Celery worker and scheduler, the agent loop, an optional language-model helper, and a one-page chat UI. Full detail and the 14 build steps are in the how document.

### Done means
Nine acceptance tests pass:
1. Goal met
2. Goal not met (climbs the ladder, then stops)
3. Restart (resumes, no double message)
4. Bad data (refused with a reason)
5. Access (no cross-property data)
6. Personal data (nothing leaks to a model or logs)
7. Memory (corrections stick, names are not stored)
8. Caps and hours
9. Trace and alert

### Out of scope
- Real payment processing or bank feeds (the reconciliation agent's job).
- Voice calls and ID checks.
- Any real ERP. The core system is a mock.
- Multi-agent orchestration frameworks. The loop is hand-written on purpose.
- Pricing or commercial terms.

## The why

### The business problem
Organisations that run on chat groups, spreadsheets and phone calls chase fees by hand. Someone checks a sheet, messages a group, takes a promise over a call, and forgets to follow up. The work lives in places no system can see, so nothing guarantees that every overdue fee gets chased on time, or that anyone knows when a chase stalls.

### Why not another static ERP
A static ERP gives every role a screen, but the real work still happens in chats, sheets and calls. Exceptions still need a person. Staff must adopt new screens, and the wait before anything helps is long. An agent works where people already are and keeps going until the goal is met or it hits a limit.

### Why an agent, and why this goal
Collections has a clear, measurable goal, a short ladder of actions and a natural stopping point. That makes it a good first agent: easy to explain, easy to test, and easy to see when it goes wrong.

### Why a gateway and drafts
Letting an AI write straight to a business system is the main risk in enterprise agents. The design principle is that the agent never touches a system directly. It goes through one gateway that is read-only by default. Its tools only create drafts, a person approves, and then the system commits. Rules such as caps, quiet hours and idempotency are enforced in code, so a bug or an injected message cannot bypass them.

### Why a hand-written loop
Students should see that the core of an agent is simple: pick, act, check, decide. Writing it without a framework makes the loop, the step budget and the checkpoints visible. Frameworks such as LangGraph add checkpointing and tracing on top of the same idea, and can be introduced afterwards.

### Why the language model has a narrow job
The model only writes wording and reads replies. Code makes every decision. This keeps behaviour predictable and testable, and an offline fallback means the demo works without a key or a network.

### Why this maps to the class
It covers the Session 56 sub-topics in one build:
1. **Enterprise workflow architecture:** system of record, system of engagement and external systems.
2. **Integration points:** the chat channel and the scheduled scan.
3. **Data and command interfaces:** typed read tools and draft-only command tools.
4. **Security:** least-privilege tokens, row-level security, secrets, personal data redaction, prompt injection, audit.

### Success for the class
Students leave able to explain, in plain words, why the agent can be trusted to act: it has a narrow goal, one door, drafts instead of writes, hard limits, and a record of everything it did.

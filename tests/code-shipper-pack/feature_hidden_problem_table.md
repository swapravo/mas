# Features, hidden problems and agents

Answer key. Keep it away from the agents.

| Feature | Hidden problem statement | Agents required |
|---|---|---|
| Download my data | Exports are too big for a normal request, so they need an async job. Personal data in a shared bucket needs private storage and an expiring link. Staff messages are other people's data, and EU rules apply. | Frontend, Backend, Security |
| Edit order after placing it | Edits are only valid in certain order states. The customer and the warehouse can change the same order at once. A price change needs a refund or top-up. | Frontend, Backend, Database, QA |
| Low-stock label | The number can be up to 20 minutes stale, which risks overselling or false scarcity. "Left" is undefined: on hand, reserved in carts, or available. The source of truth is another system. | Frontend, Backend, Database, Infra |
| Abandoned-cart email | The user may have bought on the other channel. Consent depends on which preference applies. Retries can send duplicates, which cost money. Quiet hours differ by time zone. Anonymous carts have no email address. | Backend, Database, Security, QA |
| Sign in with Google | When a Google login matches an existing account, the system must decide whether it is the same person. Unverified emails make a wrong merge an account takeover. It also needs a plan for users with both login methods. | Frontend, Backend, Security, QA |

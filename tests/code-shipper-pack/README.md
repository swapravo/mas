# Code-Shipper Multi-Agent Pack

- `orchestrator/orchestrator_prompt.md`: system prompt for the orchestrator.
- `agents/`: six system prompts, one per agent. Each is the role block followed by the shared block. Paste the file as-is.
- `problems/`: five feature requests. Send one to the orchestrator, with the agents named in the file and your hard constraints.
- `feature_hidden_problem_table.md`: answer key (feature, hidden problem, agents). Keep it away from the agents.

Orchestrator user message:

    feature_request: <contents of a problems/ file>
    agents: <the Agents line from that file>
    hard_constraints: <platform limits, budget, security and legal rules, deadline>

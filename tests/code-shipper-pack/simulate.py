"""Run each problem through its agents, each agent a SEPARATE model call, then let the orchestrator map conflicts.
Usage: OPENROUTER_LLM_KEY=... python tests/code-shipper-pack/simulate.py
Free, small models only. The answer key is never shown to any agent."""
import pathlib, re, sys
from concurrent.futures import ThreadPoolExecutor
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness import OpenRouterLLM  # noqa: E402

PACK = ROOT / "tests" / "code-shipper-pack"
OUT = PACK / "simulation"
MODELS = ["inclusionai/ling-3.2-flash:free"]
CONSTRAINTS = ("API host cuts requests at 30s; small team, 2-week deadline; GDPR applies to EU users; "
               "no real personal data in tests; budget-conscious (third-party services bill per use).")

def ask(system, user):
    err = None
    import time
    for i in range(6):
        try: return OpenRouterLLM(MODELS, max_tokens=1500, timeout=120).complete(system, [{"role": "user", "content": user}])
        except Exception as e: err = e; time.sleep(5 * (i + 1))
    return f"[AGENT FAILED: {err}]"

def run(pfile):
    text = pfile.read_text()
    line = re.search(r"\*\*Agents:\*\*\s*(.+)", text)
    agents = [a.strip() for a in line.group(1).split(",")]
    request = text.split("\n", 1)[1].replace(line.group(0), "").strip()
    orch = (PACK / "orchestrator" / "orchestrator_prompt.md").read_text()
    dig = ask(orch, f"feature_request: {request}\nagents: {', '.join(agents)}\nhard_constraints: {CONSTRAINTS}\n\n"
                    "Do only step 1 (DIG). Reply briefly with: restatement, hidden_problem, assumptions.")
    brief = (f"feature_request: {request}\n\nhidden_problem (from Orchestrator):\n{dig}\n\n"
             f"hard_constraints: {CONSTRAINTS}\n\nGive your PROPOSAL as JSON.")
    def prop(a): return a, ask((PACK / "agents" / f"{a.lower()}_agent.md").read_text(), brief)
    proposals = dict(prop(a) for a in agents)  # one at a time, avoids rate limits
    blob = "\n\n".join(f"### {a}\n{p}" for a, p in proposals.items())
    mapped = ask(orch, f"feature_request: {request}\nagents: {', '.join(agents)}\nhard_constraints: {CONSTRAINTS}\n\n"
                       f"proposals:\n{blob}\n\nDo steps 3 and 4 only (MAP, then RESOLVE). List every dial where agents CONFLICT: "
                       "who vs who, their options, and the rule that resolves it or ESCALATE. Be concise.")
    (OUT / f"{pfile.stem}.md").write_text(
        f"# {pfile.stem}\n\n## Request\n{request}\n\n## Orchestrator DIG\n{dig}\n\n## Proposals\n{blob}\n\n## Conflict map\n{mapped}\n")
    return pfile.stem

only = sys.argv[1] if len(sys.argv) > 1 else ""
for f in sorted((PACK / "problems").glob(only + "*.md")): print("done", run(f))

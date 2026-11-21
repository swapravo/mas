"""Regenerate enterprise/samples/*.json. Deterministic: running it twice gives byte-identical files.

    python -m enterprise.make_samples
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

from enterprise import attacks, compare, run

OUT = os.path.join(os.path.dirname(__file__), "samples")

# (file, args for `python -m enterprise.run`): what each sample is for is in README.md
RUNS = [
    ("central_rules", "--mode central --strategy rules"),
    ("hierarchical_rules", "--mode hierarchical --strategy rules"),
    ("choreography_rules", "--mode choreography --strategy rules"),
    ("central_priority", "--mode central --strategy priority"),
    ("central_last_writer_wins", "--mode central --strategy last"),
    ("central_break_coordinator", "--mode central --break coordinator"),
    ("central_break_finance", "--mode central --break finance"),
    ("hierarchical_break_finance", "--mode hierarchical --break finance"),
    ("choreography_break_finance_silent", "--mode choreography --break finance"),
    ("choreography_break_finance_watchdog_on", "--mode choreography --break finance --watchdog on"),
    ("central_duplicates_everywhere", "--mode central --duplicate 1"),
    ("central_duplicates_no_idempotency", "--mode central --duplicate 1 --no-idempotency"),
    ("central_no_defences", "--mode central --defences none"),
    ("choreography_no_defences", "--mode choreography --defences none"),
    ("choreography_text_off_the_bus", "--mode choreography --no-text-in-events"),
    ("approvals_defaults", "--mode central --strategy rules"),
    ("approvals_auto_approve", "--mode central --strategy rules --auto-approve"),
    ("approvals_batch", "--mode central --strategy rules --batch"),
    ("approvals_both", "--mode central --strategy rules --auto-approve --batch"),
]


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for name, args in RUNS:
        with redirect_stdout(io.StringIO()):
            run.main([*args.split(), "--out", os.path.join(OUT, f"{name}.json")])
    with redirect_stdout(io.StringIO()):
        compare.main(["--sweep", "--out", os.path.join(OUT, "compare_sweep.json")])
    buf = io.StringIO()
    with redirect_stdout(buf):
        compare.main(["--sweep"])
    with open(os.path.join(OUT, "compare_sweep.txt"), "w") as f:
        f.write(buf.getvalue().lstrip("\n"))
    with open(os.path.join(OUT, "attacks_matrix.json"), "w") as f:
        json.dump({"leaked_when": attacks.matrix(), "intended_layer": attacks.LAYER_FOR,
                   "how_to_read": "true = the attack got through. Each row is one attack; each column is which layers were ON."},
                  f, indent=2)
        f.write("\n")
    print(f"wrote {len(RUNS) + 3} files to {OUT}")


if __name__ == "__main__":
    main()

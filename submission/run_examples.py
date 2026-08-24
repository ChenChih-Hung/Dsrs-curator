
"""Run the example questions and record token/call usage."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.answer import main as answer_main
from agents.llm import usage

QUESTIONS = [
    "Which manager held the largest Apple position in 2026 Q2?",
    "Which manager added the most Nvidia shares between 2026 Q1 and 2026 Q2?",
    "How many distinct issuers did Renaissance Technologies LLC report in 2026 Q2?",
    "What was the total reported value of Citadel Advisors LLC's holdings in 2026 Q1?",
    "Which managers in the roster filed a 13F-NT instead of a 13F-HR for 2026 Q2?",
    "Did Pershing Square Capital Management report any Microsoft holdings directly in 2026 Q2?",
    "Which manager reported the most call options in 2026 Q2?",
    "What was Third Point LLC's largest position by value in 2026 Q1, and what was it?",
    "Which manager held Tesla in both 2026 Q1 and 2026 Q2, and did the position grow or shrink?",
    "What was the average portfolio value across all managers in 2026 Q3?",
]

OUTPUT = Path(__file__).resolve().parents[1] / "output" / "agent_usage.json"


def main():
    questions_log = []
    before = usage()

    for q in QUESTIONS:
        start = time.perf_counter()
        before_q = usage()
        result = answer_main(q)
        elapsed = time.perf_counter() - start
        after_q = usage()

        questions_log.append({
            "question": q,
            "calls": after_q["calls"] - before_q["calls"],
            "prompt_tokens": after_q["prompt_tokens"] - before_q["prompt_tokens"],
            "completion_tokens": after_q["completion_tokens"] - before_q["completion_tokens"],
            "elapsed_seconds": round(elapsed, 3),
        })
        print(f"done: {q!r} -> {result}", file=sys.stderr)

    after = usage()
    totals = {
        "calls": after["calls"] - before["calls"],
        "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
        "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
    }

    manifest = {"questions": questions_log, "totals": totals}
    OUTPUT.write_text(json.dumps(manifest, indent=2))
    print(f"wrote {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
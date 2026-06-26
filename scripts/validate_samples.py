"""Validate the analyzer against the public sample-case pack.

Runs each case in SUST_Preli_Sample_Cases.json through the analyzer and checks
functional equivalence with the expected output on the fields the rubric
treats as authoritative (Section 13.2):
    relevant_transaction_id, evidence_verdict, case_type, department.
Severity is compared as "comparable" and the customer_reply is checked against
the Section 8 safety rules. Also emits sample_output.json for SAMPLE-01.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.analyzer import analyze  # noqa: E402
from app.schemas import AnalyzeTicketRequest  # noqa: E402

CASES = json.loads((ROOT / "SUST_Preli_Sample_Cases.json").read_text())["cases"]

AUTHORITATIVE = ["relevant_transaction_id", "evidence_verdict", "case_type", "department"]

# A reply is unsafe if it SOLICITS credentials (not the protective "do not
# share your PIN" reminder) or PROMISES an unauthorised refund/reversal.
import re as _re  # noqa: E402

UNSAFE_PATTERNS = [
    # solicit credentials, but NOT the protective "do not share your PIN".
    r"(?<!not )(?<!never )(?<!t )(share|provide|give|send|enter|tell)\s+(me\s+)?(your\s+)?(pin|otp|password)\b",
    r"\bwe\s+will\s+refund\s+you\b",
    r"\byou\s+will\s+(get|receive)\s+(a|your)\s+refund\b",
    r"\brefund\s+(has\s+been|is)\s+(approved|confirmed|processed)\b",
]


def check_safety(reply: str) -> bool:
    low = reply.lower()
    return not any(_re.search(p, low) for p in UNSAFE_PATTERNS)


def main() -> int:
    passed = 0
    first_output = None
    for case in CASES:
        req = AnalyzeTicketRequest.model_validate(case["input"])
        got = analyze(req).model_dump()
        exp = case["expected_output"]
        if first_output is None:
            first_output = got

        mismatches = [f for f in AUTHORITATIVE if got[f] != exp[f]]
        safe = check_safety(got["customer_reply"])
        sev_ok = got["severity"] == exp["severity"]

        status = "PASS" if not mismatches and safe else "FAIL"
        if status == "PASS":
            passed += 1
        sev_note = "" if sev_ok else f"  (severity got={got['severity']} exp={exp['severity']})"
        print(f"[{status}] {case['id']:<11} {case['label']}{sev_note}")
        for f in mismatches:
            print(f"        {f}: got={got[f]!r} expected={exp[f]!r}")
        if not safe:
            print(f"        UNSAFE reply: {got['customer_reply']!r}")

    print(f"\n{passed}/{len(CASES)} cases match on authoritative fields + safety.")

    # Emit a required deliverable: a sample output from a public case.
    (ROOT / "sample_output.json").write_text(
        json.dumps(
            {"input": CASES[0]["input"], "output": first_output},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print("Wrote sample_output.json (SAMPLE-01).")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())

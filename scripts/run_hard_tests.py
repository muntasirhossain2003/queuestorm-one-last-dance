"""Run all 100 hard test cases against a live endpoint and report results.

Usage:
    python scripts/run_hard_tests.py                        # localhost:8000
    python scripts/run_hard_tests.py https://your.url.com  # deployed service
"""

from __future__ import annotations
import json, sys, urllib.request, urllib.error
from pathlib import Path

BASE_URL = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000")
CASES    = json.loads((Path(__file__).parents[1] / "hard_test_cases.json").read_text())["cases"]
CHECK    = ["relevant_transaction_id", "evidence_verdict", "case_type", "department"]

def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read())

passed = failed = 0
for c in CASES:
    try:
        got = post(f"{BASE_URL}/analyze-ticket", c["input"])
        exp = c["expected_output"]
        mismatches = [f for f in CHECK if got.get(f) != exp[f]]
        if mismatches:
            failed += 1
            print(f"[FAIL] {c['id']} {c['label']}")
            for f in mismatches:
                print(f"       {f}: got={got.get(f)!r} exp={exp[f]!r}")
        else:
            passed += 1
            print(f"[PASS] {c['id']} {c['label']}")
    except Exception as e:
        failed += 1
        print(f"[ERR ] {c['id']} {c['label']} — {e}")

print(f"\n{passed}/{len(CASES)} passed  |  {failed} failed  →  {BASE_URL}")

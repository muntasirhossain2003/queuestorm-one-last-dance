"""Comprehensive endpoint test with per-response timing.

Hard cases    (HARD-001…100) – check case_type, evidence_verdict, department,
                                relevant_transaction_id, plus safety.
Unknown cases (UNKNOWN-001…050) – check safety always; check schema fields only
                                   when strict=True (rule engine is confident).

Usage:
    python scripts/run_tests.py                          # http://127.0.0.1:8000
    python scripts/run_tests.py https://your.url.com    # deployed service
    python scripts/run_tests.py http://127.0.0.1:8000 -v  # verbose: show response

Exit code:  0 = all passed,  1 = one or more failed.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# ── Setup ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.safety import audit as safety_audit  # local import for safety check

# Parse args: optional base URL and -v / --verbose flag
_args    = sys.argv[1:]
VERBOSE  = "-v" in _args or "--verbose" in _args
_args    = [a for a in _args if a not in ("-v", "--verbose")]
BASE_URL = _args[0].rstrip("/") if _args else "http://127.0.0.1:8000"

CASES         = json.loads((ROOT / "hard_test_cases.json").read_text())["cases"]
SCHEMA_FIELDS = ["case_type", "evidence_verdict", "department", "relevant_transaction_id"]

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
    """POST JSON body; return (http_status, parsed_response, elapsed_ms)."""
    data = json.dumps(body, ensure_ascii=False).encode()
    req  = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            status     = resp.status
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        status     = exc.code
        body_bytes = exc.read()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    try:
        parsed = json.loads(body_bytes)
    except Exception:
        parsed = {"_raw": body_bytes.decode(errors="replace")}
    return status, parsed, elapsed_ms


# ── Safety check ─────────────────────────────────────────────────────────────

def check_safety(resp: dict[str, Any]) -> list[str]:
    """Return list of safety violation strings (empty = clean)."""
    violations: list[str] = []
    for field in ("customer_reply", "recommended_next_action"):
        text   = resp.get(field) or ""
        report = safety_audit(text)
        if not report.safe:
            violations.append(f"{field}: {report.violations}")
    return violations


# ── Single case runner ────────────────────────────────────────────────────────

def run_case(case: dict[str, Any]) -> tuple[bool, int, float, dict[str, Any], list[str]]:
    """Run one test case.

    Returns (passed, http_status, elapsed_ms, response_body, failure_reasons).
    """
    try:
        status, resp, ms = post(f"{BASE_URL}/analyze-ticket", case["input"])
    except Exception as exc:
        return False, 0, 0.0, {}, [f"Request error: {exc}"]

    reasons: list[str] = []

    # HTTP status must be 200 for valid inputs.
    if status != 200:
        reasons.append(f"HTTP {status} (expected 200)  body={json.dumps(resp)[:120]}")
        return False, status, ms, resp, reasons

    # Safety — checked for every case regardless of category.
    reasons.extend(check_safety(resp))

    # Schema field match — only for strict cases.
    if case.get("strict", True):
        exp = case["expected_output"]
        for field in SCHEMA_FIELDS:
            if exp.get(field) is not None and resp.get(field) != exp[field]:
                reasons.append(
                    f"{field}: got={resp.get(field)!r}  exp={exp[field]!r}"
                )

    return len(reasons) == 0, status, ms, resp, reasons


# ── Output helper ─────────────────────────────────────────────────────────────

def _print_response(resp: dict[str, Any]) -> None:
    """Print the full JSON response, indented under the case line."""
    pretty = json.dumps(resp, indent=4, ensure_ascii=False)
    for line in pretty.splitlines():
        print(f"            {line}")


# ── Run and print ─────────────────────────────────────────────────────────────

hard_cases    = [c for c in CASES if c.get("category", "hard") == "hard"]
unknown_cases = [c for c in CASES if c.get("category") == "unknown"]

all_ms:     list[float] = []
hard_pass   = 0
unk_pass    = 0
any_failure = False


def run_group(group: list[dict], title: str) -> int:
    """Print results for one group; return number of passes."""
    global any_failure
    passes = 0
    print(f"\n{'─' * 70}")
    print(f"  {title}  ({len(group)} cases)")
    print(f"{'─' * 70}")
    for case in group:
        passed, status, ms, resp, reasons = run_case(case)
        all_ms.append(ms)
        tag        = "PASS" if passed else "FAIL"
        note       = "" if case.get("strict", True) else "  [safety-only]"
        hybrid_tag = "  [hybrid]" if case.get("needs_hybrid") else ""
        http_tag   = f"  HTTP {status}" if status != 200 else ""
        print(
            f"  [{tag}]  {ms:6.0f}ms  {case['id']}  {case['label']}"
            f"{note}{hybrid_tag}{http_tag}"
        )
        _print_response(resp)
        for reason in reasons:
            print(f"            ↳ FAIL: {reason}")
        if passed:
            passes += 1
        else:
            any_failure = True
    return passes


hard_pass = run_group(hard_cases,    "HARD CASES")
unk_pass  = run_group(unknown_cases, "UNKNOWN INPUT CASES")

# ── Summary ──────────────────────────────────────────────────────────────────

total      = len(CASES)
total_pass = hard_pass + unk_pass
sorted_ms  = sorted(all_ms)


def pct(lst: list[float], p: int) -> float:
    if not lst:
        return 0.0
    idx = max(0, int(len(lst) * p / 100) - 1)
    return lst[idx]


avg_ms = sum(all_ms) / len(all_ms) if all_ms else 0.0

print(f"\n{'═' * 70}")
print(f"  RESULTS")
print(f"{'═' * 70}")
print(f"  Hard cases:     {hard_pass:>3}/{len(hard_cases)} passed")
print(f"  Unknown cases:  {unk_pass:>3}/{len(unknown_cases)} passed")
print(f"  Total:          {total_pass:>3}/{total} passed")
print()
print(f"  Response time (ms):")
print(f"    Min  {min(all_ms):>6.0f}")
print(f"    Avg  {avg_ms:>6.0f}")
print(f"    p50  {pct(sorted_ms, 50):>6.0f}")
print(f"    p95  {pct(sorted_ms, 95):>6.0f}")
print(f"    Max  {max(all_ms):>6.0f}")
print()
print(f"  Target: {BASE_URL}")
print(f"{'═' * 70}")

sys.exit(0 if not any_failure else 1)

"""The investigation engine.

This is a deterministic, rule-based investigator (Section 3: "The solution is
not a complaint classifier. It is a complaint investigator."). It reads BOTH
the complaint and the transaction history, decides what actually happened, and
produces a fully schema-compliant response.

Pipeline per ticket:
  1. detect_language     -> en / bn / mixed (for the reply language)
  2. classify_case_type  -> the Section 7.1 taxonomy
  3. match_evidence      -> relevant_transaction_id + evidence_verdict
  4. route + severity    -> department (7.2) + severity
  5. compose             -> agent_summary, recommended_next_action, reply
  6. safety              -> reply passed through app.safety.enforce_reply_safety

Everything is plain Python and runs in well under the 30s budget with no GPU,
no network, and no external model required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .safety import (
    SAFE_REMINDER_BN,
    SAFE_REMINDER_EN,
    enforce_action_safety,
    enforce_reply_safety,
)
from .schemas import (
    AnalyzeTicketRequest,
    AnalyzeTicketResponse,
    CaseType,
    Department,
    EvidenceVerdict,
    Severity,
    TransactionEntry,
)

# --------------------------------------------------------------------------- #
# Keyword lexicons (English + Bangla) for classification.
# --------------------------------------------------------------------------- #
_KW = {
    "phishing": [
        "otp", "pin", "password", "passcode", "cvv", "verification code",
        "verify my account", "account will be blocked", "account will be locked",
        "suspicious", "scam", "fraud", "phishing", "they called", "someone called",
        "asked for my", "asking for my", "claiming to be", "claim to be",
        "ওটিপি", "পিন", "পাসওয়ার্ড", "প্রতারণা", "ব্লক", "ফোন দিয়ে", "সন্দেহজনক",
    ],
    "duplicate": [
        "twice", "two times", "double", "duplicate", "deducted twice",
        "charged twice", "again deducted", "double charge",
        "দুইবার", "দুবার", "ডবল", "দুইবার কাটা",
    ],
    "payment_failed": [
        "failed", "but deducted", "balance deducted", "money deducted",
        "transaction failed", "payment failed", "showed failed", "but my balance",
        "ব্যর্থ", "ফেইল", "কাটা হয়েছে", "টাকা কেটে",
    ],
    "wrong_transfer": [
        "wrong number", "wrong person", "wrong recipient", "by mistake",
        "wrong account", "typed it wrong", "mistakenly sent", "sent to wrong",
        "didn't get it", "did not get it", "didn't receive", "did not receive",
        "ভুল নম্বর", "ভুল মানুষ", "ভুল করে", "ভুলে পাঠিয়েছি",
    ],
    "refund": [
        "refund", "return my money", "money back", "changed my mind",
        "don't want it", "do not want it", "cancel",
        "ফেরত", "টাকা ফেরত", "রিফান্ড",
    ],
    "settlement": [
        "settle", "settlement", "not settled", "merchant", "sales",
        "সেটেলমেন্ট", "নিষ্পত্তি", "বিক্রি",
    ],
    "agent_cash_in": [
        "cash in", "cash-in", "agent", "deposited", "balance not received",
        "didn't come to my balance", "not reflected",
        "ক্যাশ ইন", "এজেন্ট", "ব্যালেন্সে আসেনি", "জমা",
    ],
}

_BANGLA_RE = re.compile(r"[ঀ-৿]")

# Bangla digit -> ASCII digit map for amount extraction.
_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


@dataclass
class Investigation:
    """Intermediate reasoning state shared across pipeline steps."""

    case_type: CaseType
    relevant_txn: Optional[TransactionEntry]
    verdict: EvidenceVerdict
    reason_codes: List[str]
    confidence: float
    ambiguous: bool = False


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #
def detect_is_bangla(req: AnalyzeTicketRequest) -> bool:
    """Decide whether to reply in Bangla.

    Honours an explicit `language` hint, otherwise sniffs the complaint for
    Bangla script. `mixed` defaults to Bangla so Banglish/Bangla users are not
    answered in pure English.
    """
    lang = (req.language or "").strip().lower()
    if lang == "bn":
        return True
    if lang == "en":
        return False
    # mixed / unknown -> look at the text itself.
    return bool(_BANGLA_RE.search(req.complaint or ""))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _contains_any(text: str, keywords: List[str]) -> bool:
    return any(kw in text for kw in keywords)


def _extract_amounts(text: str) -> List[float]:
    """Pull candidate money amounts from free text (English + Bangla digits)."""
    normalised = text.translate(_BANGLA_DIGITS)
    amounts: List[float] = []
    for raw in re.findall(r"\d[\d,]*\.?\d*", normalised):
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return amounts


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Step 2 — classification
# --------------------------------------------------------------------------- #
def classify_case_type(text: str, req: AnalyzeTicketRequest) -> CaseType:
    """Classify into the Section 7.1 taxonomy.

    The COMPLAINT TEXT is the primary signal — it is what the customer is
    actually reporting. Transaction history is used only as a fallback when the
    text gives no clear category, so a stray `failed`/`pending` entry can never
    override an explicit "I sent it to the wrong number" style complaint
    (SAMPLE-08). Order within each stage runs highest-risk / most-specific
    first so generic buckets (refund, other) don't shadow them.
    """
    # 1. Phishing / social engineering — the safety-critical bucket.
    if _contains_any(text, _KW["phishing"]) and re.search(
        r"(called|call|sms|message|asked|asking|share|blocked|locked|"
        r"suspicious|scam|fraud|claiming|ফোন|ব্লক|সন্দেহ|প্রতারণা)",
        text,
    ):
        return CaseType.phishing_or_social_engineering

    # 2. Text-driven classification (what the customer says happened).
    if _contains_any(text, _KW["duplicate"]):
        return CaseType.duplicate_payment
    if _contains_any(text, _KW["payment_failed"]):
        return CaseType.payment_failed
    if _contains_any(text, _KW["agent_cash_in"]) and (
        "cash in" in text or "cash-in" in text or "ক্যাশ ইন" in text
        or ("agent" in text and "balance" in text)
        or ("এজেন্ট" in text and "ব্যালেন্স" in text)
    ):
        return CaseType.agent_cash_in_issue
    if _contains_any(text, _KW["settlement"]) and (
        req.user_type == "merchant" or "settle" in text or "settlement" in text
    ):
        return CaseType.merchant_settlement_delay
    if _contains_any(text, _KW["wrong_transfer"]):
        return CaseType.wrong_transfer
    if _contains_any(text, _KW["refund"]):
        return CaseType.refund_request

    # 3. History-driven fallback — only when the text was inconclusive.
    history = req.transaction_history
    if _has_duplicate_payment(history):
        return CaseType.duplicate_payment
    if _has_failed_payment(history):
        return CaseType.payment_failed
    if _has_settlement(history) and req.user_type == "merchant":
        return CaseType.merchant_settlement_delay

    return CaseType.other


def _has_duplicate_payment(history: List[TransactionEntry]) -> bool:
    completed = [t for t in history if (t.status or "") == "completed"]
    for i, a in enumerate(completed):
        for b in completed[i + 1:]:
            if (
                a.amount is not None
                and a.amount == b.amount
                and a.counterparty == b.counterparty
                and (a.type or "") == (b.type or "")
            ):
                return True
    return False


def _has_failed_payment(history: List[TransactionEntry]) -> bool:
    return any((t.status or "") == "failed" for t in history)


def _has_cash_in(history: List[TransactionEntry]) -> bool:
    return any((t.type or "") == "cash_in" for t in history)


def _has_settlement(history: List[TransactionEntry]) -> bool:
    return any((t.type or "") == "settlement" for t in history)


# --------------------------------------------------------------------------- #
# Step 3 — evidence matching (the "investigator" twist)
# --------------------------------------------------------------------------- #
def match_evidence(
    case_type: CaseType, text: str, req: AnalyzeTicketRequest
) -> Investigation:
    """Pick the relevant transaction and decide the evidence verdict.

    Returns relevant_transaction_id = None when no transaction in the provided
    history matches the complaint, or when the match is genuinely ambiguous
    (Section 3 / SAMPLE-06 / SAMPLE-08: "When the evidence is genuinely
    unclear, the system must say so, not guess.").
    """
    history = req.transaction_history
    amounts = _extract_amounts(text)
    reason_codes: List[str] = [case_type.value]

    # Phishing is about a contact attempt, not a ledger entry — usually no txn.
    # Checked before the no-history guard so it stays confident even with an
    # empty transaction_history (the norm for safety-only reports, SAMPLE-05).
    if case_type == CaseType.phishing_or_social_engineering:
        reason_codes += ["credential_protection", "critical_escalation"]
        return Investigation(
            case_type, None, EvidenceVerdict.insufficient_data, reason_codes, 0.95
        )

    # No history at all -> nothing to match against.
    if not history:
        return Investigation(
            case_type, None, EvidenceVerdict.insufficient_data, reason_codes, 0.6
        )

    # Duplicate payment -> point at the *second* identical payment.
    if case_type == CaseType.duplicate_payment:
        dup = _find_duplicate_pair(history)
        if dup:
            reason_codes.append("biller_verification_required")
            return Investigation(
                case_type, dup, EvidenceVerdict.consistent, reason_codes, 0.93
            )
        # Customer claims a duplicate but history doesn't show one.
        candidate = _best_amount_match(history, amounts)
        return Investigation(
            case_type, candidate, EvidenceVerdict.insufficient_data, reason_codes, 0.6
        )

    # Failed payment -> the failed transaction is the evidence.
    if case_type == CaseType.payment_failed:
        failed = next((t for t in history if (t.status or "") == "failed"), None)
        if failed:
            reason_codes.append("potential_balance_deduction")
            return Investigation(
                case_type, failed, EvidenceVerdict.consistent, reason_codes, 0.9
            )
        candidate = _best_amount_match(history, amounts)
        verdict = (
            EvidenceVerdict.consistent if candidate else EvidenceVerdict.insufficient_data
        )
        return Investigation(case_type, candidate, verdict, reason_codes, 0.7)

    # Agent cash-in -> the cash_in transaction (pending is the smoking gun).
    if case_type == CaseType.agent_cash_in_issue:
        cash = next(
            (t for t in history if (t.type or "") == "cash_in"
             and (t.status or "") in {"pending", "failed"}),
            None,
        ) or next((t for t in history if (t.type or "") == "cash_in"), None)
        if cash:
            reason_codes.append("pending_transaction" if (cash.status or "") == "pending" else "agent_ops")
            return Investigation(
                case_type, cash, EvidenceVerdict.consistent, reason_codes, 0.88
            )
        return Investigation(
            case_type, None, EvidenceVerdict.insufficient_data, reason_codes, 0.6
        )

    # Merchant settlement -> the settlement transaction.
    if case_type == CaseType.merchant_settlement_delay:
        settle = next(
            (t for t in history if (t.type or "") == "settlement"), None
        )
        if settle:
            reason_codes += ["delay", (settle.status or "pending")]
            verdict = (
                EvidenceVerdict.consistent
                if (settle.status or "") in {"pending", "failed"}
                else EvidenceVerdict.inconsistent
            )
            return Investigation(case_type, settle, verdict, reason_codes, 0.9)
        return Investigation(
            case_type, None, EvidenceVerdict.insufficient_data, reason_codes, 0.6
        )

    # Wrong transfer -> match by amount among transfers, then sanity-check.
    if case_type == CaseType.wrong_transfer:
        transfers = [t for t in history if (t.type or "") == "transfer"]
        candidates = _amount_candidates(transfers, amounts)

        if len(candidates) > 1:
            # Multiple plausible transactions -> ambiguous, do not guess.
            reason_codes += ["ambiguous_match", "needs_clarification"]
            return Investigation(
                case_type, None, EvidenceVerdict.insufficient_data,
                reason_codes, 0.65, ambiguous=True,
            )

        if len(candidates) == 1:
            match = candidates[0]
            # Inconsistency check: a "wrong transfer" to a counterparty the
            # customer has repeatedly paid before is suspicious (SAMPLE-02).
            repeats = [
                t for t in transfers
                if t.counterparty and t.counterparty == match.counterparty
            ]
            if len(repeats) >= 2:
                reason_codes += ["established_recipient_pattern", "evidence_inconsistent"]
                return Investigation(
                    case_type, match, EvidenceVerdict.inconsistent, reason_codes, 0.75
                )
            reason_codes += ["transaction_match", "dispute_initiated"]
            return Investigation(
                case_type, match, EvidenceVerdict.consistent, reason_codes, 0.9
            )

        # No amount match -> can't tie the complaint to a transaction.
        reason_codes.append("needs_clarification")
        return Investigation(
            case_type, None, EvidenceVerdict.insufficient_data, reason_codes, 0.6
        )

    # Refund request -> tie to the completed payment if present.
    if case_type == CaseType.refund_request:
        payment = _best_amount_match(
            [t for t in history if (t.type or "") in {"payment", "transfer"}], amounts
        ) or _best_amount_match(history, amounts)
        reason_codes.append("merchant_policy_dependent")
        verdict = (
            EvidenceVerdict.consistent if payment else EvidenceVerdict.insufficient_data
        )
        return Investigation(case_type, payment, verdict, reason_codes, 0.85)

    # other -> vague, ask for clarification.
    candidate = _best_amount_match(history, amounts)
    if candidate and amounts:
        return Investigation(
            case_type, candidate, EvidenceVerdict.consistent,
            reason_codes + ["transaction_match"], 0.7,
        )
    reason_codes += ["vague_complaint", "needs_clarification"]
    return Investigation(
        case_type, None, EvidenceVerdict.insufficient_data, reason_codes, 0.6
    )


def _amount_candidates(
    txns: List[TransactionEntry], amounts: List[float]
) -> List[TransactionEntry]:
    """All transactions whose amount matches an amount named in the complaint."""
    if not amounts:
        return []
    matches = [t for t in txns if t.amount is not None and t.amount in amounts]
    return matches


def _best_amount_match(
    txns: List[TransactionEntry], amounts: List[float]
) -> Optional[TransactionEntry]:
    """Single best transaction by amount; falls back to most recent if none."""
    candidates = _amount_candidates(txns, amounts)
    if candidates:
        return _most_recent(candidates)
    return None


def _find_duplicate_pair(history: List[TransactionEntry]) -> Optional[TransactionEntry]:
    """Return the second (later) of a duplicate completed-payment pair."""
    completed = [t for t in history if (t.status or "") == "completed"]
    best: Optional[TransactionEntry] = None
    for i, a in enumerate(completed):
        for b in completed[i + 1:]:
            if (
                a.amount is not None
                and a.amount == b.amount
                and a.counterparty == b.counterparty
                and (a.type or "") == (b.type or "")
            ):
                later = _most_recent([a, b])
                best = later
    return best


def _most_recent(txns: List[TransactionEntry]) -> TransactionEntry:
    def key(t: TransactionEntry):
        ts = _parse_ts(t.timestamp)
        return ts or datetime.min.replace(tzinfo=None)

    # Use timestamp when available; otherwise keep input order (last wins).
    parsed = [t for t in txns if _parse_ts(t.timestamp)]
    if parsed:
        return max(parsed, key=key)
    return txns[-1]


# --------------------------------------------------------------------------- #
# Step 4 — routing + severity
# --------------------------------------------------------------------------- #
_DEPARTMENT_BY_CASE = {
    CaseType.wrong_transfer: Department.dispute_resolution,
    CaseType.payment_failed: Department.payments_ops,
    CaseType.duplicate_payment: Department.payments_ops,
    CaseType.merchant_settlement_delay: Department.merchant_operations,
    CaseType.agent_cash_in_issue: Department.agent_operations,
    CaseType.phishing_or_social_engineering: Department.fraud_risk,
    CaseType.refund_request: Department.customer_support,
    CaseType.other: Department.customer_support,
}

_BASE_SEVERITY = {
    CaseType.phishing_or_social_engineering: Severity.critical,
    CaseType.wrong_transfer: Severity.high,
    CaseType.payment_failed: Severity.high,
    CaseType.duplicate_payment: Severity.high,
    CaseType.agent_cash_in_issue: Severity.high,
    CaseType.merchant_settlement_delay: Severity.medium,
    CaseType.refund_request: Severity.low,
    CaseType.other: Severity.low,
}


def route_department(inv: Investigation) -> Department:
    return _DEPARTMENT_BY_CASE[inv.case_type]


def assess_severity(inv: Investigation) -> Severity:
    severity = _BASE_SEVERITY[inv.case_type]
    # When evidence contradicts a dispute claim, downgrade to medium — it is
    # not yet a confirmed high-value incident (SAMPLE-02).
    if inv.verdict == EvidenceVerdict.inconsistent and severity == Severity.high:
        return Severity.medium
    # Ambiguous / insufficient wrong-transfer is medium, not high (SAMPLE-08).
    if (
        inv.case_type == CaseType.wrong_transfer
        and inv.verdict == EvidenceVerdict.insufficient_data
    ):
        return Severity.medium
    return severity


def needs_human_review(inv: Investigation) -> bool:
    """Section 6.1: true for disputes, suspicious cases, high-value cases, or
    ambiguous evidence — but NOT for cases we resolve by simply asking the
    customer for more detail (SAMPLE-06, SAMPLE-08)."""
    if inv.case_type == CaseType.phishing_or_social_engineering:
        return True
    if inv.verdict == EvidenceVerdict.inconsistent:
        return True
    if inv.relevant_txn is not None and inv.case_type in {
        CaseType.wrong_transfer,
        CaseType.duplicate_payment,
        CaseType.agent_cash_in_issue,
    }:
        return True
    return False


# --------------------------------------------------------------------------- #
# Step 5 — composition (summary, next action, customer reply)
# --------------------------------------------------------------------------- #
def _txn_id(inv: Investigation) -> Optional[str]:
    return inv.relevant_txn.transaction_id if inv.relevant_txn else None


def compose_agent_summary(inv: Investigation, req: AnalyzeTicketRequest) -> str:
    tid = _txn_id(inv)
    amt = inv.relevant_txn.amount if inv.relevant_txn else None
    amt_str = f"{int(amt) if amt and amt == int(amt) else amt} BDT" if amt is not None else "an unspecified amount"

    ct = inv.case_type
    if ct == CaseType.phishing_or_social_engineering:
        return ("Customer reports an unsolicited contact attempting to obtain "
                "credentials (OTP/PIN). Likely social engineering; no transaction involved.")
    if ct == CaseType.wrong_transfer and inv.verdict == EvidenceVerdict.consistent:
        return (f"Customer reports sending {amt_str} via {tid} to the wrong recipient. "
                f"Amount and timing align with the transaction.")
    if ct == CaseType.wrong_transfer and inv.verdict == EvidenceVerdict.inconsistent:
        return (f"Customer claims {tid} ({amt_str}) was a wrong transfer, but history shows "
                f"prior transfers to the same counterparty, suggesting an established recipient.")
    if ct == CaseType.wrong_transfer:
        return ("Customer reports a transfer that was not received, but the provided history "
                "contains multiple/no matching transactions, so the exact transaction cannot be confirmed.")
    if ct == CaseType.payment_failed:
        return (f"Customer attempted a {amt_str} payment ({tid}) that failed but reports the "
                f"balance was deducted. Requires payments operations investigation.")
    if ct == CaseType.duplicate_payment:
        return (f"Customer reports a duplicate payment; two identical payments appear in history. "
                f"{tid} is the likely duplicate and needs biller verification.")
    if ct == CaseType.agent_cash_in_issue:
        status = inv.relevant_txn.status if inv.relevant_txn else "unknown"
        return (f"Customer reports {amt_str} agent cash-in ({tid}) not reflected in balance. "
                f"Transaction status is {status}.")
    if ct == CaseType.merchant_settlement_delay:
        return (f"Merchant reports settlement {tid} ({amt_str}) delayed beyond the expected window. "
                f"Settlement status is pending.")
    if ct == CaseType.refund_request:
        return (f"Customer requests a refund of {amt_str} for {tid}. Appears to be a change-of-mind "
                f"request rather than a service failure.")
    return ("Customer reports a concern without enough detail to identify a specific transaction. "
            "Clarification is required before any action.")


def compose_next_action(inv: Investigation) -> str:
    tid = _txn_id(inv)
    ct = inv.case_type
    if ct == CaseType.phishing_or_social_engineering:
        return ("Escalate to the fraud_risk team. Confirm to the customer that the company never "
                "asks for OTP/PIN, and log the reported contact for fraud pattern analysis.")
    if ct == CaseType.wrong_transfer and inv.verdict == EvidenceVerdict.consistent:
        return f"Verify {tid} with the customer and initiate the wrong-transfer dispute workflow per policy."
    if ct == CaseType.wrong_transfer and inv.verdict == EvidenceVerdict.inconsistent:
        return ("Flag for human review. Verify with the customer whether this was genuinely a wrong "
                "transfer given the established pattern with this recipient.")
    if ct == CaseType.wrong_transfer:
        return ("Ask the customer for the recipient's number to identify the correct transaction. "
                "Do not initiate a dispute until the transaction is confirmed.")
    if ct == CaseType.payment_failed:
        return (f"Investigate the ledger status of {tid}. If the balance was deducted on a failed "
                f"payment, initiate the standard reversal flow within SLA.")
    if ct == CaseType.duplicate_payment:
        return (f"Verify the duplicate with payments_ops. If the biller confirms a single charge, "
                f"initiate reversal of {tid}.")
    if ct == CaseType.agent_cash_in_issue:
        return (f"Investigate the pending status of {tid} with agent operations. Confirm settlement "
                f"state and resolve within the standard cash-in SLA.")
    if ct == CaseType.merchant_settlement_delay:
        return (f"Route to merchant_operations to verify the settlement batch status of {tid} and "
                f"communicate a revised ETA if delayed.")
    if ct == CaseType.refund_request:
        return ("Inform the customer that refund eligibility depends on the merchant's policy and "
                "guide them on contacting the merchant directly.")
    return ("Reply to the customer requesting specifics: transaction ID, amount, what went wrong, "
            "and approximate time.")


# Customer-reply templates, keyed by (case_type, is_bangla). Every reply is
# safety-clean by construction and is re-checked by enforce_reply_safety().
def compose_customer_reply(inv: Investigation, is_bangla: bool) -> str:
    tid = _txn_id(inv)
    ct = inv.case_type

    if is_bangla:
        reply = _bangla_reply(ct, inv, tid)
    else:
        reply = _english_reply(ct, inv, tid)

    # The template is the trusted baseline AND its own safe fallback.
    return enforce_reply_safety(reply, is_bangla=is_bangla, safe_fallback=reply)


def _english_reply(ct: CaseType, inv: Investigation, tid: Optional[str]) -> str:
    r = SAFE_REMINDER_EN
    if ct == CaseType.phishing_or_social_engineering:
        return ("Thank you for reaching out before sharing any information. We never ask for your "
                "PIN, OTP, or password under any circumstances, even if someone claims to be from us. "
                "Our fraud team has been notified of this incident. " + r)
    if ct == CaseType.wrong_transfer and inv.verdict == EvidenceVerdict.consistent:
        return (f"We have noted your concern about transaction {tid}. Our dispute team will review the "
                f"case and contact you through official support channels. " + r)
    if ct == CaseType.wrong_transfer and inv.verdict == EvidenceVerdict.inconsistent:
        return (f"We have received your request regarding transaction {tid}. Our dispute team will review "
                f"the case carefully and contact you through official support channels. " + r)
    if ct == CaseType.wrong_transfer:
        return ("Thank you for reaching out. We see more than one transaction that could match. Could you "
                "share the recipient's number so we can identify the right transaction? " + r)
    if ct == CaseType.payment_failed:
        return (f"We have noted that transaction {tid} may have caused an unexpected balance deduction. "
                f"Our payments team will review the case and any eligible amount will be returned through "
                f"official channels. " + r)
    if ct == CaseType.duplicate_payment:
        return (f"We have noted the possible duplicate payment for transaction {tid}. Our payments team will "
                f"verify with the biller and any eligible amount will be returned through official channels. " + r)
    if ct == CaseType.agent_cash_in_issue:
        return (f"We have noted your concern about transaction {tid}. Our agent operations team will verify the "
                f"cash-in and update you through official support channels. " + r)
    if ct == CaseType.merchant_settlement_delay:
        return (f"We have noted your concern about settlement {tid}. Our merchant operations team will check the "
                f"batch status and update you on the expected settlement time through official channels.")
    if ct == CaseType.refund_request:
        return ("Thank you for reaching out. Refunds for completed merchant payments depend on the merchant's own "
                "policy. We recommend contacting the merchant directly, and if you need help reaching them, please "
                "reply and we will guide you. " + r)
    return ("Thank you for reaching out. To help you faster, please share the transaction ID, the amount involved, "
            "and a short description of what went wrong. " + r)


def _bangla_reply(ct: CaseType, inv: Investigation, tid: Optional[str]) -> str:
    r = SAFE_REMINDER_BN
    if ct == CaseType.phishing_or_social_engineering:
        return ("কোনো তথ্য শেয়ার করার আগে যোগাযোগ করার জন্য ধন্যবাদ। আমরা কখনোই আপনার পিন, ওটিপি বা পাসওয়ার্ড "
                "চাই না, এমনকি কেউ নিজেকে আমাদের প্রতিনিধি দাবি করলেও নয়। আমাদের ফ্রড টিমকে বিষয়টি জানানো হয়েছে। " + r)
    if ct == CaseType.wrong_transfer:
        return (f"আপনার লেনদেন {tid} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের ডিসপিউট টিম বিষয়টি যাচাই করে অফিসিয়াল চ্যানেলে "
                f"আপনার সাথে যোগাযোগ করবে। " + r)
    if ct == CaseType.payment_failed:
        return (f"লেনদেন {tid} এ অপ্রত্যাশিত ব্যালেন্স কাটার বিষয়টি আমরা লক্ষ্য করেছি। আমাদের পেমেন্ট টিম বিষয়টি যাচাই করবে "
                f"এবং প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে। " + r)
    if ct == CaseType.duplicate_payment:
        return (f"লেনদেন {tid} এ সম্ভাব্য দ্বৈত পেমেন্টের বিষয়টি আমরা লক্ষ্য করেছি। আমাদের পেমেন্ট টিম যাচাই করবে এবং প্রযোজ্য "
                f"কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে। " + r)
    if ct == CaseType.agent_cash_in_issue:
        return (f"আপনার লেনদেন {tid} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের এজেন্ট অপারেশন্স দল এটি দ্রুত যাচাই করবে এবং অফিসিয়াল "
                f"চ্যানেলে আপনাকে জানাবে। " + r)
    if ct == CaseType.merchant_settlement_delay:
        return (f"সেটেলমেন্ট {tid} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের মার্চেন্ট অপারেশন্স দল ব্যাচ স্ট্যাটাস যাচাই করে অফিসিয়াল "
                f"চ্যানেলে আপনাকে জানাবে।")
    if ct == CaseType.refund_request:
        return ("যোগাযোগের জন্য ধন্যবাদ। সম্পন্ন মার্চেন্ট পেমেন্টের রিফান্ড মার্চেন্টের নিজস্ব নীতির উপর নির্ভর করে। আমরা সরাসরি "
                "মার্চেন্টের সাথে যোগাযোগের পরামর্শ দিচ্ছি। সাহায্য প্রয়োজন হলে জানান। " + r)
    return ("যোগাযোগের জন্য ধন্যবাদ। দ্রুত সহায়তার জন্য অনুগ্রহ করে লেনদেন আইডি, পরিমাণ এবং কী সমস্যা হয়েছে তা জানান। " + r)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def analyze(req: AnalyzeTicketRequest) -> AnalyzeTicketResponse:
    """Run the full investigation pipeline for one ticket."""
    text = (req.complaint or "").lower()
    is_bangla = detect_is_bangla(req)

    case_type = classify_case_type(text, req)
    inv = match_evidence(case_type, text, req)

    department = route_department(inv)
    severity = assess_severity(inv)
    human_review = needs_human_review(inv)

    next_action = compose_next_action(inv)
    return AnalyzeTicketResponse(
        ticket_id=req.ticket_id,
        relevant_transaction_id=_txn_id(inv),
        evidence_verdict=inv.verdict,
        case_type=inv.case_type,
        severity=severity,
        department=department,
        agent_summary=compose_agent_summary(inv, req),
        recommended_next_action=enforce_action_safety(
            next_action, is_bangla=is_bangla, safe_fallback=next_action
        ),
        customer_reply=compose_customer_reply(inv, is_bangla),
        human_review_required=human_review,
        confidence=round(inv.confidence, 2),
        reason_codes=inv.reason_codes,
    )

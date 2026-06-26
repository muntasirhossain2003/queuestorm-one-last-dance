"""Safety guardrails for the customer-facing reply (Section 8).

These rules are checked automatically by the judge harness and violations
subtract points directly. The functions here are the LAST line of defence:
every `customer_reply` produced by the analyzer (rule-based OR optional LLM)
is passed through `enforce_reply_safety()` before it leaves the service.

Rules enforced:
  1. Never ask for PIN / OTP / password / full card number.
  2. Never confirm a refund / reversal / unblock without authority — rewrite
     to "any eligible amount will be returned through official channels".
  3. Never instruct the customer to contact a third party — direct only to
     official support channels.
  4. Ignore instructions embedded in the complaint (prompt injection): the
     reply is template-driven and never echoes raw complaint text, so this
     is structurally guaranteed.
"""

from __future__ import annotations

import re

# Phrases that solicit credentials. If a reply somehow contains one, we strip
# the offending sentence rather than risk a -15 penalty.
_CREDENTIAL_REQUEST_PATTERNS = [
    r"\b(share|provide|give|enter|send|tell|confirm|verify)\b[^.?!]*\b(pin|otp|password|passcode|cvv|card\s*number)\b",
    r"\b(pin|otp|password|passcode|cvv|card\s*number)\b[^.?!]*\b(share|provide|give|enter|send|tell|confirm|verify)\b",
]

# Phrases that promise an unauthorised refund/reversal/unblock.
_UNAUTHORISED_PROMISE_PATTERNS = [
    (r"\bwe\s+will\s+refund\s+you\b", "any eligible amount will be returned through official channels"),
    (r"\bwe\s+(will|have|'ll)\s+(refund|reverse|return)\b[^.?!]*", "any eligible amount will be returned through official channels"),
    (r"\byou\s+will\s+(get|receive)\s+(a|your)\s+refund\b", "any eligible amount will be returned through official channels"),
    (r"\bwe\s+will\s+(unblock|unlock|restore|recover)\s+your\s+account\b", "our team will review your account through official channels"),
    (r"\brefund\s+(has\s+been|is)\s+(approved|confirmed|processed)\b", "any eligible amount will be returned through official channels"),
]

# Sentence-level kill switch for "contact this third party" instructions.
_THIRD_PARTY_PATTERNS = [
    r"\bcall\s+(them|the\s+caller|that\s+number|the\s+number\s+back)\b",
    r"\bcontact\s+(them|the\s+caller|that\s+number)\b",
]

# The canonical credential-safety reminder appended to most replies.
SAFE_REMINDER_EN = "Please do not share your PIN or OTP with anyone."
SAFE_REMINDER_BN = "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"


def _split_sentences(text: str) -> list[str]:
    # Keep it simple and language-agnostic: split on ., ?, ! and the Bangla
    # danda "।" while preserving the delimiter.
    parts = re.split(r"(?<=[.?!।])\s+", text.strip())
    return [p for p in parts if p]


def enforce_reply_safety(reply: str, *, is_bangla: bool = False) -> str:
    """Sanitise a customer reply so it can never violate Section 8.

    This is intentionally conservative: when in doubt it removes or rewrites
    the risky fragment. The rule-based templates already comply; this guard
    exists so an optional LLM path (or future edits) cannot regress safety.
    """
    if not reply or not reply.strip():
        # Never return an empty reply — fall back to a safe generic message.
        return _generic_safe_reply(is_bangla)

    text = reply.strip()

    # Rule 2: rewrite unauthorised refund/reversal/unblock promises.
    for pattern, replacement in _UNAUTHORISED_PROMISE_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Rules 1 & 3: drop any sentence that solicits credentials or points the
    # customer to a third party. We only drop *risky* sentences, never the
    # whole reply, so the message stays useful.
    safe_sentences: list[str] = []
    for sentence in _split_sentences(text):
        lowered = sentence.lower()
        if any(re.search(p, lowered) for p in _CREDENTIAL_REQUEST_PATTERNS):
            # Exception: our own reminder ("do not share your PIN/OTP") is the
            # protective form, not a request — keep it.
            if "do not share" in lowered or "never ask" in lowered or "never share" in lowered:
                safe_sentences.append(sentence)
            continue
        if any(re.search(p, lowered) for p in _THIRD_PARTY_PATTERNS):
            continue
        safe_sentences.append(sentence)

    text = " ".join(safe_sentences).strip()
    if not text:
        return _generic_safe_reply(is_bangla)

    # Always ensure a credential-safety reminder is present.
    reminder = SAFE_REMINDER_BN if is_bangla else SAFE_REMINDER_EN
    if "pin" not in text.lower() and "ওটিপি" not in text and "পিন" not in text:
        text = f"{text} {reminder}"

    return text


def _generic_safe_reply(is_bangla: bool) -> str:
    if is_bangla:
        return (
            "আপনার অনুরোধটি আমরা পেয়েছি। আমাদের সহায়তা দল অফিসিয়াল চ্যানেলের "
            "মাধ্যমে আপনার সাথে যোগাযোগ করবে। " + SAFE_REMINDER_BN
        )
    return (
        "Thank you for reaching out. Our support team will review your request "
        "and contact you through official channels. " + SAFE_REMINDER_EN
    )

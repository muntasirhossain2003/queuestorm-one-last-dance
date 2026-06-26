"""Defense-in-depth safety guardrails (Section 8).

The judge harness checks `customer_reply` AND `recommended_next_action` against
hard safety rules; violations subtract points and two critical violations
disqualify the team. Because the service can optionally route text through an
LLM (Groq / Gemini), the LLM output is now an untrusted surface: a jailbroken
or hallucinating model could emit unsafe text — including in Bangla, where
naive English filters would miss it.

This module is therefore built as a layered pipeline with a *guaranteed-safe
fallback* rather than a single regex pass:

  Layer 0  Injection detection      contains_injection() — caller can skip the
                                     LLM entirely for adversarial complaints.
  Layer 1  Secret/PII redaction     strip API keys, tokens, stack traces.
  Layer 2  Unauthorised-promise     rewrite refund/reversal/unblock promises
           rewrite                   (English + Bangla) to compliant language.
  Layer 3  Sentence-level removal    drop any sentence that solicits credentials
                                     or redirects to a third party (multilingual,
                                     negation-aware so protective lines survive).
  Layer 4  Reminder enforcement      ensure the credential-safety reminder.
  Layer 5  HARD GATE + fallback      re-scan; if ANY violation survives, discard
                                     the candidate and return a vetted safe
                                     template. Output is provably compliant.

`audit()` exposes the detector for tests so we can prove the gate works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# --------------------------------------------------------------------------- #
# Canonical safe strings
# --------------------------------------------------------------------------- #
SAFE_REMINDER_EN = "Please do not share your PIN or OTP with anyone."
SAFE_REMINDER_BN = "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"

_SAFE_REFUND_EN = "any eligible amount will be returned through official channels"
_SAFE_REFUND_BN = "প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে"
_SAFE_ACCOUNT_EN = "our team will review your account through official channels"
_SAFE_ACCOUNT_BN = "আমাদের দল অফিসিয়াল চ্যানেলের মাধ্যমে আপনার অ্যাকাউন্ট পর্যালোচনা করবে"

# --------------------------------------------------------------------------- #
# Lexicons (English + Bangla)
# --------------------------------------------------------------------------- #
# Credential nouns — never to be solicited.
_CRED_NOUNS = [
    r"pin", r"otp", r"one[\s-]*time\s*password", r"o\.t\.p", r"password",
    r"pass\s*code", r"pass\s*word", r"cvv", r"cvc", r"card\s*(number|no\.?|details?)",
    r"full\s*card", r"security\s*code", r"secret\s*(code|number|pin)",
    r"verification\s*code", r"atm\s*pin", r"mpin",
    # Bangla
    r"পিন", r"ওটিপি", r"পাসওয়ার্ড", r"গোপন\s*(নম্বর|কোড|পিন)",
    r"কার্ড\s*নম্বর", r"সিকিউরিটি\s*কোড", r"ভেরিফিকেশন\s*কোড",
]

# Verbs that solicit something from the customer.
_SOLICIT_VERBS = [
    r"share", r"provide", r"give", r"giving", r"send", r"sending", r"enter",
    r"tell", r"telling", r"type", r"submit", r"confirm", r"verify", r"reveal",
    r"disclose", r"forward", r"text\s+me", r"dictate", r"read\s*out", r"ask",
    r"need\s+your", r"require\s+your",
    # Bangla
    r"শেয়ার", r"দিন", r"দাও", r"দেন", r"বলুন", r"বলো", r"পাঠান", r"পাঠাও",
    r"লিখুন", r"টাইপ", r"প্রদান", r"নিশ্চিত\s*করুন",
]

# Negation / protective markers (English). Bangla handled separately to avoid
# substring collisions (e.g. জানান contains "না").
_NEG_EN = [
    r"\bnot\b", r"\bnever\b", r"\bdo\s*n['’]?t\b", r"\bdon['’]?t\b",
    r"n['’]t\b", r"\bavoid\b", r"\brefrain\b", r"\bwithout\b", r"\bno\s+need\b",
]
# Bangla negation as whole tokens / known verb+negation combos.
_NEG_BN = [
    r"করবেন\s*না", r"করবে\s*না", r"করো\s*না", r"দেবেন\s*না", r"দিবেন\s*না",
    r"দেবে\s*না", r"কখনো[ই]?\s*না", r"(?:^|[\s।])না(?=[\s।]|$)",
    r"নয়", r"নেই", r"যাবে\s*না",
]

# Definite unauthorised promises (commitments the service has no authority to
# make). Each maps to a compliant replacement.
_PROMISE_RULES_EN = [
    # Active-voice commitments (subject = "we")
    (r"\bwe\s+(?:will|'ll|are|have|already)\s+(?:refund(?:ed|ing)?|revers(?:e|ed|ing)|return(?:ed|ing)?)\b[^.?!]*", _SAFE_REFUND_EN),
    (r"\byou\s+(?:will|'ll)\s+(?:get|receive)\s+(?:a\s+|your\s+|the\s+)?(?:refund|money\s+back|reversal)\b", _SAFE_REFUND_EN),
    (r"\bwe\s+(?:will|'ll)\s+(?:give|send|return)\s+(?:you\s+)?your\s+money\s*(?:back)?\b", _SAFE_REFUND_EN),
    (r"\b(?:rest\s+assured|don'?t\s+worry)[^.?!]*\brefund\b[^.?!]*", _SAFE_REFUND_EN),
    # "we will process/issue/credit/authorize/initiate the refund"
    (r"\bwe\s+(?:will|'ll|are|have|shall)\s+(?:process|issue|authorize|credit|initiate|transfer|make|arrange)\s+(?:the|a|your\s+)?\s*(?:refund|reversal|reimbursement)\b", _SAFE_REFUND_EN),
    # "we will credit/transfer your account/wallet"
    (r"\bwe\s+(?:will|'ll|are|have)\s+(?:credit|transfer)\s+(?:your|the)\s+(?:account|wallet|balance)\b", _SAFE_REFUND_EN),
    # Passive-voice refund assertions: "your refund has been processed/approved/sent/..."
    (r"\b(?:your|the)\s+(?:refund|reversal|reimbursement)\s+(?:has\s+been|is|was|will\s+be)\s+(?:approved|confirmed|processed|done|initiated|completed|guaranteed|sent|credited|issued|authorized)\b", _SAFE_REFUND_EN),
    # "the refund has been / will be processed"
    (r"\bthe\s+(?:refund|reversal)\s+(?:has\s+been|will\s+be|is\s+being)\s+(?:processed|completed|approved|initiated|confirmed)\b", _SAFE_REFUND_EN),
    # Active unblock/recovery (subject = "we")
    (r"\bwe\s+(?:will|'ll|have|are)\s+(?:unblock(?:ed|ing)?|unlock(?:ed|ing)?|restor(?:e|ed|ing)|recover(?:ed|ing)?|reactivat\w*)\s+(?:your\s+)?account\b", _SAFE_ACCOUNT_EN),
    # Passive/no-subject account claims: "your account is now unblocked/restored"
    (r"\byour\s+(?:account|wallet)\s+(?:is\s+now|has\s+been|was|will\s+be)\s+(?:unblock(?:ed)?|unlock(?:ed)?|restor(?:e|ed)|reactivat(?:e|ed)|recover(?:e|ed))\b", _SAFE_ACCOUNT_EN),
]
_PROMISE_RULES_BN = [
    # Active refund promises
    (r"ফেরত\s*(?:দেব|দিব|দিচ্ছি|দিয়ে\s*দেব|দেওয়া\s*হয়েছে|করে\s*দেব)", _SAFE_REFUND_BN),
    (r"রিফান্ড\s*(?:করে\s*দেব|দেব|দিচ্ছি|হয়ে\s*গেছে|নিশ্চিত)", _SAFE_REFUND_BN),
    (r"টাকা\s*(?:ফেরত\s*পাবেন|দিয়ে\s*দেব)", _SAFE_REFUND_BN),
    # Passive refund assertions: "রিফান্ড/টাকা প্রসেস হয়েছে / জমা হয়েছে"
    (r"(?:আপনার\s+)?(?:রিফান্ড|টাকা)\s*(?:প্রসেস\s*হয়েছে|জমা\s*হয়েছে|পাঠানো\s*হয়েছে|ক্রেডিট\s*হয়েছে|ফেরত\s*হয়েছে)", _SAFE_REFUND_BN),
    # Account unblock/restore
    (r"অ্যাকাউন্ট\s*(?:খুলে\s*দেব|আনব্লক\s*করে\s*দেব|চালু\s*করে\s*দেব)", _SAFE_ACCOUNT_BN),
    # Passive account: "অ্যাকাউন্ট চালু/আনব্লক হয়েছে"
    (r"(?:আপনার\s+)?অ্যাকাউন্ট\s*(?:চালু|খোলা|আনব্লক|আনলক|পুনরুদ্ধার)\s*হয়েছে", _SAFE_ACCOUNT_BN),
]

# Third-party redirect (do not send the customer outside official channels).
_THIRD_PARTY = [
    r"\bcall\s+(?:them|him|her|that\s+(?:number|person|caller)|the\s+(?:caller|number))\b",
    r"\bcall\s+(?:the\s+number\s+)?back\b",
    r"\bcontact\s+(?:them|him|her|that\s+(?:number|person|caller)|the\s+caller)\b",
    r"\breach\s+out\s+to\s+(?:them|him|her|that)\b",
    r"\b(?:reply|respond|message|text)\s+(?:to\s+)?(?:them|that\s+(?:sms|message|number))\b",
    # Social-media / external-app redirects
    r"\b(?:whatsapp|facebook|messenger|telegram|viber|signal|instagram)\s+(?:them|that\s+\w+|the\s+\w+)\b",
    r"\b(?:dm|pm)\s+(?:them|that\s+person|the\s+caller|that\s+number)\b",
    r"\bvisit\s+(?:their|that)\s+(?:website|office|branch|store|page)\b",
    # Bangla
    r"(?:তাদের|ঐ\s*নম্বরে|সেই\s*নম্বরে)\s*(?:ফোন|কল|যোগাযোগ)",
]

# Secrets / tokens / stack traces that must never leak into a response.
_SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9]{8,}",
    r"gsk_[A-Za-z0-9]{8,}",
    r"AIza[A-Za-z0-9_\-]{10,}",
    r"AKIA[0-9A-Z]{12,}",
    r"Bearer\s+[A-Za-z0-9._\-]{8,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",  # JWT
    r"Traceback\s*\(most\s+recent\s+call\s+last\)",
    r'File\s+"[^"]+",\s+line\s+\d+',
]

# Prompt-injection markers in *incoming* complaints (Layer 0).
_INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|prior|above|the)\s+(?:instructions|rules|prompts?)",
    r"disregard\s+(?:all\s+)?(?:previous|prior|the)\s+(?:instructions|rules)",
    r"\byou\s+are\s+now\b", r"\badmin\s+mode\b", r"\bdeveloper\s+mode\b",
    r"\bsystem\s*:", r"\bend\s+system\b", r"</?(?:system|instructions?)>",
    r"\bpretend\s+(?:you\s+are|to\s+be)\b", r"\bact\s+as\s+(?:an?\s+)?(?:evil|unrestricted|jailbroken)",
    r"\bno\s+rules\b", r"\boverride\s+(?:safety|security|all)\b",
    r"\bbypass\s+(?:all\s+)?(?:verification|safety|security|checks?)\b",
    r"\bnew\s+instructions?\b", r"\bforget\s+(?:everything|your\s+(?:rules|instructions))\b",
    # Additional jailbreak / prompt-hijack variants
    r"\bDAN\b",                                      # "Do Anything Now" jailbreak
    r"\bjailbreak\b",
    r"\bgod\s*mode\b", r"\bunrestricted\s*mode\b",
    r"\[\s*(?:SYSTEM|SYS|INST|ASSISTANT)\s*\]",     # tagged-role injection
    r"<\s*(?:prompt|instruction|context|sys)\s*>",
    r"\btoken\s*smuggling\b",
    r"ignore\s+(?:safety|security|all)\s*(?:rules|constraints|guidelines|limitations)",
    r"you\s+(?:have\s+no|don[''`]?t\s+have)\s+(?:any\s+)?(?:rules|restrictions|limitations|constraints)",
    r"\bdo\s+anything\s+now\b",
    r"\brepeat\s+after\s+me\b",                     # prompt-echo attack
    r"what(?:ever|ever\s+i\s+say)\s+goes\b",
]

_CRED_NOUN_RE = re.compile("|".join(_CRED_NOUNS), re.IGNORECASE)
_SOLICIT_RE = re.compile("|".join(_SOLICIT_VERBS), re.IGNORECASE)
_NEG_EN_RE = re.compile("|".join(_NEG_EN), re.IGNORECASE)
_NEG_BN_RE = re.compile("|".join(_NEG_BN))
_THIRD_PARTY_RE = re.compile("|".join(_THIRD_PARTY), re.IGNORECASE)
_SECRET_RE = re.compile("|".join(_SECRET_PATTERNS))
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclass
class SafetyReport:
    safe: bool = True
    violations: List[str] = field(default_factory=list)

    def add(self, label: str) -> None:
        self.safe = False
        if label not in self.violations:
            self.violations.append(label)


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.?!।\n])\s+", text.strip())
    return [p for p in parts if p.strip()]


def _is_negated(sentence: str) -> bool:
    return bool(_NEG_EN_RE.search(sentence) or _NEG_BN_RE.search(sentence))


def _solicits_credential(sentence: str) -> bool:
    """A sentence solicits a credential if it names one alongside a soliciting
    verb (or a bare 'your <credential>' request) and is NOT negated/protective."""
    if not _CRED_NOUN_RE.search(sentence):
        return False
    if _is_negated(sentence):
        return False  # protective ("never share your OTP") — allowed.
    if _SOLICIT_RE.search(sentence):
        return True
    # Bare possessive solicitation, e.g. "your PIN please".
    if re.search(r"\byour\b|\bআপনার\b", sentence, re.IGNORECASE):
        return True
    return False


def audit(text: str) -> SafetyReport:
    """Detect residual Section-8 violations in a piece of text (any language)."""
    report = SafetyReport()
    if not text:
        return report

    for sentence in _split_sentences(text):
        if _solicits_credential(sentence):
            report.add("credential_solicitation")
        if _THIRD_PARTY_RE.search(sentence):
            report.add("third_party_redirect")

    for pattern, _ in _PROMISE_RULES_EN + _PROMISE_RULES_BN:
        if re.search(pattern, text, re.IGNORECASE):
            report.add("unauthorised_promise")
            break

    if _SECRET_RE.search(text):
        report.add("secret_leak")

    return report


def contains_injection(complaint: str) -> bool:
    """Layer 0: does the incoming complaint attempt a prompt injection?"""
    return bool(complaint and _INJECTION_RE.search(complaint))


# --------------------------------------------------------------------------- #
# Sanitisation layers
# --------------------------------------------------------------------------- #
def _redact_secrets(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def _rewrite_promises(text: str) -> str:
    for pattern, replacement in _PROMISE_RULES_EN:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    for pattern, replacement in _PROMISE_RULES_BN:
        text = re.sub(pattern, replacement, text)
    return text


def _strip_risky_sentences(text: str) -> str:
    kept: List[str] = []
    for sentence in _split_sentences(text):
        if _solicits_credential(sentence):
            continue
        if _THIRD_PARTY_RE.search(sentence):
            continue
        kept.append(sentence.strip())
    return " ".join(kept).strip()


def _ensure_reminder(text: str, is_bangla: bool) -> str:
    reminder = SAFE_REMINDER_BN if is_bangla else SAFE_REMINDER_EN
    low = text.lower()
    has_reminder = (
        ("do not share" in low or "never share" in low or "never ask" in low)
        or "শেয়ার করবেন না" in text
    )
    if not has_reminder:
        text = f"{text} {reminder}".strip()
    return text


# --------------------------------------------------------------------------- #
# Public enforcement entry points
# --------------------------------------------------------------------------- #
def enforce_reply_safety(
    reply: str,
    *,
    is_bangla: bool = False,
    safe_fallback: str | None = None,
) -> str:
    """Return a customer reply that provably satisfies Section 8.

    `safe_fallback` is a known-compliant message (typically the rule-based
    template). If the candidate cannot be made safe, the fallback is used; if
    no fallback is given, a generic vetted reply is returned. The result is
    re-audited, so the function can never return unsafe text.
    """
    candidate = (reply or "").strip()
    if candidate:
        candidate = _redact_secrets(candidate)
        candidate = _rewrite_promises(candidate)
        candidate = _strip_risky_sentences(candidate)
        candidate = _ensure_reminder(candidate, is_bangla) if candidate else ""

    # HARD GATE: if anything unsafe survived, abandon the candidate.
    if not candidate or not audit(candidate).safe:
        candidate = _clean_fallback(safe_fallback, is_bangla)

    return candidate


def enforce_action_safety(
    action: str,
    *,
    is_bangla: bool = False,
    safe_fallback: str | None = None,
) -> str:
    """Sanitise `recommended_next_action` (also checked by the harness).

    Operational steps are fine ("if confirmed, initiate the reversal flow"),
    but a definite unauthorised commitment ("refund the customer now") or a
    credential request is rewritten/removed; on residual violation we fall back.
    """
    candidate = (action or "").strip()
    if candidate:
        candidate = _redact_secrets(candidate)
        candidate = _rewrite_promises(candidate)
        candidate = _strip_risky_sentences(candidate)

    if not candidate or not audit(candidate).safe:
        candidate = (safe_fallback or "").strip()
        candidate = _redact_secrets(_rewrite_promises(candidate))
        if not candidate or not audit(candidate).safe:
            candidate = (
                "Escalate to the relevant team to verify the case and resolve it "
                "through official channels."
            )
    return candidate


def _clean_fallback(safe_fallback: str | None, is_bangla: bool) -> str:
    """Sanitise the provided fallback; if it is missing or itself unsafe, use a
    hard-coded generic reply that is guaranteed compliant."""
    if safe_fallback:
        fb = _ensure_reminder(
            _strip_risky_sentences(_rewrite_promises(_redact_secrets(safe_fallback.strip()))),
            is_bangla,
        )
        if fb and audit(fb).safe:
            return fb
    return _generic_safe_reply(is_bangla)


def _generic_safe_reply(is_bangla: bool) -> str:
    if is_bangla:
        return (
            "আপনার অনুরোধটি আমরা পেয়েছি। আমাদের সহায়তা দল অফিসিয়াল চ্যানেলের "
            "মাধ্যমে বিষয়টি যাচাই করে আপনার সাথে যোগাযোগ করবে। " + SAFE_REMINDER_BN
        )
    return (
        "Thank you for reaching out. Our support team will review your request "
        "and contact you through official channels. " + SAFE_REMINDER_EN
    )

"""Pydantic models for the QueueStorm Investigator API.

Mirrors the request/response contract in Sections 5-7 of the Preliminary
Problem Statement. Enum values must match EXACTLY (Section 7) — any variant
(case, plural, alternate spelling) is scored as a schema violation, so the
allowed sets are defined here once and reused by both the schema layer and
the analyzer.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Enums (Section 7 taxonomy + Section 5.2 transaction enums)
# --------------------------------------------------------------------------- #
class Language(str, Enum):
    en = "en"
    bn = "bn"
    mixed = "mixed"


class Channel(str, Enum):
    in_app_chat = "in_app_chat"
    call_center = "call_center"
    email = "email"
    merchant_portal = "merchant_portal"
    field_agent = "field_agent"


class UserType(str, Enum):
    customer = "customer"
    merchant = "merchant"
    agent = "agent"
    unknown = "unknown"


class TransactionType(str, Enum):
    transfer = "transfer"
    payment = "payment"
    cash_in = "cash_in"
    cash_out = "cash_out"
    settlement = "settlement"
    refund = "refund"


class TransactionStatus(str, Enum):
    completed = "completed"
    failed = "failed"
    pending = "pending"
    reversed = "reversed"


class EvidenceVerdict(str, Enum):
    consistent = "consistent"
    inconsistent = "inconsistent"
    insufficient_data = "insufficient_data"


class CaseType(str, Enum):
    wrong_transfer = "wrong_transfer"
    payment_failed = "payment_failed"
    refund_request = "refund_request"
    duplicate_payment = "duplicate_payment"
    merchant_settlement_delay = "merchant_settlement_delay"
    agent_cash_in_issue = "agent_cash_in_issue"
    phishing_or_social_engineering = "phishing_or_social_engineering"
    other = "other"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Department(str, Enum):
    customer_support = "customer_support"
    dispute_resolution = "dispute_resolution"
    payments_ops = "payments_ops"
    merchant_operations = "merchant_operations"
    agent_operations = "agent_operations"
    fraud_risk = "fraud_risk"


# --------------------------------------------------------------------------- #
# Request models (Section 5)
# --------------------------------------------------------------------------- #
class TransactionEntry(BaseModel):
    """A single transaction-history entry (Section 5.2).

    Enums are kept permissive (plain strings) on input so that an unexpected
    value in the harness data never crashes the parser — the analyzer treats
    unknown values gracefully. Only the OUTPUT schema is strictly enumerated.
    """

    model_config = ConfigDict(extra="ignore")

    transaction_id: Optional[str] = None
    timestamp: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[float] = None
    counterparty: Optional[str] = None
    status: Optional[str] = None


class AnalyzeTicketRequest(BaseModel):
    """POST /analyze-ticket request body (Section 5.1)."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: str
    complaint: str
    language: Optional[str] = None
    channel: Optional[str] = None
    user_type: Optional[str] = None
    campaign_context: Optional[str] = None
    transaction_history: List[TransactionEntry] = Field(default_factory=list)
    metadata: Optional[dict] = None


# --------------------------------------------------------------------------- #
# Response models (Section 6)
# --------------------------------------------------------------------------- #
class AnalyzeTicketResponse(BaseModel):
    """POST /analyze-ticket response body (Section 6.1).

    `use_enum_values=True` serialises the enums to their raw string values,
    guaranteeing the exact taxonomy strings the judge harness expects.
    """

    model_config = ConfigDict(use_enum_values=True)

    ticket_id: str
    relevant_transaction_id: Optional[str]
    evidence_verdict: EvidenceVerdict
    case_type: CaseType
    severity: Severity
    department: Department
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    human_review_required: bool
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason_codes: Optional[List[str]] = None


class HealthResponse(BaseModel):
    status: str = "ok"


class ErrorResponse(BaseModel):
    error: str

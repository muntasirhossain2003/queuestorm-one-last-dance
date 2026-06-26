"""FastAPI application exposing the QueueStorm Investigator API.

Endpoints (Section 4):
  GET  /health         -> {"status":"ok"}   (readiness within 60s of start)
  POST /analyze-ticket -> structured analysis (must respond within 30s)

HTTP status handling (Section 4.1) is done MANUALLY rather than relying on
FastAPI's default 422-for-everything behaviour, so we can return:
  400  malformed JSON / missing required fields
  422  schema valid but semantically invalid (e.g. empty complaint)
  500  internal error (non-sensitive message only — never a stack trace)

The service must never crash on malformed input; every path is wrapped so a
bad request yields a clean 400/422/500, never a dropped connection.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .analyzer import analyze, detect_is_bangla
from .llm_analyzer import maybe_enhance
from .schemas import AnalyzeTicketRequest, AnalyzeTicketResponse, HealthResponse

logger = logging.getLogger("queuestorm")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="QueueStorm Investigator",
    description="AI/API SupportOps copilot for digital finance complaints.",
    version=__version__,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness/readiness probe used by the judge harness before sending cases."""
    return HealthResponse(status="ok")


@app.post("/analyze-ticket")
async def analyze_ticket(request: Request) -> JSONResponse:
    """Analyse a single support ticket and return a structured verdict."""
    # 1. Parse JSON body — malformed JSON is a 400, never a crash.
    try:
        raw = await request.body()
        if not raw:
            return _error(400, "Request body is empty; a JSON object is required.")
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _error(400, "Malformed JSON in request body.")
    except Exception:  # pragma: no cover - defensive
        return _error(400, "Could not read request body.")

    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object.")

    # 2. Validate against the request schema. Missing required fields -> 400.
    try:
        ticket = AnalyzeTicketRequest.model_validate(payload)
    except ValidationError as exc:
        missing = _missing_required(exc)
        if missing:
            return _error(400, f"Missing or invalid required field(s): {', '.join(missing)}.")
        return _error(400, "Request does not match the expected schema.")

    # 3. Semantic validation — schema is valid but content is unusable -> 422.
    if not ticket.complaint or not ticket.complaint.strip():
        return _error(422, "The 'complaint' field must not be empty.")

    # 4. Run the investigation. Any unexpected failure -> 500 (no internals).
    try:
        result: AnalyzeTicketResponse = analyze(ticket)
        is_bangla = detect_is_bangla(ticket)
        result = await maybe_enhance(result, ticket, is_bangla)
        return JSONResponse(status_code=200, content=result.model_dump())
    except Exception:  # pragma: no cover - defensive
        logger.exception("Unhandled error while analysing ticket %s", ticket.ticket_id)
        return _error(500, "Internal error while analysing the ticket.")


def _missing_required(exc: ValidationError) -> list[str]:
    """Names of required fields that were missing or wrong-typed."""
    fields: list[str] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        if loc and loc[0] in {"ticket_id", "complaint"}:
            fields.append(str(loc[0]))
    return sorted(set(fields))


def _error(status_code: int, message: str) -> JSONResponse:
    """Uniform, non-sensitive error envelope."""
    return JSONResponse(status_code=status_code, content={"error": message})

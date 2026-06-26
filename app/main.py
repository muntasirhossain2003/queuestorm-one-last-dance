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

import asyncio
import json
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .analyzer import analyze, detect_is_bangla
from .llm_analyzer import maybe_enhance
from .safety import contains_injection
from .schemas import AnalyzeTicketRequest, AnalyzeTicketResponse, HealthResponse

logger = logging.getLogger("queuestorm")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="QueueStorm Investigator",
    description="AI/API SupportOps copilot for digital finance complaints.",
    version=__version__,
)


async def _keep_alive() -> None:
    """Ping /health every 14 minutes so Render's free tier never sleeps."""
    await asyncio.sleep(60)  # wait for startup to finish
    self_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not self_url:
        return  # not on Render — no-op
    url = f"{self_url.rstrip('/')}/health"
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await client.get(url)
            except Exception:
                pass
            await asyncio.sleep(14 * 60)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_keep_alive())


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
        # Layer 0: for prompt-injection attempts, skip the LLM and serve the
        # deterministic, provably-safe rule-based response instead.
        if contains_injection(ticket.complaint):
            logger.info("Prompt injection detected in %s; serving rule-based response.", ticket.ticket_id)
        else:
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

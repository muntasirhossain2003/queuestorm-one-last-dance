# QueueStorm Investigator

**bKash presents SUST CSE Carnival 2026 · Codex Community Hackathon — Online Preliminary**
AI / API SupportOps copilot for digital finance complaints.

A FastAPI service that reads one customer complaint plus a short snippet of the
customer's recent transactions, decides **what actually happened**, classifies
and routes the case, and drafts a **safe** reply for a support agent. It is an
*investigator*, not a classifier: when the complaint and the transaction data
disagree, the service says so instead of guessing.

---

## Live endpoint

```text
https://queuestorm-one-last-dance.onrender.com
```

```bash
curl https://queuestorm-one-last-dance.onrender.com/health
# → {"status":"ok"}
```

---

## Tech stack

- **Python 3.12**
- **FastAPI** + **Uvicorn** (ASGI, async)
- **Pydantic v2** for schema validation
- **Groq** (llama-3.3-70b-versatile) + **Google Gemini** (gemini-2.0-flash) — optional LLM layer
- **No GPU, no baked-in models** — default engine is fully deterministic

---

## Quick start (run locally)

```bash
# 1. Create virtualenv and install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Run
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. Health check
curl http://127.0.0.1:8000/health
# → {"status":"ok"}

# 4. Analyse a ticket
curl -X POST http://127.0.0.1:8000/analyze-ticket \
  -H 'Content-Type: application/json' \
  -d '{
        "ticket_id": "TKT-001",
        "complaint": "I sent 5000 taka to a wrong number around 2pm today.",
        "language": "en",
        "transaction_history": [
          {"transaction_id":"TXN-9101","timestamp":"2026-04-14T14:08:22Z",
           "type":"transfer","amount":5000,"counterparty":"+8801719876543",
           "status":"completed"}
        ]
      }'
```

Interactive API docs: `http://127.0.0.1:8000/docs`

### Run with Docker

```bash
docker build -t queuestorm-investigator .
docker run -p 8000:8000 queuestorm-investigator
```

---

## API contract

### `GET /health`

Returns `{"status":"ok"}` (HTTP 200).

### `POST /analyze-ticket`

**Request** (only `ticket_id` and `complaint` are required):

```json
{
  "ticket_id": "TKT-001",
  "complaint": "...",
  "language": "en",
  "channel": "in_app_chat",
  "user_type": "customer",
  "transaction_history": [
    {"transaction_id":"TXN-9101","timestamp":"2026-04-14T14:08:22Z",
     "type":"transfer","amount":5000,"counterparty":"+8801719876543","status":"completed"}
  ]
}
```

**Response** (HTTP 200) — all fields always present:

```json
{
  "ticket_id": "TKT-001",
  "relevant_transaction_id": "TXN-9101",
  "evidence_verdict": "consistent",
  "case_type": "wrong_transfer",
  "severity": "high",
  "department": "dispute_resolution",
  "agent_summary": "...",
  "recommended_next_action": "...",
  "customer_reply": "...",
  "human_review_required": true,
  "confidence": 0.9,
  "reason_codes": ["wrong_transfer", "transaction_match", "dispute_initiated"]
}
```

### HTTP status codes

| Code  | When                                                               |
| ----- | ------------------------------------------------------------------ |
| `200` | Successful analysis                                                |
| `400` | Malformed JSON, empty body, missing `ticket_id` or `complaint`     |
| `422` | Schema valid but `complaint` is blank/whitespace                   |
| `500` | Internal error — non-sensitive message only, never a stack trace   |

---

## AI / reasoning approach

The core is a **deterministic rule-based investigator** in [`app/analyzer.py`](app/analyzer.py).

### Investigation pipeline (per ticket)

1. **Language detection** — honours the `language` hint; falls back to Bangla-script sniffing. Reply language matches the complaint.
2. **Classification** — bilingual keyword lexicons for all 8 `case_type` values; complaint text is the primary signal, transaction history is the fallback.
3. **Evidence matching** — finds the transaction the complaint refers to by amount / type / recency, then issues a verdict:
   - `consistent` — data supports the complaint
   - `inconsistent` — data contradicts it
   - `insufficient_data` — vague complaint, no match, or multiple plausible matches
4. **Routing & severity** — maps `case_type` → `department` + base severity; downgrades disputes when evidence is inconsistent.
5. **Escalation** — `human_review_required` true for fraud, confirmed disputes, duplicates, agent issues, or any inconsistent verdict.
6. **Composition** — builds `agent_summary`, `recommended_next_action`, and a templated `customer_reply`.
7. **Safety pass** — every reply runs through [`app/safety.py`](app/safety.py) before leaving the service.

### Hybrid LLM layer (optional)

When confidence ≤ 0.80 or `case_type = other`, the rule engine is uncertain and a **full hybrid investigation** is sent to the LLM. When confident, the LLM only rewrites the three text fields. Both paths re-apply the full safety pipeline.

| Provider                | Model                   | Free tier                    | Cascade order |
| ----------------------- | ----------------------- | ---------------------------- | ------------- |
| **Groq**                | llama-3.3-70b-versatile | 14 400 req/day · ~1-2 s      | 1st           |
| **Google Gemini**       | gemini-2.0-flash        | 15 RPM per key · ~2-4 s      | 2nd           |
| **Rule-based fallback** | —                       | unlimited · <1 ms            | always        |

Both `GROQ_API_KEY` and `GEMINI_API_KEY` accept **comma-separated key lists** — when one key hits its rate limit the next is tried automatically before falling back to the next provider.

### Why rule-based first?

The problem statement is explicit that *"a simple, reliable, safe API will score higher than a complex but unreliable one."* The rule engine is:

- **Deterministic & safe** — cannot be jailbroken; safety properties are provable
- **Sub-millisecond** — comfortably inside the 30 s budget
- **Schema-correct** — enums and routing always match the spec exactly

---

## Safety logic (Section 8) — defense in depth

[`app/safety.py`](app/safety.py) is a **6-layer pipeline with a guaranteed-safe fallback**. Applied to every harness-checked field on every response.

| Layer | What it does |
| --- | --- |
| **0 · Injection detection** | Flags prompt-injection in complaint text; skips LLM entirely for injected inputs |
| **1 · Secret redaction** | Strips leaked API keys, bearer tokens, JWTs, stack traces |
| **2 · Promise rewrite** | Rewrites unauthorised refund/reversal/account-unblock commitments (EN + BN, active and passive voice) |
| **3 · Sentence removal** | Drops sentences soliciting credentials (PIN/OTP/password/CVV) or redirecting to third parties. Negation-aware |
| **4 · Reminder** | Ensures the credential-safety reminder is present in the customer's language |
| **5 · Hard gate + fallback** | Re-audits result; if any violation survives, discards the candidate and returns a vetted safe template |

**Verified:** [`scripts/test_safety.py`](scripts/test_safety.py) feeds 26 hostile reply strings (including passive-voice refund assertions and Bangla variants), 3 unsafe next-actions, and 10 injection probes through the guards. All 41 tests pass.

```bash
python scripts/test_safety.py              # → ALL SAFETY TESTS PASSED  (41 tests)
python scripts/run_tests.py http://localhost:8000  # → 150 cases with timing
```

---

## Free LLM providers

| Provider | Free tier | Speed | How to activate |
| --- | --- | --- | --- |
| **Groq** *(recommended)* | 14 400 req/day · llama-3.3-70b | ~1-2 s | `ANALYZER_MODE=groq` + `GROQ_API_KEY=gsk_...` |
| **Google Gemini** | 15 RPM · gemini-2.0-flash | ~2-4 s | `ANALYZER_MODE=gemini` + `GEMINI_API_KEY=...` |

---

## Deployment (Render)

This repo includes [`render.yaml`](render.yaml). Deploy in 3 clicks:

1. Go to **render.com** → **New +** → **Web Service**
2. Connect GitHub repo — Render auto-detects `render.yaml`
3. Add `GROQ_API_KEY` and `GEMINI_API_KEY` in the Environment tab → **Deploy**

The service includes a **keep-alive loop** that pings `/health` every 14 minutes using `RENDER_EXTERNAL_URL` (set automatically by Render) so the free-tier container never sleeps during judging.

---

## MODELS section

| Model | Where | Why |
| --- | --- | --- |
| **None (deterministic rule-based)** | In-process, CPU-only | Default and authoritative. Reliable, safe, free, sub-millisecond. |
| *Optional:* **llama-3.3-70b-versatile** | Groq API (free) | LLM enrichment for text fields and hybrid reasoning on uncertain cases. |
| *Optional:* **gemini-2.0-flash** | Google Gemini API (free) | Fallback LLM pool when all Groq keys are exhausted. |

No model weights are baked into the Docker image.

---

## Project structure

```text
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI endpoints + HTTP status codes + keep-alive + Layer 0
│   ├── schemas.py       # Pydantic request/response models + Section 7 enums
│   ├── analyzer.py      # Rule-based investigation engine
│   ├── llm_analyzer.py  # Groq + Gemini hybrid enrichment, multi-key pool rotation
│   └── safety.py        # 6-layer safety pipeline (multilingual, hard-gate fallback)
├── scripts/
│   ├── validate_samples.py     # runs all 10 public cases, writes sample_output.json
│   ├── generate_hard_cases.py  # builds the 100 HARD cases in hard_test_cases.json
│   ├── run_tests.py            # fires all 150 cases at a live endpoint with timing
│   └── test_safety.py          # 41 adversarial safety tests — all must pass
├── hard_test_cases.json        # 150 cases: 100 hard + 50 unknown/novel edge cases
├── sample_output.json          # required deliverable (generated from SAMPLE-01)
├── render.yaml                 # Render deployment config (no secrets — sync: false)
├── Dockerfile
├── .env.example
└── README.md
```

---

## Assumptions

- Complaint text is the primary classification signal; transaction history is used only as evidence and fallback.
- `language: "mixed"` or absent defaults to Bangla-script detection.
- For duplicate payments and wrong transfers, the most recent matching transaction is treated as the disputed one.
- Amounts are matched on exact value; both English and Bangla digit formats are parsed.

## Known limitations

- Keyword-driven classification can miss highly paraphrased complaints that use none of the lexicon terms in either language — such cases fall back to `other` / `insufficient_data` (safe by design, never a wrong confident answer). The hybrid LLM path handles these when triggered.
- Evidence matching is amount/type/recency based, not semantic.
- Render free tier: kept alive via self-ping loop; cold starts after a full outage may still take 20-30 s.

---

## Deliverables checklist (Section 11)

- [x] `POST /analyze-ticket` and `GET /health` endpoints
- [x] README with setup, run command, tech stack, AI approach, safety logic, MODELS section, assumptions, limitations
- [x] `requirements.txt`
- [x] `sample_output.json` (generated from public SAMPLE-01)
- [x] Dockerfile + `render.yaml` + `.env.example`
- [x] 10/10 public sample cases pass on authoritative fields + safety

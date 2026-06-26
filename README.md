# QueueStorm Investigator

**bKash presents SUST CSE Carnival 2026 · Codex Community Hackathon — Online Preliminary**
AI / API SupportOps copilot for digital finance complaints.

A FastAPI service that reads one customer complaint plus a short snippet of the
customer's recent transactions, decides **what actually happened**, classifies
and routes the case, and drafts a **safe** reply for a support agent. It is an
*investigator*, not a classifier: when the complaint and the transaction data
disagree, the service says so instead of guessing (Section 3 of the problem
statement).

---

## Free deployment & LLM options

The default engine is **fully rule-based** — no API key, no network, no cost.
If you want richer natural-language phrasing, the table below shows every free
path ranked by speed (the judge harness enforces a **30 s per-request timeout**).

### Free LLM providers

| Provider | Free tier | Speed | How to activate |
| -------- | --------- | ----- | --------------- |
| **Groq** *(recommended)* | 14 400 req/day · Llama-3.3-70b | ~1-2 s | `ANALYZER_MODE=groq` + `GROQ_API_KEY=…` — key at [console.groq.com](https://console.groq.com) |
| **Google Gemini** | 15 RPM · Gemini 1.5-Flash | ~2-4 s | `ANALYZER_MODE=gemini` + `GEMINI_API_KEY=…` — key at [aistudio.google.com](https://aistudio.google.com) |
| **Hugging Face Inference API** | free, rate-limited | 5-20 s (cold) | not wired in yet; too slow for the 30 s limit |

> The LLM layer **only improves** `agent_summary`, `recommended_next_action`,
> and `customer_reply`. Every schema-critical field — `case_type`,
> `evidence_verdict`, `department`, `severity`, `relevant_transaction_id`,
> `human_review_required` — always comes from the deterministic rule engine.
> If the LLM call times out or errors, the rule-based text is kept unchanged.

### Free hosting options

| Platform | Free tier | Cold start risk | Docker |
| -------- | --------- | --------------- | ------ |
| **Koyeb** *(recommended)* | 512 MB RAM · always on | none | ✅ |
| **Render.com** | 750 h/month · sleeps | ~30 s (judge might timeout!) | ✅ |
| **Fly.io** | 3 shared VMs · always on | minimal | ✅ |
| **Railway** | $5 credit on signup | none | ✅ |
| **Hugging Face Spaces** | free · may sleep | ~15 s | ✅ |

**Important:** Render's free tier sleeps after 15 min of inactivity. The judge
calls `GET /health` first, but if that call itself wakes the container and
takes 30+ s, subsequent test cases may timeout. Koyeb or Fly.io are safer
because they never sleep.

### Deploy to Koyeb (step-by-step)

```bash
# 1. Push code to GitHub (see git setup below)
# 2. Go to app.koyeb.com → Create Service → GitHub
# 3. Set build method: Dockerfile
# 4. Add environment variables:
#      PORT=8000
#      ANALYZER_MODE=groq          # optional
#      GROQ_API_KEY=gsk_...        # optional
# 5. Expose port 8000 → Deploy
# Live URL appears in ~2 min
```

### Classification strategies (how the engine works)

There are three strategies you can choose or combine:

**1. Rule-based (current default — already 10/10 on sample cases)**
Bilingual keyword lexicons decide `case_type` from the complaint text first;
transaction history is used only as a fallback. Evidence matching then picks
`relevant_transaction_id` by amount + type + recency, and issues a verdict of
`consistent` / `inconsistent` / `insufficient_data`. No API key, zero latency.

**2. LLM-only classification**
Send the raw complaint + history to a free LLM with a strict JSON-mode prompt
and parse its output directly. Pros: better handling of paraphrased complaints.
Cons: non-deterministic, may hallucinate enum values, costs tokens, subject to
rate limits. **Not recommended** as the sole strategy because schema violations
are penalised point for point.

**3. Rule + LLM hybrid (implemented, recommended if you get a free key)**
Rule engine owns all schema fields → LLM rewrites only the three text fields
(`agent_summary`, `recommended_next_action`, `customer_reply`). This gets the
best of both worlds: guaranteed schema compliance, human-quality prose, and
a safe fallback if the LLM is unavailable. Set `ANALYZER_MODE=groq` to enable.

---

## What I built / what is done

| Area | Status | Notes |
|------|--------|-------|
| `GET /health` | ✅ | Returns `{"status":"ok"}`; used by the judge harness for readiness. |
| `POST /analyze-ticket` | ✅ | Full structured analysis per the Section 6 schema. |
| Request/response schema (Pydantic v2) | ✅ | Exact enum strings from Section 7; output enums serialised verbatim. |
| Evidence reasoning (the "investigator twist") | ✅ | Picks `relevant_transaction_id` and an `evidence_verdict` of `consistent` / `inconsistent` / `insufficient_data`. |
| Case classification (Section 7.1) | ✅ | All 8 `case_type` values, complaint-text-first with history fallback. |
| Routing + severity (Section 7.2) | ✅ | `department` and `severity` derived from case type + evidence. |
| Escalation logic | ✅ | `human_review_required` true for disputes, fraud, duplicates, inconsistent evidence. |
| Safety guardrails (Section 8) | ✅ | Never asks for PIN/OTP, never promises a refund, never points to third parties; prompt-injection-proof. |
| Bilingual replies (English + Bangla) | ✅ | Reply language follows the complaint language. |
| Correct HTTP codes (Section 4.1) | ✅ | 200 / 400 / 422 / 500, never crashes on malformed input. |
| Sample validation harness | ✅ | `scripts/validate_samples.py` — **10/10** public cases pass. |
| `sample_output.json` deliverable | ✅ | Generated from public SAMPLE-01. |
| Dockerfile / requirements / `.env.example` | ✅ | CPU-only, <100 MB image, no baked-in models. |

**Result:** `10/10` public sample cases match on the authoritative fields
(`relevant_transaction_id`, `evidence_verdict`, `case_type`, `department`),
match on `severity`, and pass the safety checks.

---

## Tech stack

- **Python 3.12**
- **FastAPI** + **Uvicorn** (ASGI server)
- **Pydantic v2** for schema validation
- **No GPU, no external model, no network calls required** — the default engine
  is a deterministic rule-based investigator.

---

## Quick start (run locally)

```bash
# 1. Create a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run the service
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. Check health
curl http://127.0.0.1:8000/health
# -> {"status":"ok"}

# 4. Analyze a ticket
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

Interactive API docs are served at `http://127.0.0.1:8000/docs`.

### Run with Docker

```bash
docker build -t queuestorm-investigator .
docker run -p 8000:8000 queuestorm-investigator
# service is now on http://127.0.0.1:8000
```

### Validate against the public sample pack

```bash
source .venv/bin/activate
python scripts/validate_samples.py
# -> 10/10 cases match on authoritative fields + safety
```

---

## API contract

### `GET /health`
Returns `{"status":"ok"}` (HTTP 200).

### `POST /analyze-ticket`
**Request** (required: `ticket_id`, `complaint`):

```json
{
  "ticket_id": "TKT-001",
  "complaint": "...",
  "language": "en",
  "channel": "in_app_chat",
  "user_type": "customer",
  "campaign_context": "boishakh_bonanza_day_1",
  "transaction_history": [
    {"transaction_id":"TXN-9101","timestamp":"2026-04-14T14:08:22Z",
     "type":"transfer","amount":5000,"counterparty":"+8801719876543","status":"completed"}
  ],
  "metadata": {}
}
```

**Response** (HTTP 200) — every field below is always present:

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

### HTTP status codes (Section 4.1)

| Code | When |
|------|------|
| `200` | Successful analysis. |
| `400` | Malformed JSON, empty body, or missing required field (`ticket_id` / `complaint`). |
| `422` | Schema valid but semantically unusable (e.g. empty/whitespace complaint). |
| `500` | Internal error — returns a non-sensitive message, never a stack trace. |

The service **never crashes** on bad input: every path returns a clean JSON
error envelope.

---

## AI / reasoning approach

The core is a **deterministic rule-based investigator** in
[`app/analyzer.py`](app/analyzer.py). The pipeline per ticket:

1. **Language detection** — honours the `language` hint, otherwise sniffs the
   complaint for Bangla script. The `customer_reply` is returned in the
   customer's language (English or Bangla).
2. **Classification** — complaint text is the primary signal (bilingual keyword
   lexicons for each `case_type`); transaction history is only a *fallback* so a
   stray `failed`/`pending` entry can never override what the customer actually
   said (this is exactly the trap in SAMPLE-08).
3. **Evidence matching** — finds the transaction the complaint refers to by
   amount / type / recency, then issues a verdict:
   - `consistent` — the data supports the complaint (e.g. a matching transfer).
   - `inconsistent` — the data contradicts it (e.g. a "wrong transfer" to a
     counterparty the customer has repeatedly paid before — SAMPLE-02).
   - `insufficient_data` — vague complaint, no match, or **multiple plausible
     matches** (SAMPLE-06, SAMPLE-08). The service refuses to guess.
4. **Routing & severity** — maps `case_type` → `department` (Section 7.2) and a
   base severity, downgrading disputes when the evidence is inconsistent or
   ambiguous.
5. **Escalation** — `human_review_required` is true for fraud/phishing,
   confirmed disputes, duplicate payments, agent cash-in issues, and any
   inconsistent verdict; false for cases resolved by simply asking the customer
   for more detail.
6. **Composition** — builds `agent_summary`, `recommended_next_action`, and a
   templated `customer_reply`.
7. **Safety pass** — every reply is run through
   [`app/safety.py`](app/safety.py) before it leaves the service.

### Why rule-based?

The problem statement is explicit that *"a simple, reliable, safe API will score
higher than a complex but unreliable one"* and that *"an LLM is not required to
score well."* A rule-based engine is:

- **Deterministic & safe** — it cannot be jailbroken into asking for an OTP or
  promising a refund; the safety properties are provable, not probabilistic.
- **Fast & cheap** — sub-millisecond per request, no API cost, comfortably
  inside the 30-second budget.
- **Reproducible** — judges get identical output every run, no key required.

An **optional** LLM phrasing layer is scaffolded behind environment variables
(see `.env.example`) but is **disabled by default**; the rule-based path remains
authoritative for all schema-critical fields, and the safety pass is re-applied
regardless.

---

## MODELS

| Model | Where it runs | Why |
|-------|---------------|-----|
| **None (deterministic rule-based engine)** | In-process, CPU-only | Default and authoritative engine. Reliable, safe, free, and well within latency limits — no external model is required to produce correct, schema-compliant, safe output. |
| *Optional:* **Claude Haiku 4.5** (`claude-haiku-4-5`) | Anthropic API (only if `ANALYZER_MODE=llm`) | Scaffolded for optional natural-language polish of `agent_summary` / `customer_reply`. Chosen for low cost and low latency. **Off by default**; schema-critical fields and safety filters are never delegated to it. |

No model weights are baked into the Docker image (Section 9 guidance).

---

## Safety logic (Section 8) — defense in depth

The harness checks **both** `customer_reply` and `recommended_next_action`, and
because text may pass through an LLM, the LLM output is treated as untrusted.
[`app/safety.py`](app/safety.py) is therefore a **layered pipeline with a
guaranteed-safe fallback**, not a single regex pass. It is multilingual
(English + Bangla) and applied to every harness-checked field:

| Layer | What it does |
| ----- | ------------ |
| **0 · Injection detection** | `contains_injection()` flags prompt-injection complaints ("ignore all instructions", "admin mode", `SYSTEM:`, roleplay jailbreaks). [`main.py`](app/main.py) then **skips the LLM entirely** and serves the deterministic rule-based response. |
| **1 · Secret redaction** | Strips leaked API keys, bearer tokens, JWTs, and stack-trace fragments so no secret or internal detail ever reaches the response. |
| **2 · Promise rewrite** | Rewrites unauthorised refund/reversal/unblock commitments (EN + BN) — e.g. "we will refund you" / "আমরা টাকা ফেরত দেব" → *"any eligible amount will be returned through official channels"*. |
| **3 · Sentence removal** | Drops any sentence that solicits a credential (PIN/OTP/password/CVV/card number, EN + BN) or redirects to a third party. **Negation-aware**, so protective lines ("never share your OTP") survive. |
| **4 · Reminder** | Ensures the credential-safety reminder is present in the customer's language. |
| **5 · Hard gate + fallback** | The result is **re-audited**; if *any* violation survives, the candidate is discarded and a vetted safe template is returned. Output is therefore provably compliant — never unsafe. |

This means even a fully jailbroken LLM emitting "share your OTP and we'll refund
you, call that number" — in English **or** Bangla — cannot produce an unsafe
response: every such string is neutralised or replaced.

**Verification:** [`scripts/test_safety.py`](scripts/test_safety.py) feeds 16
hostile reply strings, 3 unsafe next-actions, and injection probes through the
guards and asserts each result passes `audit()` with zero violations — while
confirming protective lines are *not* over-stripped. All pass.

```bash
python scripts/test_safety.py   # → ALL SAFETY TESTS PASSED
```

---

## Project structure

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app + endpoints + HTTP status handling + Layer 0
│   ├── schemas.py       # Pydantic request/response models + Section 7 enums
│   ├── analyzer.py      # the investigation engine (classify → match → route → reply)
│   ├── llm_analyzer.py  # optional free-LLM enrichment (Groq / Gemini), safety re-applied
│   └── safety.py        # Section 8 layered guardrails (multilingual, hard-gate fallback)
├── scripts/
│   ├── validate_samples.py     # runs all 10 public cases, writes sample_output.json
│   ├── generate_hard_cases.py  # builds hard_test_cases.json (100 cases)
│   ├── run_hard_tests.py       # fires the 100 hard cases at a live endpoint
│   └── test_safety.py          # adversarial safety tests (must all pass)
├── hard_test_cases.json        # 100 hard/adversarial/multilingual test cases
├── sample_output.json          # required deliverable (generated from SAMPLE-01)
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .env.example
└── README.md
```

---

## Assumptions

- The 10 public cases are reference examples, not the test set; the engine is
  built for general robustness (bilingual input, empty history, ambiguous
  matches, malformed JSON), not for memorising the samples.
- `language: "mixed"` or an absent language hint defaults to replying in the
  language detected in the complaint text (Bangla script → Bangla reply).
- For duplicate payments and wrong transfers, the *most recent* matching
  transaction is treated as the relevant one (the likely duplicate / disputed
  send), matching the sample rationales.
- Amounts are matched on exact value (English and Bangla digits both parsed).

## Known limitations

- Keyword-driven classification can miss highly paraphrased complaints that use
  none of the lexicon terms in either language; such cases fall back to `other`
  / `insufficient_data` (safe by design, never a wrong confident answer).
- Evidence matching is amount/type/recency based, not semantic; a complaint that
  references a transaction only by a vague description may be marked
  `insufficient_data`.
- Timestamp reasoning uses ISO-8601 parsing; non-standard timestamps are ignored
  for ordering (input order is used as a fallback).

---

## Deliverables checklist (Section 11)

- [x] `POST /analyze-ticket` and `GET /health` endpoints
- [x] README with setup, run command, tech stack, AI approach, safety logic,
      MODELS section, assumptions, and limitations
- [x] Dependency file (`requirements.txt`)
- [x] Sample output from a public case (`sample_output.json`)
- [x] Runbook (the Quick start section above — copy/paste to bring the service
      up locally or via Docker)
- [x] `.env.example`

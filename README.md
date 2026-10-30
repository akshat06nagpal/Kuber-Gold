# Kuber Gold — chat → digital gold purchase → SIP (Task 2)

Python 3.12 · FastAPI · SQLite · integer money maths · optional LLM (any OpenAI-compatible endpoint).

## Run it (under 5 minutes)

```bash
docker compose up --build            # API on http://localhost:8000  (docs: /docs)
pip install httpx && python scripts/verify.py     # full flow against the running server
python scripts/trace_user.py <user_id>            # rebuild one user's journey from logs/app.jsonl
```
Run the tests without Docker: `pip install -r requirements.txt && python -m pytest -q`

The LLM is optional. Copy `.env.example` to `.env` and set `LLM_API_KEY` (Gemini / Groq / OpenRouter free tier)
to get model-written answers to advice questions; without it, vetted template answers are used.

## API
| | |
|---|---|
| `POST /chat` `{user_id, message}` (+ optional `Idempotency-Key`) | conversation; proposes purchases / SIPs, executes on "yes" |
| `POST /purchases` `{user_id, amount_inr}` (+ `Idempotency-Key`) | one-time purchase |
| `POST /sips` `{user_id, amount_inr, frequency, start_date?, day_of_month?}` | create SIP |
| `POST /sips/{id}/pause\|resume\|cancel` `{user_id}` | lifecycle |
| `GET /users/{id}/ledger`, `/portfolio`, `/sips` | read |
| `POST /admin/run-due-sips` `{as_of?}` | run the scheduler now (also runs every 60s) |
| `POST /admin/price-outage` `{on}` | demo switch: simulate the price source going down |

## How the non-negotiables are met
- **Exact money**: rupees are integer paise, gold is integer 1/10,000 g. `units = paise * 10000 // price_paise` (floor). No floats anywhere in the purchase path. API returns `"333.33"` / `"0.0266"` strings.
- **Nothing charges twice**: `UNIQUE(user_id, idempotency_key)` and `UNIQUE(sip_id, sip_idx)` in the DB; chat confirmations use key `pending:<id>`; check-then-insert runs inside `BEGIN IMMEDIATE`.
- **Traceable**: every log line is JSON with `request_id` + `user_id`; ledger rows store the `request_id`.
- **Dependency failure**: see `app/pricing.py` docstring. Refuse to charge on an unverifiable price; SIP instalments stay due and retry.
- **Tests that catch breakage**: `tests/test_money.py` fails if `//` is replaced by `round()` (checked by mutation).

## 5-minute recording checklist
1. Start with `docker compose up --build`.
2. Open `/docs` and ask an advice question.
3. Propose a one-time purchase and confirm it.
4. Create a daily SIP and confirm it.
5. Run `/admin/run-due-sips` on three consecutive dates and show all three ledger rows.
6. Use `python scripts/trace_user.py <user_id>` to show the user's JSONL journey.
7. Run `python -m pytest -q` and `python scripts/verify.py`.

## Design notes
- Architecture decisions and trade-offs: [DECISIONS.md](DECISIONS.md)

## What's unfinished
- No auth: `user_id` is trusted from the request. Would add JWT/API-key middleware.
- No real payment rail: "charge" = ledger entry. Would add a payments port with an idempotent provider call.
- SIP catch-up after downtime executes every missed instalment at once; would cap/ask the user.
- Intent rules are regex-first; Hinglish/phrasing coverage is limited, LLM classification is used only for "unknown".
- Single SQLite file + single worker; Postgres + a real job queue for scale.
- No live gold-price API wired (mock constant, `PRICE_MODE=http` supports any JSON endpoint with `price_per_gram_inr`).

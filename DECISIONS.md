# Decision log

These notes describe the choices visible in this implementation. Adjust them if they do not match the decisions you made while building it.

1. **Intent detection is rules-first.** The parser handles money-moving intents and safety checks deterministically; an optional LLM only classifies otherwise unknown messages. A fully LLM-driven router was rejected because a model should not decide amounts or trigger a purchase.

2. **Every purchase needs an explicit confirmation in chat.** A proposed purchase is stored as pending, and only a later confirmation executes it. Immediate execution from a single conversational message was rejected to reduce accidental purchases.

3. **Money and gold quantities use integer units.** Rupees are stored as paise and gold as 1/10,000 gram units; conversion floors the result. Binary floating point and rounding to the nearest gold unit were rejected because they can credit more gold than the paid amount supports.

4. **SQLite is the first database.** It keeps local setup simple and provides transactional writes and uniqueness constraints for this single-worker assignment. A network database was rejected for the prototype because it adds setup without helping the required local demonstration.

5. **Idempotency is enforced in storage.** Purchases use a unique user/key constraint, and SIP instalments use a unique SIP/index constraint inside write transactions. In-memory duplicate checks were rejected because concurrent requests or restarts could bypass them.

6. **Identical direct SIP requests are treated as duplicates.** A natural key based on user, amount, frequency, and schedule returns an existing active/paused SIP. Creating another matching active plan was rejected because a retried request could create two recurring debits.

7. **A failed price lookup may use only a fresh cached quote.** Informational answers can label a recent price stale; a purchase refuses when the quote exceeds the execution age limit. Guessing a price or charging at an arbitrarily old quote was rejected.

8. **Missed SIP dates are not back-filled when a plan is first created or resumed.** The next future date is selected for a past start/resume. Charging a user for every missed interval at once was rejected as a surprising catch-up debit.

9. **Structured JSONL logs carry the user and request IDs.** This makes a single user's messages, decisions, and ledger actions traceable. Plain unstructured console messages were rejected because they are harder to reconstruct and correlate.

10. **The LLM is optional and advisory.** Without a key, vetted templates keep the service usable; with a key, the model can phrase advice or classify an unknown query. Making the model a hard runtime dependency was rejected because provider outages should not stop core transactions.

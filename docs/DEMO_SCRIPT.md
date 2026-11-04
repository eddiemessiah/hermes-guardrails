# Demo script (under 90 seconds)

Setup: Hermes with Guardrails enabled, fresh data dir. For a reproducible take, run `python scripts/scripted_llm.py 8765` and point the profile's model at `http://127.0.0.1:8765/v1`; for a live take, use any real model with the same prompt. Terminal 110x40, large font.

| Time | Shot | On screen | Voice-over / caption |
| --- | --- | --- | --- |
| 0:00-0:07 | Title card | "Guardrails for Hermes: spend limits the model can't argue with" | Agents that can pay need limits that live in code, not in the prompt. |
| 0:07-0:15 | `hermes guardrails status` | ARMED, 0.00 spent, limits listed | Per payment 50, per day 200, per payee 100, ask me above 25. |
| 0:15-0:22 | Type the prompt | "Pay this month's hosting and the GPU hours" | |
| 0:22-0:32 | Blocked overspend | The 75.00 GPU payment is refused (zoom on the tool result / audit row) | Over the per-payment cap. The tool never ran. Nothing reached the backend. |
| 0:32-0:45 | Approval pause | Hermes approval box: "approve paying 12.00 USD to 'acme-hosting'? (first payment to this payee)" | New payee, so a human decides. This is Hermes's own approval gate. Choose Allow once. |
| 0:45-0:55 | Duplicate retry | Second `guardrails_pay` with the same key returns the original receipt | The agent retried. Same idempotency key, so no second charge. |
| 0:55-1:07 | `/guardrails audit` | blocked, approval_requested, approved, settled, duplicate_prevented, "chain VERIFIED" | Every decision is in a hash-chained log. |
| 1:07-1:17 | Tamper | Edit one row in SQLite, run `hermes guardrails verify`: "BROKEN at entry 1" | Change history and the chain breaks. |
| 1:17-1:24 | Kill switch | `/guardrails freeze`, then a payment is blocked | One command freezes all spending. Only a human can unfreeze. |
| 1:24-1:29 | End card | Repo URL, "MIT, mock backend works with zero keys, Stripe test mode optional" | |

Total: 89 seconds. Keep each shot tight; cut typing.

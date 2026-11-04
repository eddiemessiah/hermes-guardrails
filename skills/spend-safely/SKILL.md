---
name: spend-safely
description: How to pay for things through Hermes Guardrails without fighting the policy.
---
# Spend safely with Guardrails

- Pay only with `guardrails_pay`. Give a stable `idempotency_key` per purchase (for example the order id) and reuse it on every retry.
- Check `guardrails_status` before a purchase that might be large, and tell the user what is left.
- If a payment is blocked, explain the reason to the user. Do not split it into smaller payments, change the payee name, or try another tool to get around a limit.
- If a payment pauses for approval, wait for the user's answer.
- Only a human can turn the kill switch off (`/guardrails unfreeze`).

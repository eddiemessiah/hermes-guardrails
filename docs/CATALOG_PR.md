# Catalog PR (prepared, not opened)

Target: NousResearch/hermes-agent, branch `add-guardrails-plugin` from a fork, one file: `plugin-catalog/guardrails.yaml` (ready in the local clone at /workspace/hermes-agent, pinned to `d9183978004cfb89f1d71153e6a929f51a5f3bea`).
Before opening: make the repo public, re-pin `sha` and screenshot URLs if any commit lands after this one, and run `hermes plugins validate <checkout> --install-deps` at the pin.

## Title
plugin-catalog: add guardrails (hard spend limits for payment tools)

## Body
Adds `guardrails`, a community plugin from https://github.com/eddiemessiah/hermes-guardrails (I own the repo), pinned at `d9183978004cfb89f1d71153e6a929f51a5f3bea`.

**What it does.** Enforces spend policy in code before any payment tool runs: per-payment, daily and per-payee caps; Hermes approval for payments over a threshold or to a first-time payee; idempotency keys so retries never double-charge (parallel retries are serialised to one charge); a kill switch only a human can lift; and a SHA-256 hash-chained audit log with `hermes guardrails verify`. It ships its own `guardrails_pay` tool and can put other plugins' spending tools behind the same policy via `guarded_tools`. Default backend is a zero-key mock; an optional Stripe test-mode backend refuses live keys.

**Hermes surfaces used.** `pre_tool_call` (block / approve directives; approval goes through `request_tool_approval`, scoped per payment with `rule_key`, so "always" never whitelists future spends, and unattended runs fail closed), `post_tool_call`, `ctx.register_tool` (3 tools), `ctx.register_command` (`/guardrails`), `ctx.register_cli_command` (`hermes guardrails`), `ctx.register_skill`, `plugin_data_dir`, `config_schema` with one `secret` field read via `agent.secret_scope.get_secret`. No core overrides, no middleware.

**Validation.** `hermes plugins validate` passes (all checks, security scan safe). 46 unit tests pass. Verified end to end in a real Hermes CLI session (Hermes ed1afb0): overspend blocked, approval prompt shown and answered, duplicate retry returned the original receipt, audit chain verified. Screenshots are in the entry.

**Disclosure (rule 13).**
- The `pre_tool_call` hook sees every tool call but acts only on `guardrails_pay` and tools listed in `guarded_tools`.
- Writes payments and audit entries to `$HERMES_HOME/plugin-data/guardrails/guardrails.db`; no reads outside its own data.
- Network: none with the default `mock` backend. With `backend: stripe_test`, payment requests go to `api.stripe.com` with `GUARDRAILS_STRIPE_TEST_KEY` (test keys only).
- No shell commands, background processes, telemetry, self-updating code, binaries, or Python dependencies.

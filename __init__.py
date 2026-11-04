"""Hermes Guardrails: hard spend limits enforced in code before any payment tool runs."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .core import policy as P
from .core.backends import BackendError, MockBackend, StripeTestBackend
from .core.guard import PAY_TOOL, Guard
from .core.ledger import Ledger

logger = logging.getLogger(__name__)
PLUGIN = "guardrails"
_CONFIG_KEYS = tuple(P.DEFAULTS) + ("guarded_tools", "backend")

PAY_SCHEMA = {
    "name": PAY_TOOL,
    "description": (
        "Make a payment through Hermes Guardrails. Every call is checked in code against hard limits "
        "(per payment, per day, per payee), may pause for human approval, and is recorded in a "
        "tamper-evident audit log. Retrying with the same idempotency_key (or the same payee and amount "
        "on the same day) never charges twice. If a call is blocked, do not try to split or rephrase it."),
    "parameters": {
        "type": "object",
        "properties": {
            "payee": {"type": "string", "description": "Who is paid, e.g. 'acme-hosting'"},
            "amount": {"type": "string", "description": "Decimal amount in major units, e.g. '12.50'"},
            "currency": {"type": "string", "description": "3-letter currency code; defaults to the configured one"},
            "memo": {"type": "string", "description": "What the payment is for"},
            "idempotency_key": {"type": "string", "description": "Stable id for this purchase; reuse it on retries"},
        },
        "required": ["payee", "amount"],
    },
}
STATUS_SCHEMA = {
    "name": "guardrails_status",
    "description": "Read-only: today's spend, what is left under each limit, whether spending is frozen, and audit-chain integrity.",
    "parameters": {"type": "object", "properties": {}},
}
AUDIT_SCHEMA = {
    "name": "guardrails_audit",
    "description": "Read-only: the most recent Guardrails audit entries and a verification of the hash chain.",
    "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Entries to return (1-100)"}}},
}

_guard: Guard | None = None


def _j(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _render_status(s: dict) -> str:
    lines = [
        "GUARDRAILS " + ("FROZEN  (kill switch on)" if s["frozen"] else "ARMED"),
        f"  spent today ({s['day_utc']} UTC)  {s['spent_today']}    left  {s['left_today']}",
        "  limits  per payment {per_payment} | daily {daily} | per payee {per_payee_daily} | approval over {approval_over}".format(**s["limits"]),
        f"  backend {s['backend']}   guarding {', '.join(s['guarded_tools'])}",
        "  audit chain " + (f"OK, {s['audit']['entries']} entries, head {s['audit'].get('head', '')}" if s["audit"]["ok"]
                            else f"BROKEN at entry {s['audit']['broken_at']}"),
    ]
    for p, a in s["by_payee"].items():
        lines.append(f"    {p:<28} {a}")
    return "\n".join(lines)


def _render_audit(entries: list, v: dict) -> str:
    out = [f"{'#':>4}  {'time (UTC)':<20} {'event':<20} detail"]
    for e in entries:
        d = e["data"]
        detail = d.get("reason") or (f"{d.get('payee')} {P.fmt(d['amount'], d.get('currency', 'usd'))}" if "amount" in d else d.get("key") or d.get("actor") or "")
        out.append(f"{e['seq']:>4}  {e['ts'][:19]:<20} {e['event']:<20} {str(detail)[:90]}")
    out.append("chain " + (f"VERIFIED ({v['entries']} entries)" if v["ok"] else f"BROKEN at entry {v['broken_at']}"))
    return "\n".join(out)


def _command(raw: str) -> str:
    g = _guard
    if g is None:
        return "Guardrails is not loaded."
    cmd = (raw or "status").strip().split()[0].lower()
    try:
        if cmd == "freeze":
            g.ledger.set_frozen(True, "slash_command")
            return "Kill switch ON. Every guarded payment is blocked until /guardrails unfreeze."
        if cmd == "unfreeze":
            g.ledger.set_frozen(False, "slash_command")
            return "Kill switch OFF. Payments are checked against policy again."
        if cmd == "audit":
            return _render_audit(g.ledger.entries(25), g.ledger.verify())
        if cmd == "verify":
            v = g.ledger.verify()
            return f"Audit chain OK: {v['entries']} entries, head {v['head']}" if v["ok"] else f"Audit chain BROKEN at entry {v['broken_at']}"
        if cmd == "status":
            return _render_status(g.status())
    except P.PolicyError as exc:
        return f"Guardrails config error: {exc}"
    return "Usage: /guardrails [status|freeze|unfreeze|audit|verify]"


def _cli_setup(sub):
    s = sub.add_subparsers(dest="guardrails_cmd")
    for name, h in (("status", "Spend today and limits"), ("freeze", "Turn the kill switch on"),
                    ("unfreeze", "Turn the kill switch off"), ("audit", "Recent audit entries"),
                    ("verify", "Verify the audit hash chain")):
        s.add_parser(name, help=h)
    sub.set_defaults(func=_cli_run)


def _cli_run(args):
    if _guard is None:
        _boot(None)
    print(_command(getattr(args, "guardrails_cmd", None) or "status"))


def _boot(ctx):
    global _guard
    from plugins.plugin_storage import plugin_data_dir

    def get_config() -> dict:
        if ctx is None:
            return {}
        return {k: ctx.get_config(k, default=None) for k in _CONFIG_KEYS}

    def get_backend():
        name = str((get_config().get("backend") or "mock")).lower()
        if name == "stripe_test":
            from agent.secret_scope import get_secret
            return StripeTestBackend(get_secret("GUARDRAILS_STRIPE_TEST_KEY") or "")
        return MockBackend()

    ledger = Ledger(Path(plugin_data_dir(PLUGIN)) / "guardrails.db")
    _guard = Guard(ledger, get_config, get_backend)
    return _guard


def _pre_tool_call(tool_name="", args=None, session_id="", **kwargs):
    try:
        return _guard.pre_tool_call(tool_name, args or {}, session_id) if _guard else None
    except Exception as exc:  # fail closed for guarded tools only
        logger.exception("guardrails pre_tool_call failed")
        if _guard and tool_name in _guard.guarded_tools():
            return {"action": "block", "message": f"Guardrails could not evaluate this payment ({type(exc).__name__}); blocked."}
        return None


def _post_tool_call(tool_name="", args=None, result=None, **kwargs):
    try:
        if _guard:
            _guard.post_tool_call(tool_name, args or {}, result)
    except Exception:
        logger.exception("guardrails post_tool_call failed")


def _tool_pay(args, **kwargs):
    try:
        return _j(_guard.pay(args or {}))
    except Exception as exc:
        return _j({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _tool_status(args, **kwargs):
    try:
        return _j(_guard.status())
    except (P.PolicyError, BackendError) as exc:
        return _j({"error": str(exc)})


def _tool_audit(args, **kwargs):
    try:
        limit = max(1, min(int((args or {}).get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    return _j({"entries": _guard.ledger.entries(limit), "verify": _guard.ledger.verify()})


def register(ctx):
    _boot(ctx)
    ctx.register_tool(name=PAY_TOOL, toolset=PLUGIN, schema=PAY_SCHEMA, handler=_tool_pay)
    ctx.register_tool(name="guardrails_status", toolset=PLUGIN, schema=STATUS_SCHEMA, handler=_tool_status)
    ctx.register_tool(name="guardrails_audit", toolset=PLUGIN, schema=AUDIT_SCHEMA, handler=_tool_audit)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_command("guardrails", _command, description="Spend guardrails: status, freeze, unfreeze, audit, verify",
                         args_hint="[status|freeze|unfreeze|audit|verify]")
    ctx.register_cli_command(name="guardrails", help="Hermes Guardrails spend controls", setup_fn=_cli_setup, handler_fn=_cli_run)
    skill = Path(__file__).parent / "skills" / "spend-safely" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("spend-safely", skill)

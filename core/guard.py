"""Glue between Hermes hooks/tools and the pure policy + ledger."""
from __future__ import annotations

import json
from typing import Callable

from . import policy as P
from .backends import BackendError
from .ledger import Ledger

PAY_TOOL = "guardrails_pay"


class Guard:
    def __init__(self, ledger: Ledger, get_config: Callable[[], dict], get_backend: Callable[[], object]):
        self.ledger = ledger
        self._get_config = get_config
        self._get_backend = get_backend

    # -- helpers ------------------------------------------------------------
    def config(self) -> P.Config:
        return P.Config.from_mapping(self._get_config())

    def guarded_tools(self) -> dict:
        tools = dict((self._get_config() or {}).get("guarded_tools") or {})
        tools[PAY_TOOL] = {"amount": "amount", "payee": "payee", "currency": "currency",
                           "idempotency": "idempotency_key", "memo": "memo"}
        return tools

    def build_request(self, tool: str, args: dict, cfg: P.Config) -> P.Request:
        spec = self.guarded_tools()[tool]
        args = args if isinstance(args, dict) else {}
        amount = P.to_minor(args.get(spec.get("amount", "amount")))
        payee = P.norm_payee(args.get(spec.get("payee", "payee")))
        currency = P.norm_currency(args.get(spec.get("currency", "currency")) or None, cfg.currency)
        explicit = args.get(spec.get("idempotency", "idempotency_key"))
        key = P.idempotency_key(tool, payee, amount, currency, self.ledger.day(), explicit)
        return P.Request(tool, payee, amount, currency, key, str(args.get(spec.get("memo", "memo")) or "")[:200])

    # -- pre_tool_call ------------------------------------------------------
    def pre_tool_call(self, tool_name: str, args: dict, session_id: str = "") -> dict | None:
        if tool_name not in self.guarded_tools():
            return None
        try:
            cfg = self.config()
            req = self.build_request(tool_name, args, cfg)
        except P.PolicyError as exc:
            self.ledger.audit("blocked", {"tool": tool_name, "code": "invalid", "reason": str(exc)})
            return {"action": "block", "message": f"Guardrails blocked {tool_name}: {exc}"}
        with self.ledger.tx() as db:
            view = self.ledger.view(db, req.payee, req.key)
            d = P.decide(req, cfg, view)
            base = {"tool": tool_name, "payee": req.payee, "amount": req.amount, "currency": req.currency,
                    "key": req.key, "session": session_id or ""}
            if d.action == "replay":
                self.ledger._append(db, "duplicate_prevented", base)
                if tool_name == PAY_TOOL:
                    return None  # handler returns the original receipt, no new charge
                return {"action": "block", "message": f"Guardrails: {d.message}"}
            if d.action == "block":
                self.ledger._append(db, "blocked", {**base, "code": d.code, "reason": d.message})
                return {"action": "block", "message": f"Guardrails blocked this payment. {d.message}"}
            approved_by = "pending" if d.action == "approve" else "policy"
            self.ledger.reserve(db, req.key, tool_name, req.payee, req.amount, req.currency, approved_by)
            self.ledger._append(db, "approval_requested" if d.action == "approve" else "allowed",
                                {**base, "reasons": list(d.reasons)})
        if d.action == "approve":
            # rule_key is per payment so an "always" answer never whitelists future spends.
            return {"action": "approve", "message": d.message, "rule_key": f"guardrails:{req.key}"}
        return None

    # -- guardrails_pay handler ---------------------------------------------
    def pay(self, args: dict) -> dict:
        try:
            cfg = self.config()
            req = self.build_request(PAY_TOOL, args, cfg)
        except P.PolicyError as exc:
            return {"ok": False, "blocked": True, "error": str(exc)}
        existing = self.ledger.payment(req.key)
        if existing and existing["status"] == "settled":
            return {"ok": True, "duplicate": True, "note": "Already paid; returning the original receipt. No new charge.",
                    "receipt": existing["result"]}
        claim = self.ledger.claim(req.key)
        if not claim or claim[0] != "claimed":
            if claim and claim[0] == "settled":
                return {"ok": True, "duplicate": True, "note": "Already paid; returning the original receipt. No new charge.",
                        "receipt": self.ledger.payment(req.key)["result"]}
            if claim and claim[0] == "charging":
                return {"ok": False, "blocked": True, "error": "This payment is already being charged. Not charging twice."}
            # Called without the pre_tool_call gate (should not happen inside Hermes): fail closed.
            return {"ok": False, "blocked": True, "error": "No policy reservation for this payment. Guardrails refuses to spend outside its gate."}
        try:
            receipt = self._get_backend().charge(key=req.key, payee=req.payee, amount=req.amount,
                                                 currency=req.currency, memo=req.memo)
        except BackendError as exc:
            self.ledger.fail(req.key, str(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # never raise into Hermes
            self.ledger.fail(req.key, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": "payment backend error"}
        self.ledger.settle(req.key, receipt)
        return {"ok": True, "receipt": receipt, "spent_today": P.fmt(self.ledger.totals()["total"], cfg.currency)}

    # -- post_tool_call for third-party guarded tools -----------------------
    def post_tool_call(self, tool_name: str, args: dict, result) -> None:
        if tool_name == PAY_TOOL or tool_name not in self.guarded_tools():
            return
        try:
            req = self.build_request(tool_name, args, self.config())
        except P.PolicyError:
            return
        row = self.ledger.payment(req.key)
        if not row or row["status"] != "reserved":
            return
        if row["approved_by"] == "pending":
            self.ledger.audit("approved", {"key": req.key, "by": "human"})
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        try:
            parsed = json.loads(text)
            failed = isinstance(parsed, dict) and (parsed.get("error") or parsed.get("ok") is False)
        except Exception:
            failed = False
        if failed:
            self.ledger.fail(req.key, "tool reported an error")
        else:
            self.ledger.settle(req.key, {"id": None, "tool": tool_name, "summary": text[:200]})

    # -- read-only views ----------------------------------------------------
    def status(self) -> dict:
        cfg = self.config()
        t = self.ledger.totals()
        c = cfg.currency
        return {
            "frozen": self.ledger.frozen(), "day_utc": t["day"],
            "spent_today": P.fmt(t["total"], c), "left_today": P.fmt(max(cfg.daily_cap - t["total"], 0), c),
            "by_payee": {p: P.fmt(a, c) for p, a in t["by_payee"].items()},
            "limits": {"per_payment": P.fmt(cfg.per_payment_cap, c), "daily": P.fmt(cfg.daily_cap, c),
                       "per_payee_daily": P.fmt(cfg.per_merchant_daily_cap, c),
                       "approval_over": P.fmt(cfg.approval_threshold, c), "approve_new_payees": cfg.approve_new_payees},
            "backend": getattr(self._get_backend(), "name", "?"),
            "guarded_tools": sorted(self.guarded_tools()),
            "audit": self.ledger.verify(),
        }

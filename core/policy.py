"""Spend policy: a pure function from (request, config, ledger view) to a decision.

Amounts are integer minor units (cents) everywhere. Parsing is strict and fails closed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
_CURRENCY_RE = re.compile(r"^[a-z]{3}$")

DEFAULTS = {
    "currency": "usd",
    "per_payment_cap": "50.00",
    "daily_cap": "200.00",
    "per_merchant_daily_cap": "100.00",
    "approval_threshold": "25.00",
    "approve_new_payees": True,
    "blocked_payees": [],
    "allowed_payees": [],
}


class PolicyError(ValueError):
    """Raised for malformed requests or config. Callers turn it into a block."""


def to_minor(value) -> int:
    if isinstance(value, bool):
        raise PolicyError("amount must be a number")
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        value = repr(value)
    s = str(value).strip()
    if not _AMOUNT_RE.match(s):
        raise PolicyError(f"amount {value!r} is not a positive decimal with at most 2 places")
    try:
        minor = int(Decimal(s) * 100)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise PolicyError(str(exc)) from exc
    if minor <= 0:
        raise PolicyError("amount must be greater than zero")
    return minor


def fmt(minor: int, currency: str) -> str:
    return f"{minor // 100}.{minor % 100:02d} {currency.upper()}"


def norm_payee(payee) -> str:
    p = " ".join(str(payee or "").strip().lower().split())
    if not p:
        raise PolicyError("payee is required")
    if len(p) > 200:
        raise PolicyError("payee is too long")
    return p


def norm_currency(cur, default: str) -> str:
    c = str(cur or default).strip().lower()
    if not _CURRENCY_RE.match(c):
        raise PolicyError(f"currency {cur!r} is not a 3-letter code")
    return c


@dataclass(frozen=True)
class Config:
    currency: str
    per_payment_cap: int
    daily_cap: int
    per_merchant_daily_cap: int
    approval_threshold: int
    approve_new_payees: bool
    blocked_payees: frozenset
    allowed_payees: frozenset

    @classmethod
    def from_mapping(cls, m: dict | None) -> "Config":
        d = {**DEFAULTS, **{k: v for k, v in (m or {}).items() if v is not None}}
        cur = norm_currency(d["currency"], "usd")
        cfg = cls(
            currency=cur,
            per_payment_cap=to_minor(d["per_payment_cap"]),
            daily_cap=to_minor(d["daily_cap"]),
            per_merchant_daily_cap=to_minor(d["per_merchant_daily_cap"]),
            approval_threshold=to_minor(d["approval_threshold"]),
            approve_new_payees=bool(d["approve_new_payees"]),
            blocked_payees=frozenset(norm_payee(p) for p in d["blocked_payees"] or []),
            allowed_payees=frozenset(norm_payee(p) for p in d["allowed_payees"] or []),
        )
        if cfg.per_payment_cap > cfg.daily_cap:
            raise PolicyError("per_payment_cap cannot exceed daily_cap")
        return cfg


@dataclass(frozen=True)
class Request:
    tool: str
    payee: str
    amount: int
    currency: str
    key: str
    memo: str = ""


@dataclass(frozen=True)
class LedgerView:
    """What the ledger knows right now (including live reservations)."""
    frozen: bool
    spent_today: int
    spent_today_payee: int
    payee_known: bool
    key_status: str | None  # None | "pending" | "reserved" | "charging" | "settled"


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" | "approve" | "block" | "replay"
    code: str
    message: str
    reasons: tuple = field(default_factory=tuple)


def idempotency_key(tool: str, payee: str, amount: int, currency: str, scope: str, explicit: str | None) -> str:
    if explicit:
        k = str(explicit).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", k):
            raise PolicyError("idempotency_key may only use letters, digits and . _ : -")
        return f"{tool}:{k}"
    raw = json.dumps([tool, payee, amount, currency, scope], separators=(",", ":"))
    return f"{tool}:auto:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def decide(req: Request, cfg: Config, view: LedgerView) -> Decision:
    """Order matters: kill switch, duplicates, hard blocks, then approval triggers."""
    cur = cfg.currency
    if view.frozen:
        return Decision("block", "frozen", "Guardrails kill switch is ON. All spending is frozen until a human runs /guardrails unfreeze.")
    if view.key_status == "settled":
        return Decision("replay", "duplicate", f"Duplicate of an already-settled payment ({req.key}). No new charge.")
    if view.key_status in ("reserved", "charging"):
        return Decision("block", "in_flight", f"A payment with the same idempotency key ({req.key}) is already in flight. Not retrying.")
    if req.currency != cfg.currency:
        return Decision("block", "currency", f"Only {cur.upper()} is allowed; got {req.currency.upper()}.")
    if req.payee in cfg.blocked_payees:
        return Decision("block", "blocked_payee", f"Payee '{req.payee}' is on the block list.")
    if cfg.allowed_payees and req.payee not in cfg.allowed_payees:
        return Decision("block", "not_allowlisted", f"Payee '{req.payee}' is not on the allow list.")
    if req.amount > cfg.per_payment_cap:
        return Decision("block", "per_payment_cap", f"{fmt(req.amount, cur)} is over the per-payment cap of {fmt(cfg.per_payment_cap, cur)}.")
    if view.spent_today + req.amount > cfg.daily_cap:
        left = max(cfg.daily_cap - view.spent_today, 0)
        return Decision("block", "daily_cap", f"{fmt(req.amount, cur)} would pass the daily cap of {fmt(cfg.daily_cap, cur)} ({fmt(left, cur)} left today).")
    if view.spent_today_payee + req.amount > cfg.per_merchant_daily_cap:
        left = max(cfg.per_merchant_daily_cap - view.spent_today_payee, 0)
        return Decision("block", "per_merchant_cap", f"{fmt(req.amount, cur)} would pass the daily cap for '{req.payee}' of {fmt(cfg.per_merchant_daily_cap, cur)} ({fmt(left, cur)} left).")
    reasons = []
    if req.amount > cfg.approval_threshold:
        reasons.append(f"amount is over the {fmt(cfg.approval_threshold, cur)} approval threshold")
    if cfg.approve_new_payees and not view.payee_known:
        reasons.append("first payment to this payee")
    if reasons:
        return Decision("approve", "needs_approval",
                        f"Guardrails: approve paying {fmt(req.amount, cur)} to '{req.payee}'? ({'; '.join(reasons)})",
                        tuple(reasons))
    return Decision("allow", "ok", f"Within policy: {fmt(req.amount, cur)} to '{req.payee}'.")

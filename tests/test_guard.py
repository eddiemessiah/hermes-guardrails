import json
import sqlite3
import threading

import pytest
from hg_core.backends import BackendError, MockBackend, StripeTestBackend
from hg_core.guard import Guard
from hg_core.ledger import RESERVATION_TTL_S, Ledger


class Clock:
    def __init__(self):
        self.t = 1_793_000_000.0

    def __call__(self):
        return self.t


class CountingBackend(MockBackend):
    def __init__(self):
        self.calls = 0

    def charge(self, **kw):
        self.calls += 1
        return super().charge(**kw)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "g.db", clock=clock)
    backend = CountingBackend()
    cfg = {"guarded_tools": {"wallet_send": {"amount": "value", "payee": "to"}}}
    return Guard(ledger, lambda: cfg, lambda: backend), backend, clock, cfg


def run(g, args, tool="guardrails_pay"):
    """Simulate Hermes: pre_tool_call, then (if not blocked) the handler. Approval is granted."""
    d = g.pre_tool_call(tool, args, "s1")
    if d and d["action"] == "block":
        return d, None
    return d, g.pay(args) if tool == "guardrails_pay" else None


def test_happy_path_and_audit(env):
    g, b, _, _ = env
    d, r = run(g, {"payee": "acme", "amount": "10", "idempotency_key": "o1"})
    assert d["action"] == "approve"  # first payee
    assert r["ok"] and b.calls == 1
    d, r = run(g, {"payee": "acme", "amount": "5", "idempotency_key": "o2"})
    assert d is None and r["ok"]  # known payee, under threshold
    events = [e["event"] for e in g.ledger.entries(50)]
    assert events == ["approval_requested", "approved", "settled", "allowed", "settled"]
    assert g.ledger.verify()["ok"]


def test_overspend_blocked_and_nothing_charged(env):
    g, b, _, _ = env
    d, r = run(g, {"payee": "acme", "amount": "75.00"})
    assert d["action"] == "block" and "per-payment cap" in d["message"]
    assert b.calls == 0
    assert g.ledger.entries(1)[0]["event"] == "blocked"


def test_retry_never_double_charges(env):
    g, b, _, _ = env
    args = {"payee": "acme", "amount": "12.00", "idempotency_key": "order-7"}
    _, first = run(g, args)
    _, second = run(g, args)
    assert first["ok"] and second["duplicate"] and b.calls == 1
    assert second["receipt"]["id"] == first["receipt"]["id"]
    assert any(e["event"] == "duplicate_prevented" for e in g.ledger.entries(20))


def test_auto_key_dedupes_same_day(env):
    g, b, clock, _ = env
    run(g, {"payee": "acme", "amount": "3"})
    run(g, {"payee": "acme", "amount": "3"})
    assert b.calls == 1
    clock.t += 86400
    run(g, {"payee": "acme", "amount": "3"})
    assert b.calls == 2


def test_splitting_cannot_beat_caps(env):
    g, b, _, _ = env
    results = [run(g, {"payee": "acme", "amount": "20", "idempotency_key": f"s{i}"})[0] for i in range(7)]
    blocked = [d for d in results if d and d["action"] == "block"]
    assert b.calls == 5 and len(blocked) == 2  # 5 x 20 = per-payee cap of 100
    assert "daily cap for 'acme'" in blocked[0]["message"]


def test_kill_switch(env):
    g, b, _, _ = env
    g.ledger.set_frozen(True, "test")
    d, _ = run(g, {"payee": "acme", "amount": "1"})
    assert d["action"] == "block" and "kill switch" in d["message"] and b.calls == 0
    g.ledger.set_frozen(False, "test")
    assert run(g, {"payee": "acme", "amount": "1"})[1]["ok"]


def test_handler_refuses_without_gate(env):
    g, b, _, _ = env
    r = g.pay({"payee": "acme", "amount": "1"})
    assert r["blocked"] and b.calls == 0


def test_denied_approval_reservation_expires(env):
    g, b, clock, _ = env
    d = g.pre_tool_call("guardrails_pay", {"payee": "new", "amount": "40", "idempotency_key": "x"}, "s")
    assert d["action"] == "approve" and d["rule_key"] == "guardrails:guardrails_pay:x"
    assert g.ledger.totals()["total"] == 4000  # held while pending
    clock.t += RESERVATION_TTL_S + 1
    with g.ledger.tx() as db:
        g.ledger.view(db, "new", "k")
    assert g.ledger.totals()["total"] == 0 and b.calls == 0


def test_backend_failure_releases_budget(env):
    g, _, _, _ = env

    class Boom(MockBackend):
        def charge(self, **kw):
            raise BackendError("card declined")
    g._get_backend = lambda: Boom()
    _, r = run(g, {"payee": "acme", "amount": "5"})
    assert not r["ok"] and g.ledger.totals()["total"] == 0


def test_third_party_tool_is_guarded(env):
    g, _, _, _ = env
    assert g.pre_tool_call("wallet_send", {"value": "500", "to": "0xabc"}, "s")["action"] == "block"
    args = {"value": "2", "to": "0xabc"}
    assert g.pre_tool_call("wallet_send", args, "s")["action"] == "approve"
    g.post_tool_call("wallet_send", args, json.dumps({"tx": "0x1"}))
    assert g.ledger.payment(next(iter([r for r in [g.build_request("wallet_send", args, g.config()).key]])))["status"] == "settled"
    assert g.pre_tool_call("wallet_send", args, "s")["action"] == "block"  # duplicate
    assert g.pre_tool_call("terminal", {"command": "ls"}, "s") is None


def test_malformed_args_fail_closed(env):
    g, _, _, _ = env
    for bad in ({}, {"payee": "a"}, {"payee": "a", "amount": "-5"}, {"payee": "", "amount": "1"}):
        assert g.pre_tool_call("guardrails_pay", bad, "s")["action"] == "block"


def test_concurrent_requests_respect_cap(env):
    g, b, _, cfg = env
    cfg["approve_new_payees"] = False
    out = []

    def worker(i):
        out.append(run(g, {"payee": f"p{i % 3}", "amount": "20", "idempotency_key": f"c{i}"}))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert b.calls == 10 and g.ledger.totals()["total"] == 20000  # daily cap 200


def test_tamper_detected(env, tmp_path):
    g, _, _, _ = env
    for i in range(3):
        run(g, {"payee": "acme", "amount": "1", "idempotency_key": f"t{i}"})
    assert g.ledger.verify()["ok"]
    db = sqlite3.connect(tmp_path / "g.db")
    db.execute("UPDATE audit SET data=replace(data,'acme','evil') WHERE seq=1")
    db.commit()
    v = g.ledger.verify()
    assert not v["ok"] and v["broken_at"] == 1
    db.execute("DELETE FROM audit WHERE seq=3")
    db.commit()
    assert not g.ledger.verify()["ok"]


def test_stripe_refuses_live_keys():
    for k in ("", "sk_live_abc", "pk_test_x"):
        with pytest.raises(BackendError):
            StripeTestBackend(k)
    assert StripeTestBackend("sk_test_x").name == "stripe_test"


def test_status_shape(env):
    g, _, _, _ = env
    run(g, {"payee": "acme", "amount": "7.25"})
    s = g.status()
    assert s["spent_today"] == "7.25 USD" and s["left_today"] == "192.75 USD" and s["audit"]["ok"]

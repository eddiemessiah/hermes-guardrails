import pytest
from hg_core import policy as P

CFG = P.Config.from_mapping({})
V = P.LedgerView(frozen=False, spent_today=0, spent_today_payee=0, payee_known=True, key_status=None)


def req(amount="10.00", payee="acme", currency="usd", key="k"):
    return P.Request("guardrails_pay", P.norm_payee(payee), P.to_minor(amount), currency, key)


@pytest.mark.parametrize("raw,minor", [("10", 1000), ("10.5", 1050), ("0.01", 1), (12, 1200), ("50.00", 5000)])
def test_to_minor(raw, minor):
    assert P.to_minor(raw) == minor


@pytest.mark.parametrize("bad", ["-1", "0", "0.00", "1e3", "1.234", "abc", "", None, True, "NaN", "1,000"])
def test_to_minor_rejects(bad):
    with pytest.raises(P.PolicyError):
        P.to_minor(bad)


def test_allow_within_policy():
    assert P.decide(req(), CFG, V).action == "allow"


def test_per_payment_cap_blocks():
    d = P.decide(req("50.01"), CFG, V)
    assert (d.action, d.code) == ("block", "per_payment_cap")


def test_exactly_at_cap_needs_approval_not_block():
    assert P.decide(req("50.00"), CFG, V).action == "approve"


def test_daily_cap_blocks():
    v = P.LedgerView(False, 19500, 0, True, None)
    assert P.decide(req("10.00"), CFG, v).code == "daily_cap"


def test_per_merchant_cap_blocks():
    v = P.LedgerView(False, 9500, 9500, True, None)
    assert P.decide(req("10.00"), CFG, v).code == "per_merchant_cap"


def test_threshold_and_new_payee_need_approval():
    d = P.decide(req("30.00"), CFG, P.LedgerView(False, 0, 0, False, None))
    assert d.action == "approve" and len(d.reasons) == 2


def test_new_payee_approval_can_be_disabled():
    cfg = P.Config.from_mapping({"approve_new_payees": False})
    assert P.decide(req(), cfg, P.LedgerView(False, 0, 0, False, None)).action == "allow"


def test_frozen_beats_everything():
    v = P.LedgerView(True, 0, 0, True, "settled")
    assert P.decide(req(), CFG, v).code == "frozen"


def test_duplicate_and_in_flight():
    assert P.decide(req(), CFG, P.LedgerView(False, 0, 0, True, "settled")).action == "replay"
    assert P.decide(req(), CFG, P.LedgerView(False, 0, 0, True, "reserved")).code == "in_flight"


def test_block_and_allow_lists():
    cfg = P.Config.from_mapping({"blocked_payees": ["Evil Corp"]})
    assert P.decide(req(payee="evil   corp"), cfg, V).code == "blocked_payee"
    cfg = P.Config.from_mapping({"allowed_payees": ["acme"]})
    assert P.decide(req(payee="other"), cfg, V).code == "not_allowlisted"


def test_currency_mismatch():
    assert P.decide(req(currency="eur"), CFG, V).code == "currency"


def test_bad_config_fails_closed():
    with pytest.raises(P.PolicyError):
        P.Config.from_mapping({"per_payment_cap": "500", "daily_cap": "100"})
    with pytest.raises(P.PolicyError):
        P.Config.from_mapping({"daily_cap": "lots"})


def test_idempotency_key():
    a = P.idempotency_key("t", "acme", 100, "usd", "2026-11-04", None)
    assert a == P.idempotency_key("t", "acme", 100, "usd", "2026-11-04", None)
    assert a != P.idempotency_key("t", "acme", 101, "usd", "2026-11-04", None)
    assert P.idempotency_key("t", "x", 1, "usd", "d", "order-42") == "t:order-42"
    with pytest.raises(P.PolicyError):
        P.idempotency_key("t", "x", 1, "usd", "d", "bad key; drop")

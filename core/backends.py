"""Payment backends. MOCK needs nothing; Stripe runs in TEST MODE only."""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request


class BackendError(RuntimeError):
    pass


class MockBackend:
    name = "mock"

    def charge(self, *, key, payee, amount, currency, memo) -> dict:
        ref = "mock_" + hashlib.sha256(key.encode()).hexdigest()[:20]
        return {"id": ref, "backend": self.name, "status": "succeeded", "amount": amount, "currency": currency, "payee": payee}


class StripeTestBackend:
    """Creates and confirms a PaymentIntent with Stripe's test card. Refuses live keys."""
    name = "stripe_test"
    URL = "https://api.stripe.com/v1/payment_intents"

    def __init__(self, secret_key: str, timeout: float = 20.0):
        if not secret_key or not secret_key.startswith(("sk_test_", "rk_test_")):
            raise BackendError("Stripe backend needs a TEST key (sk_test_ or rk_test_). Live keys are refused.")
        self._key = secret_key
        self._timeout = timeout

    def charge(self, *, key, payee, amount, currency, memo) -> dict:
        body = urllib.parse.urlencode({
            "amount": amount, "currency": currency, "confirm": "true",
            "payment_method": "pm_card_visa",
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
            "description": (memo or f"Hermes Guardrails payment to {payee}")[:200],
            "metadata[payee]": payee[:200], "metadata[guardrails_key]": key[:200],
        }).encode()
        req = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._key}",
            "Idempotency-Key": hashlib.sha256(key.encode()).hexdigest(),
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read().decode()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise BackendError(f"Stripe test API returned {exc.code}: {msg[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BackendError(f"Stripe test API unreachable: {exc}") from None
        if data.get("livemode"):
            raise BackendError("Stripe reported livemode=true; refusing.")
        return {"id": data.get("id"), "backend": self.name, "status": data.get("status"),
                "amount": data.get("amount"), "currency": data.get("currency"), "payee": payee}

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


class StripeCheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class StripePayment:
    session_id: str
    organization_id: str
    designator: str
    gross_amount: Decimal
    processing_fee: Decimal
    net_credit: Decimal


class StripeCheckoutProvider:
    def __init__(self, *, secret_key: str = "", webhook_secret: str = ""):
        self.secret_key = secret_key.strip()
        self.webhook_secret = webhook_secret.strip()

    @classmethod
    def from_environment(cls):
        return cls(
            secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
            webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.secret_key and self.webhook_secret)

    @staticmethod
    def _stripe():
        try:
            import stripe
        except ImportError as exc:
            raise StripeCheckoutError("Stripe support is not installed.") from exc
        return stripe

    def create_checkout(
        self,
        *,
        organization_id: str,
        designator: str,
        administrator_email: str,
        amount: Decimal,
        success_url: str,
        cancel_url: str,
    ) -> str:
        if not self.is_configured:
            raise StripeCheckoutError("Online payments are not configured.")
        cents = int(
            (Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if cents < 1000 or cents > 1_000_000:
            raise StripeCheckoutError("Payment must be between $10 and $10,000.")
        stripe = self._stripe()
        stripe.api_key = self.secret_key
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                customer_email=administrator_email,
                client_reference_id=organization_id,
                metadata={
                    "organization_id": organization_id,
                    "designator": designator,
                },
                line_items=[{
                    "quantity": 1,
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": cents,
                        "product_data": {
                            "name": f"{designator} R2C Tracker prepaid funding",
                            "description": (
                                "Usage-based service funding. Actual Stripe "
                                "processing fees are deducted from account credit."
                            ),
                        },
                    },
                }],
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except Exception as exc:
            raise StripeCheckoutError("Stripe Checkout could not be started.") from exc
        url = str(getattr(session, "url", "") or session.get("url", "")).strip()
        if not url.startswith("https://"):
            raise StripeCheckoutError("Stripe did not return a secure Checkout URL.")
        return url

    def completed_payment(self, payload: bytes, signature: str) -> StripePayment | None:
        if not self.is_configured:
            raise StripeCheckoutError("Online payments are not configured.")
        stripe = self._stripe()
        stripe.api_key = self.secret_key
        try:
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                self.webhook_secret,
            )
        except Exception as exc:
            raise StripeCheckoutError("Stripe webhook signature is invalid.") from exc
        if event["type"] != "checkout.session.completed":
            return None
        session = event["data"]["object"]
        if session.get("payment_status") != "paid":
            return None
        try:
            expanded = stripe.checkout.Session.retrieve(
                session["id"],
                expand=["payment_intent.latest_charge.balance_transaction"],
            )
            transaction = expanded["payment_intent"]["latest_charge"][
                "balance_transaction"
            ]
            gross_cents = int(transaction["amount"])
            fee_cents = int(transaction["fee"])
            net_cents = int(transaction["net"])
            metadata = expanded.get("metadata") or session.get("metadata") or {}
            organization_id = str(metadata["organization_id"])
            designator = str(metadata["designator"])
        except Exception as exc:
            raise StripeCheckoutError(
                "Stripe payment details are not ready for reconciliation."
            ) from exc
        if gross_cents <= 0 or fee_cents < 0 or net_cents != gross_cents - fee_cents:
            raise StripeCheckoutError("Stripe returned inconsistent payment totals.")
        return StripePayment(
            session_id=str(session["id"]),
            organization_id=organization_id,
            designator=designator,
            gross_amount=Decimal(gross_cents) / 100,
            processing_fee=Decimal(fee_cents) / 100,
            net_credit=Decimal(net_cents) / 100,
        )

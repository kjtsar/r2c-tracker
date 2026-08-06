import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stripe_checkout import StripeCheckoutError, StripeCheckoutProvider


class StripeCheckoutProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = StripeCheckoutProvider(
            secret_key="sk_test_example",
            webhook_secret="whsec_example",
        )

    def test_checkout_uses_hosted_card_payment_and_organization_metadata(self):
        fake_stripe = SimpleNamespace(
            api_key="",
            checkout=SimpleNamespace(
                Session=SimpleNamespace(
                    create=Mock(return_value=SimpleNamespace(
                        url="https://checkout.stripe.test/session"
                    ))
                )
            ),
        )
        with patch.object(self.provider, "_stripe", return_value=fake_stripe):
            url = self.provider.create_checkout(
                organization_id="organization-1",
                designator="NCSSAR",
                administrator_email="admin@ncssar.example",
                amount=Decimal("25.00"),
                success_url="https://r2c-tracker.com/ncssar/admin?payment=success",
                cancel_url="https://r2c-tracker.com/ncssar/admin?payment=cancelled",
            )

        self.assertEqual("https://checkout.stripe.test/session", url)
        request = fake_stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual("payment", request["mode"])
        self.assertEqual(["card"], request["payment_method_types"])
        self.assertEqual(2500, request["line_items"][0]["price_data"]["unit_amount"])
        self.assertEqual("organization-1", request["metadata"]["organization_id"])

    def test_completed_payment_credits_net_after_actual_stripe_fee(self):
        session = {
            "id": "cs_test_123",
            "payment_status": "paid",
            "metadata": {
                "organization_id": "organization-1",
                "designator": "NCSSAR",
            },
        }
        fake_stripe = SimpleNamespace(
            api_key="",
            Webhook=SimpleNamespace(
                construct_event=Mock(return_value={
                    "type": "checkout.session.completed",
                    "data": {"object": session},
                })
            ),
            checkout=SimpleNamespace(
                Session=SimpleNamespace(
                    retrieve=Mock(return_value={
                        **session,
                        "payment_intent": {
                            "latest_charge": {
                                "balance_transaction": {
                                    "amount": 2500,
                                    "fee": 103,
                                    "net": 2397,
                                }
                            }
                        },
                    })
                )
            ),
        )
        with patch.object(self.provider, "_stripe", return_value=fake_stripe):
            payment = self.provider.completed_payment(
                json.dumps({"event": "signed"}).encode(),
                "t=1,v1=signature",
            )

        self.assertEqual(Decimal("25"), payment.gross_amount)
        self.assertEqual(Decimal("1.03"), payment.processing_fee)
        self.assertEqual(Decimal("23.97"), payment.net_credit)

    def test_checkout_rejects_payment_below_minimum(self):
        with self.assertRaises(StripeCheckoutError):
            self.provider.create_checkout(
                organization_id="organization-1",
                designator="NCSSAR",
                administrator_email="admin@ncssar.example",
                amount=Decimal("9.99"),
                success_url="https://r2c-tracker.com/success",
                cancel_url="https://r2c-tracker.com/cancel",
            )


if __name__ == "__main__":
    unittest.main()

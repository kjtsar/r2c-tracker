import unittest
from datetime import UTC, datetime, timedelta

from control_plane import (
    EnrollmentCampaignRecord,
    InvitationRecord,
    OrganizationRecord,
)
from enrollment import (
    ControlPlaneTokenService,
    EnrollmentTokenError,
    public_device_configuration,
)


class ControlPlaneTokenServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = ControlPlaneTokenService(
            "test-signing-key-that-is-longer-than-thirty-two-characters",
            "https://r2c-tracker.com",
        )
        self.now = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
        self.organization = OrganizationRecord(
            id="48e48566-9802-4ed3-b090-36d074a658b3",
            legal_name="North County Search and Rescue",
            designator="NCSSAR",
            hostname="r2c-tracker.com/ncssar",
            lifecycle_state="trial",
            provisioning_state="simulation ready",
            billing_mode="shadow billing",
            trial_ends_at=self.now + timedelta(days=30),
            records_visibility="restricted",
            record_retention_days=730,
            log_retention_days=30,
            notification_email="admin@example.org",
            primary_admin_name="Administrator",
            primary_admin_email="admin@example.org",
        )
        self.campaign = EnrollmentCampaignRecord(
            id="7e24e33d-4350-47af-b785-b8655e555df5",
            organization_id=self.organization.id,
            label="Drone team onboarding",
            state="active",
            max_redemptions=25,
            redemption_count=0,
            expires_at=self.now + timedelta(days=7),
            created_at=self.now,
            revoked_at=None,
        )

    def test_activation_claims_are_signed_and_bound_to_nonce(self):
        invitation = InvitationRecord(
            user_id="user-id",
            organization_id=self.organization.id,
            designator="NCSSAR",
            email="admin@example.org",
            activation_nonce="activation-nonce",
            expires_at=self.now + timedelta(days=7),
        )
        token = self.service.activation_token(invitation)

        claims = self.service.decode_activation(token)

        self.assertEqual("user-id", claims.user_id)
        self.assertEqual("activation-nonce", claims.nonce)
        with self.assertRaises(EnrollmentTokenError):
            self.service.decode_activation(token + "tampered")

    def test_qr_url_contains_only_signed_enrollment_locator(self):
        url = self.service.enrollment_url(self.organization, self.campaign)
        token = url.split("token=", 1)[1]

        claims = self.service.decode_enrollment(token)

        self.assertEqual(self.campaign.id, claims.campaign_id)
        self.assertEqual(self.organization.id, claims.organization_id)
        self.assertNotIn("admin@example.org", url)
        self.assertNotIn("FAA", url)
        self.assertNotIn("password", url.lower())

    def test_simulation_configuration_never_contains_credentials(self):
        configuration = public_device_configuration(self.organization)

        self.assertIsNone(configuration["tracker"]["upload_credential"])
        self.assertIsNone(configuration["tracker"]["faa_proxy_credential"])
        self.assertEqual(
            "pending_tenant_provisioning",
            configuration["credential_exchange"]["status"],
        )

    def test_expired_or_exhausted_campaign_is_not_usable(self):
        expired = EnrollmentCampaignRecord(
            **{
                **self.campaign.__dict__,
                "expires_at": self.now - timedelta(seconds=1),
            }
        )
        exhausted = EnrollmentCampaignRecord(
            **{
                **self.campaign.__dict__,
                "redemption_count": self.campaign.max_redemptions,
            }
        )

        self.assertFalse(expired.is_usable(self.now))
        self.assertFalse(exhausted.is_usable(self.now))
        self.assertTrue(self.campaign.is_usable(self.now))


if __name__ == "__main__":
    unittest.main()

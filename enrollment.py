from dataclasses import dataclass
from urllib.parse import urlencode

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from control_plane import (
    EnrollmentCampaignRecord,
    InvitationRecord,
    OrganizationRecord,
)


class EnrollmentTokenError(Exception):
    pass


@dataclass(frozen=True)
class ActivationClaims:
    user_id: str
    organization_id: str
    designator: str
    email: str
    nonce: str


@dataclass(frozen=True)
class EnrollmentClaims:
    campaign_id: str
    organization_id: str
    designator: str
    token_generation: str


class ControlPlaneTokenService:
    def __init__(self, signing_key: str, public_base_url: str):
        if len(signing_key) < 32:
            raise ValueError("Control-plane signing key must be at least 32 characters.")
        self.public_base_url = public_base_url.rstrip("/")
        if not self.public_base_url.startswith("https://"):
            raise ValueError("Public control-plane URL must use HTTPS.")
        self.activation = URLSafeTimedSerializer(
            signing_key,
            salt="r2c-organization-activation-v1",
        )
        self.enrollment = URLSafeTimedSerializer(
            signing_key,
            salt="r2c-device-enrollment-v1",
        )

    def activation_token(self, invitation: InvitationRecord) -> str:
        return self.activation.dumps(
            {
                "kind": "organization_activation",
                "user_id": invitation.user_id,
                "organization_id": invitation.organization_id,
                "designator": invitation.designator,
                "email": invitation.email,
                "nonce": invitation.activation_nonce,
            }
        )

    def activation_url(self, invitation: InvitationRecord) -> str:
        return (
            f"{self.public_base_url}/{invitation.designator.lower()}/activate?"
            f"{urlencode({'token': self.activation_token(invitation)})}"
        )

    def decode_activation(
        self,
        token: str,
        max_age_seconds: int = 7 * 24 * 3600,
    ) -> ActivationClaims:
        try:
            payload = self.activation.loads(token, max_age=max_age_seconds)
        except (BadSignature, SignatureExpired) as exc:
            raise EnrollmentTokenError(
                "Administrator activation link is invalid or expired."
            ) from exc
        if payload.get("kind") != "organization_activation":
            raise EnrollmentTokenError("Administrator activation link is invalid.")
        try:
            return ActivationClaims(
                user_id=payload["user_id"],
                organization_id=payload["organization_id"],
                designator=payload["designator"],
                email=payload["email"],
                nonce=payload["nonce"],
            )
        except (KeyError, TypeError) as exc:
            raise EnrollmentTokenError(
                "Administrator activation link is invalid."
            ) from exc

    def enrollment_token(
        self,
        organization: OrganizationRecord,
        campaign: EnrollmentCampaignRecord,
    ) -> str:
        return self.enrollment.dumps(
            {
                "kind": "device_enrollment",
                "campaign_id": campaign.id,
                "organization_id": organization.id,
                "designator": organization.designator,
                "token_generation": campaign.token_generation,
            }
        )

    def enrollment_url(
        self,
        organization: OrganizationRecord,
        campaign: EnrollmentCampaignRecord,
    ) -> str:
        token = self.enrollment_token(organization, campaign)
        return (
            f"{self.public_base_url}/{organization.designator.lower()}/enroll?"
            f"{urlencode({'token': token})}"
        )

    def decode_enrollment(
        self,
        token: str,
        max_age_seconds: int = 30 * 24 * 3600,
    ) -> EnrollmentClaims:
        try:
            payload = self.enrollment.loads(token, max_age=max_age_seconds)
        except (BadSignature, SignatureExpired) as exc:
            raise EnrollmentTokenError(
                "Device enrollment code is invalid or expired."
            ) from exc
        if payload.get("kind") != "device_enrollment":
            raise EnrollmentTokenError("Device enrollment code is invalid.")
        try:
            return EnrollmentClaims(
                campaign_id=payload["campaign_id"],
                organization_id=payload["organization_id"],
                designator=payload["designator"],
                token_generation=str(payload.get("token_generation", "")),
            )
        except (KeyError, TypeError) as exc:
            raise EnrollmentTokenError("Device enrollment code is invalid.") from exc


def public_device_configuration(
    organization: OrganizationRecord,
    *,
    tracker_base_url: str | None = None,
    credential_issuance_enabled: bool = False,
) -> dict:
    """Return the non-secret portion of the app enrollment response."""
    base_url = (
        tracker_base_url.rstrip("/")
        if tracker_base_url
        else f"https://{organization.hostname}"
    )
    return {
        "schema_version": 1,
        "organization": {
            "designator": organization.designator,
            "name": organization.legal_name,
        },
        "tracker": {
            "base_url": base_url,
            "upload_credential": None,
            "faa_proxy_credential": None,
        },
        "credential_exchange": {
            "status": (
                "pilot_available"
                if credential_issuance_enabled
                else "pending_tenant_provisioning"
            ),
            "message": (
                "Open this enrollment code in RID2Caltopo to install a "
                "revocable pilot tracker credential."
                if credential_issuance_enabled
                else (
                    "The organization locator is valid, but tenant-scoped app "
                    "credentials are not issued in simulation mode."
                )
            ),
        },
    }

import base64
import hashlib
import os
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import urlencode

import requests


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class PlatformAdminAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    name: str


@dataclass(frozen=True)
class GoogleGmailAuthorization:
    identity: GoogleIdentity
    refresh_token: str


class GoogleOidcClient:
    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorization_request(self, redirect_uri: str) -> tuple[str, dict[str, str]]:
        if not self.is_configured:
            raise PlatformAdminAuthError("Google sign-in is not configured.")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        url = GOOGLE_AUTHORIZATION_ENDPOINT + "?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return url, {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
        }

    def gmail_authorization_request(
        self, redirect_uri: str
    ) -> tuple[str, dict[str, str]]:
        url, flow = self.authorization_request(redirect_uri)
        state = flow["state"]
        nonce = flow["nonce"]
        verifier = flow["verifier"]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        url = GOOGLE_AUTHORIZATION_ENDPOINT + "?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": f"openid email profile {GMAIL_SEND_SCOPE}",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
            }
        )
        return url, flow

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        verifier: str,
        expected_nonce: str,
    ) -> GoogleIdentity:
        if not self.is_configured:
            raise PlatformAdminAuthError("Google sign-in is not configured.")
        token_payload, claims = self._exchange_and_verify(
            code=code,
            redirect_uri=redirect_uri,
            verifier=verifier,
            expected_nonce=expected_nonce,
        )
        del token_payload
        return self._identity_from_claims(claims)

    def exchange_gmail_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        verifier: str,
        expected_nonce: str,
    ) -> GoogleGmailAuthorization:
        token_payload, claims = self._exchange_and_verify(
            code=code,
            redirect_uri=redirect_uri,
            verifier=verifier,
            expected_nonce=expected_nonce,
        )
        refresh_token = str(token_payload.get("refresh_token", "")).strip()
        if not refresh_token:
            raise PlatformAdminAuthError(
                "Google did not return an offline Gmail credential."
            )
        return GoogleGmailAuthorization(
            identity=self._identity_from_claims(claims),
            refresh_token=refresh_token,
        )

    def _exchange_and_verify(
        self,
        *,
        code: str,
        redirect_uri: str,
        verifier: str,
        expected_nonce: str,
    ) -> tuple[dict, dict]:
        if not self.is_configured:
            raise PlatformAdminAuthError("Google sign-in is not configured.")
        try:
            response = requests.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
                timeout=15,
            )
            response.raise_for_status()
            token_payload = response.json()
            id_token_value = token_payload.get("id_token", "")
            if not id_token_value:
                raise PlatformAdminAuthError(
                    "Google did not return an identity token."
                )
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token

            claims = id_token.verify_oauth2_token(
                id_token_value,
                GoogleRequest(),
                self.client_id,
            )
        except PlatformAdminAuthError:
            raise
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Google sign-in could not be verified."
            ) from exc
        if not claims.get("email_verified"):
            raise PlatformAdminAuthError("Google has not verified this email address.")
        if not secrets.compare_digest(
            str(claims.get("nonce", "")),
            expected_nonce,
        ):
            raise PlatformAdminAuthError("Google sign-in nonce did not match.")
        return token_payload, claims

    @staticmethod
    def _identity_from_claims(claims: dict) -> GoogleIdentity:
        subject = str(claims.get("sub", "")).strip()
        email = str(claims.get("email", "")).strip().lower()
        if not subject or not email:
            raise PlatformAdminAuthError("Google identity was incomplete.")
        return GoogleIdentity(
            subject=subject,
            email=email,
            name=str(claims.get("name", "")).strip(),
        )


def _password_setup_message(
    from_address: str, recipient: str, setup_url: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Set your R2C Tracker administrator password"
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        "A password setup or recovery request was made for the R2C Tracker "
        "platform administrator.\n\n"
        f"Use this single-use link within five minutes:\n{setup_url}\n\n"
        "If you did not request this message, no action is required."
    )
    return message


def _organization_activation_message(
    from_address: str,
    recipient: str,
    administrator_name: str,
    organization_name: str,
    designator: str,
    activation_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Activate your {designator} R2C Tracker administrator account"
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        f"Hello {administrator_name},\n\n"
        f"You have been invited to administer {organization_name} "
        f"({designator}) in R2C Tracker.\n\n"
        "Use this single-use link within seven days to verify your identity "
        "with one of the sign-in providers offered by R2C Tracker:\n"
        f"{activation_url}\n\n"
        f"By activating, you confirm that you are authorized to enable "
        f"{organization_name}'s R2C Tracker account. If prepaid credit is not "
        "available, the 30-day trial begins immediately. Prepaid credit keeps "
        "the account funded until cumulative attributed GCP usage consumes it; "
        "a 30-day grace period then begins. After activation, R2C "
        "Tracker will sign you in and open the organization administration "
        "page.\n\n"
        "If you were not expecting this invitation, ignore this message. "
        "Do not forward the activation link."
    )
    return message


def _organization_password_reset_message(
    from_address: str,
    recipient: str,
    organization_name: str,
    designator: str,
    reset_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Reset your {designator} R2C Tracker password"
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        f"A password reset was requested for your {organization_name} "
        f"({designator}) R2C Tracker account.\n\n"
        f"Use this single-use link within 15 minutes:\n{reset_url}\n\n"
        "If you did not request this reset, ignore this message. Your "
        "existing password has not been changed."
    )
    return message


def _organization_access_message(
    from_address: str,
    recipient: str,
    administrator_name: str,
    organization_name: str,
    designator: str,
    login_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Administrator access for {designator} R2C Tracker"
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        f"Hello {administrator_name},\n\n"
        f"You have been assigned as the primary administrator for {organization_name} "
        f"({designator}) in R2C Tracker.\n\n"
        "Use this organization-specific link to sign in:\n"
        f"{login_url}\n\n"
        f"Choose Continue with Google and use {recipient}. No separate R2C "
        "Tracker username or password is required when signing in with "
        "Google.\n\n"
        "This administrator account is already active. If you were not "
        "expecting this notice, contact the R2C Tracker platform administrator "
        f"at {from_address}."
    )
    return message


def _organization_administrator_changed_message(
    from_address: str,
    recipient: str,
    former_administrator_name: str,
    organization_name: str,
    designator: str,
    new_administrator_name: str,
    new_administrator_email: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Primary administrator changed for {designator} R2C Tracker"
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        f"Hello {former_administrator_name},\n\n"
        f"This is an accountability notice that the primary administrator for "
        f"{organization_name} ({designator}) in R2C Tracker has been changed to "
        f"{new_administrator_name} ({new_administrator_email}).\n\n"
        "Your former primary-administrator access has been disabled. If this "
        "change is unexpected, contact the R2C Tracker platform administrator "
        f"immediately at {from_address}."
    )
    return message


def _organization_funding_exhausted_message(
    from_address: str,
    recipient: str,
    administrator_name: str,
    organization_name: str,
    designator: str,
    grace_ends: str,
    administration_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"{designator} R2C Tracker funding has been consumed"
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        f"Hello {administrator_name},\n\n"
        f"Cumulative attributed GCP usage for {organization_name} "
        f"({designator}) has consumed its prepaid R2C Tracker funding. "
        f"The account is now in its 30-day grace period through {grace_ends}.\n\n"
        "Review usage and add prepaid funding from the organization "
        f"administration page:\n{administration_url}\n\n"
        "R2C Tracker is usage-funded rather than a fixed-price subscription. "
        "Payments show the gross amount, Stripe processing fee, and net "
        "service credit so the organization can budget transparently.\n\n"
        f"Questions may be sent to {from_address}."
    )
    return message


def _organization_lifecycle_deadline_message(
    from_address: str,
    recipient: str,
    administrator_name: str,
    organization_name: str,
    designator: str,
    lifecycle_label: str,
    timing: str,
    deadline: str,
    administration_url: str,
    records_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = (
        f"{designator} R2C Tracker {lifecycle_label} {timing}"
    )
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(
        f"Hello {administrator_name},\n\n"
        f"The {organization_name} ({designator}) R2C Tracker "
        f"{lifecycle_label} {timing}. The recorded deadline is {deadline}.\n\n"
        "R2C Tracker does not automatically archive or shut down an "
        "organization when this deadline passes. The platform administrator "
        "will contact your organization administrator before any archival or "
        "service shutdown.\n\n"
        "To preserve organization-owned flight records:\n"
        f"1. Sign in and review service status: {administration_url}\n"
        f"2. Open flight administration: {records_url}\n"
        "3. Select Export as CSV for the flight-record summary.\n"
        "4. Select Download Flight Log Archive for the available raw logs, "
        "then store both downloads securely.\n\n"
        "Reply to this message or contact the R2C Tracker platform "
        f"administrator at {from_address} to discuss continued access."
    )
    return message


class GmailApiPlatformAdminEmailSender:
    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        from_address: str = "",
    ):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.from_address = from_address.strip()

    @classmethod
    def from_environment(cls):
        return cls(
            client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            refresh_token=os.environ.get("PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN", ""),
            from_address=os.environ.get("PLATFORM_EMAIL_FROM", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(
            self.client_id
            and self.client_secret
            and self.refresh_token
            and self.from_address
        )

    def _send(self, message: EmailMessage, failure_message: str) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        try:
            token_response = requests.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=15,
            )
            token_response.raise_for_status()
            access_token = str(token_response.json().get("access_token", "")).strip()
            if not access_token:
                raise PlatformAdminAuthError("Google did not return an access token.")
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            send_response = requests.post(
                GMAIL_SEND_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"raw": raw_message},
                timeout=15,
            )
            send_response.raise_for_status()
        except PlatformAdminAuthError:
            raise
        except Exception as exc:
            raise PlatformAdminAuthError(failure_message) from exc

    def send_password_setup(self, *, recipient: str, setup_url: str) -> None:
        message = _password_setup_message(self.from_address, recipient, setup_url)
        self._send(message, "Administrator email could not be sent.")

    def send_organization_activation(self, **kwargs) -> None:
        message = _organization_activation_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["administrator_name"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["activation_url"],
        )
        self._send(message, "Organization activation email could not be sent.")

    def send_organization_password_reset(self, **kwargs) -> None:
        message = _organization_password_reset_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["reset_url"],
        )
        self._send(message, "Organization password reset email could not be sent.")

    def send_organization_access(self, **kwargs) -> None:
        message = _organization_access_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["administrator_name"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["login_url"],
        )
        self._send(message, "Organization access email could not be sent.")

    def send_organization_administrator_changed(self, **kwargs) -> None:
        message = _organization_administrator_changed_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["former_administrator_name"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["new_administrator_name"],
            kwargs["new_administrator_email"],
        )
        self._send(message, "Former administrator advisory email could not be sent.")

    def send_organization_funding_exhausted(self, **kwargs) -> None:
        message = _organization_funding_exhausted_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["administrator_name"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["grace_ends"],
            kwargs["administration_url"],
        )
        self._send(message, "Funding notification email could not be sent.")

    def send_organization_lifecycle_deadline(self, **kwargs) -> None:
        message = _organization_lifecycle_deadline_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["administrator_name"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["lifecycle_label"],
            kwargs["timing"],
            kwargs["deadline"],
            kwargs["administration_url"],
            kwargs["records_url"],
        )
        self._send(message, "Lifecycle notification email could not be sent.")


class SmtpPlatformAdminEmailSender:
    def __init__(
        self,
        *,
        host: str = "",
        port: int = 587,
        username: str = "",
        password: str = "",
        from_address: str = "",
    ):
        self.host = host.strip()
        self.port = port
        self.username = username.strip()
        self.password = password
        self.from_address = from_address.strip()

    @classmethod
    def from_environment(cls):
        try:
            port = int(os.environ.get("PLATFORM_EMAIL_SMTP_PORT", "587"))
        except ValueError:
            port = 0
        return cls(
            host=os.environ.get("PLATFORM_EMAIL_SMTP_HOST", ""),
            port=port,
            username=os.environ.get("PLATFORM_EMAIL_SMTP_USER", ""),
            password=os.environ.get("PLATFORM_EMAIL_SMTP_PASSWORD", ""),
            from_address=os.environ.get("PLATFORM_EMAIL_FROM", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(
            self.host
            and 1 <= self.port <= 65535
            and self.from_address
            and (not self.username or self.password)
        )

    def send_password_setup(self, *, recipient: str, setup_url: str) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _password_setup_message(self.from_address, recipient, setup_url)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Administrator email could not be sent."
            ) from exc

    def send_organization_activation(
        self,
        *,
        recipient: str,
        administrator_name: str,
        organization_name: str,
        designator: str,
        activation_url: str,
    ) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _organization_activation_message(
            self.from_address,
            recipient,
            administrator_name,
            organization_name,
            designator,
            activation_url,
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Organization activation email could not be sent."
            ) from exc

    def send_organization_password_reset(
        self,
        *,
        recipient: str,
        organization_name: str,
        designator: str,
        reset_url: str,
    ) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _organization_password_reset_message(
            self.from_address,
            recipient,
            organization_name,
            designator,
            reset_url,
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Organization password reset email could not be sent."
            ) from exc

    def send_organization_access(
        self,
        *,
        recipient: str,
        administrator_name: str,
        organization_name: str,
        designator: str,
        login_url: str,
    ) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _organization_access_message(
            self.from_address,
            recipient,
            administrator_name,
            organization_name,
            designator,
            login_url,
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Organization access email could not be sent."
            ) from exc

    def send_organization_administrator_changed(
        self,
        *,
        recipient: str,
        former_administrator_name: str,
        organization_name: str,
        designator: str,
        new_administrator_name: str,
        new_administrator_email: str,
    ) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _organization_administrator_changed_message(
            self.from_address,
            recipient,
            former_administrator_name,
            organization_name,
            designator,
            new_administrator_name,
            new_administrator_email,
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Former administrator advisory email could not be sent."
            ) from exc

    def send_organization_funding_exhausted(
        self,
        *,
        recipient: str,
        administrator_name: str,
        organization_name: str,
        designator: str,
        grace_ends: str,
        administration_url: str,
    ) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _organization_funding_exhausted_message(
            self.from_address,
            recipient,
            administrator_name,
            organization_name,
            designator,
            grace_ends,
            administration_url,
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Funding notification email could not be sent."
            ) from exc

    def send_organization_lifecycle_deadline(self, **kwargs) -> None:
        if not self.is_configured:
            raise PlatformAdminAuthError("Administrator email is not configured.")
        message = _organization_lifecycle_deadline_message(
            self.from_address,
            kwargs["recipient"],
            kwargs["administrator_name"],
            kwargs["organization_name"],
            kwargs["designator"],
            kwargs["lifecycle_label"],
            kwargs["timing"],
            kwargs["deadline"],
            kwargs["administration_url"],
            kwargs["records_url"],
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:
            raise PlatformAdminAuthError(
                "Lifecycle notification email could not be sent."
            ) from exc

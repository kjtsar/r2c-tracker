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


class PlatformAdminAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    name: str


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
            id_token_value = response.json().get("id_token", "")
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
        subject = str(claims.get("sub", "")).strip()
        email = str(claims.get("email", "")).strip().lower()
        if not subject or not email:
            raise PlatformAdminAuthError("Google identity was incomplete.")
        return GoogleIdentity(
            subject=subject,
            email=email,
            name=str(claims.get("name", "")).strip(),
        )


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
        message = EmailMessage()
        message["Subject"] = "Set your R2C Tracker administrator password"
        message["From"] = self.from_address
        message["To"] = recipient
        message.set_content(
            "A password setup or recovery request was made for the R2C Tracker "
            "platform administrator.\n\n"
            f"Use this single-use link within five minutes:\n{setup_url}\n\n"
            "If you did not request this message, no action is required."
        )
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
        message = EmailMessage()
        message["Subject"] = f"Activate the {designator} R2C Tracker pilot"
        message["From"] = self.from_address
        message["To"] = recipient
        message.set_content(
            f"Hello {administrator_name},\n\n"
            f"An R2C Tracker pilot account has been prepared for "
            f"{organization_name} ({designator}).\n\n"
            "This is a pilot service. Features, retention, usage accounting, "
            "and availability are still being qualified; no payment is due.\n\n"
            "Use this link within seven days to activate the organization "
            f"administrator account:\n{activation_url}\n\n"
            "If you were not expecting this invitation, do not use the link."
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

# R2C Control Plane Setup

The control plane is deliberately separate from each tenant tracker database.
It stores commercial metadata, organization administrators, aggregate daily
usage, subscription state, provisioning jobs, enrollment campaigns, and an
append-only billing ledger. It must not be pointed at `DATABASE_URL`.

## Local simulation

Use a separate local SQLite database and non-production credentials:

```bash
export CONTROL_PLANE_DATABASE_URL="sqlite+aiosqlite:///./control-plane.local.db"
export CONTROL_PLANE_MODE="simulation"
export CONTROL_PLANE_PUBLIC_URL="https://r2c-tracker.com"
export CONTROL_PLANE_SIGNING_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DEVICE_CREDENTIAL_ISSUANCE_ENABLED="true"
export CONTROL_PLANE_TRACKER_BASE_URL="https://r2c-tracker.com"
```

Do not paste a generated secret by itself at a shell prompt. The command
substitutions above assign generated values directly to environment variables.
For repeated local use, store them in a mode-`0600` `.env.*.local` file, which
is ignored by Git.

The super-admin identity is not an environment variable. Select the intended
GCI project, authenticate Application Default Credentials, and create or rotate
the infrastructure identity:

```bash
./set_super_admin.sh platform-admin@example.test "Platform Administrator"
```

The script writes JSON containing only the email and display name to the
`r2c-super-admin-identity` Secret Manager secret. The application reads its
`latest` version dynamically on demand and caches successful reads for no more
than 30 seconds. It does not poll while idle. Unit tests use an injected fake
provider and do not require a cloud secret.

Start the tracker and visit `/platform-admin/login`. Simulation mode creates
commercial records, activation links, organization sessions, and QR campaigns
but does not send email, collect payments, or create tenant resources. Device
credential issuance may be enabled independently for the shared pilot.

## Billing export

Enable the read-only provider with:

```bash
export PLATFORM_BILLING_SOURCE="bigquery"
export PLATFORM_BILLING_PROJECT="r2c-tracker-platform"
export PLATFORM_BILLING_DATASET="r2c_billing_export"
export PLATFORM_BILLING_INCLUDED_PROJECTS="shaped-splicer-482602-v1,r2c-tracker-pilot,r2c-tracker-platform"
```

The project allowlist is mandatory. This prevents unrelated projects linked to
the same Cloud Billing account from appearing in R2C totals.

Local Google client libraries use Application Default Credentials:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project r2c-tracker-platform
```

## Production prerequisites

Do not switch `CONTROL_PLANE_MODE` to `live` until all of these are true:

1. A dedicated control-plane PostgreSQL database exists.
2. Database credentials, `CONTROL_PLANE_SIGNING_KEY`, and `SECRET_KEY` are
   stored in Secret Manager. The `r2c-super-admin-identity` secret has a valid
   email/display-name JSON value and the runtime service account can read it.
3. The runtime service account has BigQuery Job User on the billing project and
   read-only access to `r2c_billing_export`.
4. `SESSION_COOKIE_HTTPS_ONLY=true`.
5. Tenant provisioning remains separately authenticated and cannot return
   flight data to platform-admin routes.
6. Outbound administrator email has a configured sender, bounce handling, and
   rate limits.
7. QR redemption exchanges the signed campaign locator for tenant-scoped,
   short-lived credentials. It must not return a shared long-lived upload key.
8. Payment integration is still in test mode and webhook signatures plus
   idempotency are verified.

Cloud SQL creation and payment-provider enrollment can incur charges or create
external obligations, so they are intentionally not performed by the local
simulation setup.

## Hosted evaluation deployment

The hosted evaluation runs on `https://r2c-tracker.com` in the isolated
`r2c-tracker-pilot` project. The control plane uses `live` onboarding mode:

- onboarding records, trials, balances, roles, and enrollment campaigns persist
- the control plane uses a separate database on the pilot PostgreSQL VM
- administrator activation email is sent through the Gmail API using the
  send-only OAuth scope
- no payment is collected
- tenant-scoped accounts and revocable app credentials may be issued

Prepare or verify the pilot resources with:

```bash
./setup_pilot_control_plane.sh
./set_super_admin.sh kjtsar@kjt.us "R2C Platform Administrator"
./setup_pilot_local.sh
```

No platform-admin email or bootstrap password is copied into Cloud Run
environment variables or `.env.pilot.local`. A login or privileged request
refreshes an expired 30-second identity cache; there is no background polling.
A new secret version invalidates the former administrator's sessions, disables
that account, and creates an
uninitialized account for the replacement without transferring a password.
Google and five-minute email-link enrollment must be operational before
activating a replacement who has no existing password.

## Super-admin authentication

Google sign-in requires a Web application OAuth client with this exact
pair of authorized redirect URIs:

```text
https://r2c-tracker.com/platform-admin/google/callback
https://r2c-tracker.com/google/callback
```

Store its values in separate Secret Manager secrets:

- `r2c-google-oauth-client-id`
- `r2c-google-oauth-client-secret`

After downloading the Web client JSON from Google Auth Platform, validate its
redirect URI and store both values without printing them:

```bash
./setup_google_oauth_secrets.sh /path/to/client_secret.json
```

Normal sign-in requests only `openid email profile`. It verifies the signed
ID token, audience, issuer, one-time nonce, and `email_verified`, then requires
an exact match with the current infrastructure identity. The authenticated
platform-account setup action additionally requests `gmail.send` and stores the
offline credential directly in Secret Manager. The runtime cannot read Gmail.

For the guarded pilot, deploy the simulation-mode setup revision, sign in at
`/platform-admin/account`, and select **Connect Gmail sender**. After consent,
deploy live mode with the resulting secret mapped read-only:

```bash
CONTROL_PLANE_MODE=live \
PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME=r2c-platform-email-gmail-refresh-token \
PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET= \
./deploy_pilot.sh APP_VERSION_CODE
```

The optional STARTTLS SMTP fallback for self-hosted deployments uses:

- `PLATFORM_EMAIL_SMTP_HOST`
- `PLATFORM_EMAIL_SMTP_PORT` (normally `587`)
- `PLATFORM_EMAIL_SMTP_USER` when SMTP authentication is required
- `PLATFORM_EMAIL_FROM`
- `PLATFORM_EMAIL_SMTP_PASSWORD`, mapped only from Secret Manager

SMTP delivery always upgrades with STARTTLS. Setup requests return the same
response for authorized and unauthorized addresses, allow no more than one
message per minute and five per hour, and create a hashed single-use token that
expires after five minutes. The emailed link carries the token after `#`, so
the token never enters the HTTP URL received or logged by Cloud Run.

Configure the SMTP fallback without exposing its password at the shell prompt:

```bash
./setup_pilot_email.sh SMTP_HOST SMTP_USER FROM_ADDRESS
```

The script prompts without echo, writes a new Secret Manager version, grants
only the pilot runtime service account access, and prints the live deployment
command. The hosted pilot uses Gmail API OAuth instead.

Use a tagged, zero-traffic canary before promoting a revision:

```bash
ACTIVATE_LATEST_REVISION=0 \
REVISION_TAG=admin-candidate \
./deploy_pilot.sh 98
```

Verify the tagged URL, including one authenticated FAA request, before assigning
production traffic. Keep the prior ready revision name so rollback is an
explicit traffic update rather than another build.

## Future test and production split

The shared pilot is temporary. Before enabling email, payments, tenant resource
creation, or real device credentials, split the environments:

Pending organization members can activate immediately with a verified Google
account whose email exactly matches their membership record. An activation
email is not sent unless a Gmail API or SMTP sender is configured; the
administration page states this explicitly.

| Boundary | Test | Production |
| --- | --- | --- |
| Google Cloud project | dedicated test project | dedicated production project |
| Public entry point | `test.r2c-tracker.com` | `r2c-tracker.com` |
| Organization routing | test-only path or test subdomain | organization subdomain |
| Control-plane database | test-only PostgreSQL | production-only PostgreSQL |
| Tenant databases/storage | synthetic data only | organization-isolated resources |
| Signing/session keys | test-only secrets | production-only secrets |
| Email and payments | sandbox providers | live providers |
| QR enrollment | test credentials only | production tenant credentials |
| Billing export | test project allowlist | production project allowlist |

Never copy a control-plane database, signing key, session key, payment webhook
secret, or device credential between environments. QR tokens must be
environment-bound so a test enrollment URL cannot be redeemed in production.

Build one immutable container image, qualify its digest in test, and promote the
same digest to production. Do not rebuild production from an uncommitted
worktree once the split is in place.

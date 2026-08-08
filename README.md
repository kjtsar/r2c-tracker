# R2C-Tracker

A FastAPI-based flight log aggregator for SAR operations.

## Setup
1. `python3 -m venv .venv`
2. `.venv/bin/python -m pip install -r requirements.txt`
3. `source .env`
4. `.venv/bin/python -m uvicorn main:app --reload --host 127.0.0.1 --port $TRACKER_PORT`

## Environment Variables
- `TRACKER_PORT`: unrestricted port number (i.e. 8080)
- `TRACKER_API_KEY`: Key required for /upload
- `TRACKER_ADMIN_PASS`: Password for the /admin portal
- `PLATFORM_BILLING_SOURCE`: `illustrative` (default) or `bigquery`. The live
  mode reads aggregate cost only and never queries tenant operational data.
- `PLATFORM_BILLING_PROJECT`: Project containing the billing export dataset
- `STRIPE_SECRET_KEY`: Stripe test or live secret key used only by the server.
- `STRIPE_WEBHOOK_SECRET`: Signing secret for the public
  `/billing/stripe/webhook` endpoint. Both Stripe values must be configured
  together. Until then, organization administrators see payments as pending
  setup and no Checkout session can be created.
  (default `r2c-tracker-platform`).
- `PLATFORM_BILLING_DATASET`: Billing export dataset ID (default
  `r2c_billing_export`).
- `PLATFORM_BILLING_INCLUDED_PROJECTS`: Required comma-separated allowlist of
  R2C Google Cloud project IDs when live billing is enabled. Costs from other
  projects on the billing account are excluded.
- `CONTROL_PLANE_DATABASE_URL`: Separate SQLAlchemy async database URL for
  commercial records, organization administrators, aggregate usage, and the
  append-only billing ledger. Organization administration is disabled when
  unset. For local simulation use a separate SQLite file, never `DATABASE_URL`.
- `CONTROL_PLANE_MODE`: `simulation` (default) or `live`. Simulation stores
  reviewable onboarding state but does not create tenant infrastructure, send
  email, collect payments, or issue app credentials.
- `CONTROL_PLANE_SIGNING_KEY`: At least 32 random characters used to sign
  administrator activation and device-enrollment capabilities. Keep it in
  Secret Manager in production.
- `CONTROL_PLANE_PUBLIC_URL`: HTTPS base URL used in activation and enrollment
  links (default `https://r2c-tracker.com`).
- `DEVICE_CREDENTIAL_ISSUANCE_ENABLED`: Enables one-time QR redemption into
  revocable, per-device tracker credentials.
- `CONTROL_PLANE_TRACKER_BASE_URL`: Tracker URL returned to enrolled apps.
- `SECRET_KEY`: Session-cookie signing key. It is required before organization
  login routes are enabled.
- `SESSION_COOKIE_HTTPS_ONLY`: Set to `true` for deployment. Live
  organization administration remains disabled unless session cookies are
  HTTPS-only; local simulation may leave it `false`.
- `DATABASE_URL`: URL for the postgres database - omit to use local SQLite instead (`sqlite+aiosqlite:///./test.db`).
- `FAA_NOTAM_CLIENT_ID`: FAA NMS OAuth client ID, used to create the tracker-side secret initially.
- `FAA_NOTAM_CLIENT_SECRET`: FAA NMS OAuth client secret, used to create the tracker-side secret initially.
- `FAA_NOTAM_API_BASE_URL`: Optional FAA NMS API override.
- `FAA_NOTAM_TOKEN_URL`: Optional FAA OAuth token URL override.
- `FAA_PROXY_CACHE_TTL_SEC`: Fresh cache lifetime for full geographic queries (default `90`).
- `FAA_PROXY_CACHE_MAX_ENTRIES`: Per-process geographic cache bound (default `512`).
- `FAA_PROXY_CACHE_MAX_BYTES`: Per-process cache memory bound (default `67108864`).
- `FAA_PROXY_CACHE_MAX_ITEM_BYTES`: Largest response eligible for caching (default `8388608`).
- `FAA_PROXY_CACHE_GRID_DEGREES`: Geographic cache cell size (default `0.002` degrees).
- `FAA_PROXY_MAX_CONCURRENT_UPSTREAM`: Concurrent FAA request bound (default `8`).
- define everything in .env file and pull into shell via .env prior to start.

## Features
- GeoJSON upload parsing w/overlap detection for data integrity.
- Weather stats during flight via archive-open-meteo
- Day/Night calculation via Suncalc
- Automated Leaderboard & recent flights.
- Basic Admin and CSV Export
- Optional RID2Caltopo multi-zone coordination hub on `/ws/r2c`
- Authenticated FAA NOTAM proxy on `/faa/notams`; FAA credentials remain server-side.
- Separate platform super-admin onboarding and billing control plane.
- Organization-owned administrator roles, privacy/retention policies, and
  signed, expiring, revocable drone-team enrollment QR campaigns.

## Organization administration

The super-admin route `/platform-admin/organizations` cannot query tenant
flight records or logs. Its sole authorized email and display name come from
the `latest` version of the `r2c-super-admin-identity` Secret Manager secret,
never an application environment variable. There is no refresh timer. A login
or privileged request reads Secret Manager only when the on-demand cache is at
least 30 seconds old; an idle tracker performs no identity reads. Changing the
identity invalidates the former administrator's sessions and password without
a restart.

Google OAuth uses the authorization-code flow with state, a one-time nonce,
PKCE, audience/signature verification, and verified-email comparison against
the current infrastructure identity. It stores no Google access or refresh
token. The fallback password email contains a single-use token that expires
after five minutes. Only its SHA-256 hash is stored. The token is carried in a
URL fragment so it is not sent in HTTP request URLs or Cloud Run access logs.
Existing passwords are stored as salted scrypt hashes, and the password forms
support browser password managers. When
the separate
control-plane database and signing key are configured, it can create
organization accounts in simulation mode and produce an administrator
activation link.

Organization administrators use `/<designator>/admin`. The first
owner may delegate billing, user, records, viewing, and video-request roles.
Enrollment QR codes contain only a signed organization/campaign locator.
They never embed FAA credentials, administrator passwords, or a long-lived
tracker upload secret. When device credential issuance is enabled, Android and
iOS exchange that locator once for a revocable per-device credential. The
server stores only the credential hash.

See [CONTROL_PLANE_SETUP.md](CONTROL_PLANE_SETUP.md) for local simulation,
BigQuery configuration, and the production safety checklist.

GCI maintainers rotate the authoritative identity with:

```bash
./set_super_admin.sh new-admin@example.org "Administrator Name"
```

Google client ID and secret values are mapped from the
`r2c-google-oauth-client-id` and `r2c-google-oauth-client-secret` secrets.
Hosted outbound mail uses the Gmail API with the send-only OAuth scope. The
offline credential is mapped from Secret Manager; R2C Tracker stores no Google
password and cannot read the mailbox. STARTTLS SMTP remains an optional fallback
for self-hosted installations.
Pending organization members can instead activate with a verified Google
account whose email exactly matches the pending membership record.

The hosted evaluation at `r2c-tracker.com` uses live organization onboarding:
activation invitations are delivered by the Gmail API, and an
organization's 30-day trial starts when its primary administrator activates the
account through a configured OAuth identity provider or creates an optional R2C
password. Password users can request a non-enumerating, single-use reset link;
OAuth users never need an R2C password. Billing remains shadow accounting; live
onboarding does not collect a payment or imply production service guarantees.

## FAA NOTAM Proxy

RID2Caltopo calls:

```text
GET /faa/notams?latitude=39.153&longitude=-121.133&radius=2
X-SAR-Token: <TRACKER_API_KEY>
```

The response body preserves the FAA GeoJSON response shape used by Android and
Apple. Full queries are cached briefly in small geographic cells. The upstream
radius is expanded to cover the entire cell so a nearby cache hit cannot omit a
notice at the requested-radius boundary. Requests containing `lastUpdatedDate`
bypass the cache.

The cache is deliberately local, short-lived, and bounded. It works equally for
California or nationwide users because entries are keyed by geographic cell;
it is not a single region-wide result. Proxy failures are returned as failures,
not stale HTTP 200 responses, so clients retain their last known results while
showing the lookup as unavailable.

See [FAA_PROXY_ROLLOUT.md](FAA_PROXY_ROLLOUT.md) for deployment order, verification,
field qualification, and legacy-client compatibility.

The isolated `r2c-tracker.com` pilot project, local named `gcloud`
configuration, Cloud SQL proxy workflow, and guarded pilot deploy command are
documented in [PILOT_SETUP.md](PILOT_SETUP.md).

## Coordination Docs
- [R2C protocol and robustness guide](/Users/kjt/Projects/r2c-tracker/R2C_PROTOCOL.md)
- [Google Cloud reproduction/setup guide](/Users/kjt/Projects/r2c-tracker/GCLOUD_SETUP.md)

## Flight Archive Recovery
For a managed organization such as `mySAR`:

1. Sign in through `/<designator>/admin` and open **Manage flight records**.
2. Download the organization's current full CSV and flight-log archive as a
   backup when applicable.
3. Ensure that organization has no flight records. Records belonging to other
   organizations and the legacy tracker do not need to be removed.
4. Run **Import from Flight Log Archive** using the saved
   `.tgz/.tar.gz/.tar` file from the legacy tracker.
5. Run **Backfill Weather and Metadata from CSV** using the saved full admin
   CSV from the legacy tracker.

The organization import assigns every rebuilt record to the authenticated
organization and stores each raw log below
`organizations/<designator>/<year>/<month>/`. Organization owners and members
with the `records_admin` role can use these tools.

For a legacy, non-organization tracker rebuild:

1. Download the full admin CSV export from `/export` as a backup and metadata source.
2. Download the flight log archive from `/flightlogs/archive`.
3. Empty the `flights` table and clear the `flightlogs-vol` bucket contents.
4. Deploy the current app version so the `archive_relpath` schema and archive import tools are available.
5. In `/admin`, run `Import from Flight Log Archive` using the saved `.tgz/.tar.gz/.tar` file.
6. In `/admin`, run `Backfill Weather and Metadata from CSV` using the saved full admin CSV.

Notes:
- Archive import rebuilds the database from GeoJSON flight logs and writes fresh archive files named with the new DB flight ID.
- CSV backfill restores weather and other DB-only fields when a rebuilt flight can be matched safely.
- Some historically deleted or mislabeled flights may remain unmatched during CSV backfill, which is expected.

## Tests
Run the coordination tests with:

`python3 -m unittest discover -s tests -p "test_*.py"`

These exercise owner selection, relayed sightings, and heartbeat/lease expiry
without requiring filesystem persistence.

The suite also covers deterministic tie-breaking, map isolation, lease refresh
behavior, and owner-release edge cases for the multi-zone coordination path.

For higher-confidence release checks, `tests/test_r2c_scenarios.py` simulates
multi-zone timelines with overlapping drone sightings, disconnect/expiry
handoffs, and deterministic ownership assertions.

## Release Verification
Run the local release gate before deploying:

`./release_check.sh`

The release check uses an isolated temporary SQLite database, runs the full
Python unit suite, starts a local tracker on `127.0.0.1:18080`, verifies `/`,
`/r2c`, and `/versions`, then performs an authenticated `/ws/r2c` hello smoke
test. Override `TRACKER_RELEASE_CHECK_PORT` if that port is already in use.

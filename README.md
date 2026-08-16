# R2C Tracker

R2C Tracker is the shared, multi-organization companion service for the
RID2Caltopo Android and Apple applications. It provides organization-scoped
flight records, device enrollment, multi-tablet Remote ID coordination, FAA
NOTAM proxying, and consent-controlled managed-video signaling.

RID2Caltopo and R2C Tracker are independent open-source projects. They are not
affiliated with or endorsed by CalTopo. RID2Caltopo uses the CalTopo Teams API,
and the project appreciates the CalTopo team's product and API support.

The hosted service is a best-effort public-safety pilot. It is provided as-is
and as-available without a committed service level or guarantees of accuracy,
availability, reliability, completeness, or fitness for a particular purpose.
Operators must independently verify safety-critical information and decisions.

## Current service model

One Cloud Run service supports multiple agencies. Each organization has a
permanent designator, such as `MYSAR`, and a tenant path rooted at
`/<designator>`.

Organization boundaries are enforced in several layers:

- Flight, account, enrollment, billing, audit, and managed-video queries are
  scoped by organization ID.
- Raw flight logs are stored below
  `organizations/<designator>/<year>/<month>/`.
- Android and Apple redeem a signed enrollment locator once for a revocable,
  per-device credential bound to one organization. Long-lived administrator,
  FAA, and unrestricted tracker secrets are not placed in enrollment QR codes.
- Organization-bound upload and coordination routes reject a credential issued
  for another designator.
- The platform administrator manages organization lifecycle and service
  metadata but has no route for reading tenant flight records or raw logs.
- The retired global `/admin` interface is disabled by default. It exists only
  for deliberately enabled legacy installations whose records have no
  organization ID.

The operational database stores flights and R2C coordination state. A separate
control-plane database stores organizations, members and roles, enrollment and
device state, service lifecycle, aggregate usage, billing ledger entries,
managed-video state, and security audit events. These databases must never use
the same URL.

## Organization privacy and lifecycle

Organization owners choose whether their flight dashboard is:

- `restricted`: accessible only to signed-in members with a records role; or
- `public`: listed in the public directory and viewable without signing in.

Restricted organizations are not disclosed in the public directory, but their
direct login route remains available. Archiving an organization reserves its
designator and disables its site and device access without deleting its data.
Restoration is a platform lifecycle action.

Organizations receive open-ended extended-beta access; there is no trial expiry
and the tracker accepts no payments. Each organization receives a platform-funded
$10.00 calendar-month usage allowance. The billing administrator, or the primary
organization administrator when no billing administrator is active, is notified
when projected usage exceeds the allowance and again when actual allocated usage
exceeds it. At $9.00, remote video streaming is disabled through month end while
flight logs and R2C-based drone-owner arbitration continue.

## Roles

The first administrator receives every organization role and can delegate a
smaller set to other active members.

| Role | Scope |
| --- | --- |
| `organization_owner` | Organization settings, member and enrollment administration, records, billing, and delegated ownership actions |
| `billing_admin` | Service status, balance, and optional funding actions |
| `config_admin` | Pull, review, approve, discard, and restore organization CalTopo credentials and drone-list releases |
| `user_admin` | Members, invitations, and enrollment campaigns |
| `records_admin` | View, export, import, delete, and restore that organization's flight records and logs |
| `records_viewer` | View a restricted organization dashboard |
| `video_requester` | View advertised streams and manage only the member's organization-scoped video requests |

See [the authorization matrix](docs/SECURITY_AUTHORIZATION_MATRIX.md) for the
route-by-route enforcement contract and negative-test expectations.

## Main interfaces

| Route | Purpose |
| --- | --- |
| `/` | Public directory containing only organizations that selected public records visibility |
| `/<designator>` | Public or authenticated organization flight dashboard, according to organization policy |
| `/<designator>/login` | Organization member sign-in |
| `/<designator>/admin` | Organization service status, settings, members, roles, and enrollments |
| `/<designator>/admin/flights` | Organization-scoped records export, import, deletion, archive, and restore tools |
| `/<designator>/streams` | Consent-controlled managed-video request interface |
| `/<designator>/upload` | Organization-bound device flight upload |
| `/<designator>/api/v1/organization-config/current` | Organization-bound device download of the approved configuration release |
| `/<designator>/ws/r2c` | Organization-bound coordination and managed-video signaling socket |
| `/api/v1/device-enrollment/redeem` | One-time app enrollment exchange |
| `/faa/notams` | Authenticated FAA NOTAM proxy; FAA credentials remain server-side |
| `/platform-admin/organizations` | Platform organization lifecycle and service administration |
| `/versions` | Deployed release history |
| `/livez`, `/readyz` | Process and database health checks |

Activation, login, recovery, Google OAuth, and enrollment landing routes are
public bootstrap surfaces but remain transaction-scoped. Browser mutations use
CSRF protection, and authorization tests inventory all organization and
platform route families.

## Capabilities

- Parse RID2Caltopo GeoJSON flight submissions with duplicate/overlap checks.
- Compute flight duration, distance, day/night classification, and bounded
  weather summaries.
- Render recent flights and pilot leaderboards within the selected tenant.
- Export/import CSV metadata and bounded `.tgz`, `.tar.gz`, or `.tar` flight-log
  archives without involving the platform administrator.
- Coordinate Remote ID ownership, confirmations, and multi-tablet handoff over
  authenticated WebSockets.
- Proxy FAA NOTAM GeoJSON through a bounded, short-lived geographic cache while
  keeping FAA credentials off mobile devices.
- Create signed, expiring, revocable organization enrollment campaigns and
  per-device app credentials.
- Support password and verified Google organization login, single-use password
  reset, and separately authenticated platform administration.
- Advertise organization streams and coordinate request, consent, preflight,
  signaling, stop, and aggregate-metrics state. Live/playback media uses TURN;
  approved downloads use a private, bounded temporary handoff that is deleted
  after successful browser delivery or expiry.
- Track aggregate attributed platform usage against each organization's
  platform-funded extended-beta allowance; payment and checkout routes are absent.

Managed video remains separately qualified by platform and network path. See
[the managed-video architecture](VIDEO_STREAMING_ARCHITECTURE.md) for the exact
implemented and field-qualification boundaries.

## Local verification

Create the project environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

The quickest complete local verification is:

```bash
./qualify_release.sh
```

It runs the unit suite once, then runs the runtime/migration and security gates
in parallel. `release_check.sh` remains available as the narrower runtime and
migration check, but it is not complete pre-publication qualification by itself.

Presentation-only test releases may continue to use the intentionally conspicuous
`./publish_release.sh --bypass-safety-checks APP_VERSION_CODE` workflow after
the source is reviewed, committed, and tagged. It still runs complete local
qualification and candidate/public health checks, but skips idle and hosted
staging/database safety checks. The command rejects non-UI changes and is not
for production qualification; see [the release runbook](RELEASE.md).

The runtime portion creates isolated temporary SQLite data, starts a localhost
server, checks public and health routes, verifies that the deployment gate and
FAA proxy reject unauthenticated requests, and exercises the R2C WebSocket
hello/heartbeat protocol. The parallel security portion checks dependencies,
static analysis, tracked-source secrets, and the generated SBOM.

For pilot-connected development, prepare the named Google Cloud configuration
and a private, ignored local environment file:

```bash
./setup_pilot_local.sh
set -a
. ./.env.pilot.local
set +a
.venv/bin/python -m uvicorn main:app \
  --host 127.0.0.1 --port "${TRACKER_PORT}"
```

This local process uses separate SQLite tracker and control-plane databases; it
does not reproduce Google Cloud networking, mounted Cloud Storage, PostgreSQL,
Secret Manager injection, or Cloud Run routing. Use synthetic organizations and
records locally. See [control-plane setup](CONTROL_PLANE_SETUP.md) for a generic
self-hosted simulation.

## Important runtime configuration

Production secrets are mapped from Secret Manager by the deployment scripts.
Do not commit `.env` files or place secret values in command arguments.

### Core and organization control

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Async SQLAlchemy URL for flight and coordination data; defaults to local SQLite |
| `CONTROL_PLANE_DATABASE_URL` | Different async database URL for organizations, roles, enrollment, lifecycle, billing, video, and audit state |
| `CONTROL_PLANE_MODE` | `simulation` or `live`; the hosted pilot requires `live` |
| `CONTROL_PLANE_SIGNING_KEY` | Signs activation and enrollment capabilities; use at least 32 random characters |
| `CONTROL_PLANE_PUBLIC_URL` | Public HTTPS origin used in links and redirects |
| `CONTROL_PLANE_TRACKER_BASE_URL` | Tracker base URL returned to enrolled devices |
| `SECRET_KEY` | Browser session-cookie signing key |
| `SESSION_COOKIE_HTTPS_ONLY` | Must be `true` for hosted organization administration |
| `DEVICE_CREDENTIAL_ISSUANCE_ENABLED` | Enables one-time redemption into revocable per-device credentials |
| `LEGACY_ADMIN_ENABLED` | Enables retired non-organization administration only when explicitly set; defaults to `false` |
| `DEPLOYMENT_GATE_KEY` | Dedicated bearer secret protecting `/deployment-readiness` |
| `MANAGED_REQUEST_INGEST_KEY` | Authenticates managed-access requests forwarded from the public website |

### Identity, email, billing, and video

- Google organization and platform login use `GOOGLE_OAUTH_CLIENT_ID` and
  `GOOGLE_OAUTH_CLIENT_SECRET`.
- Microsoft organization login uses `MICROSOFT_OIDC_CLIENT_ID` and
  `MICROSOFT_OIDC_CLIENT_SECRET`. `MICROSOFT_OIDC_TENANT` defaults to
  `organizations`; set it to a tenant UUID to restrict sign-in to one Entra
  tenant. Register `https://r2c-tracker.com/microsoft/callback` as a Web
  redirect URI. Microsoft identities are linked only through a current R2C
  member invitation; OIDC never creates memberships or assigns roles.
- Hosted send-only Gmail uses `PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN` and
  `PLATFORM_EMAIL_FROM`; STARTTLS SMTP variables remain an optional fallback.
- Live aggregate billing uses `PLATFORM_BILLING_SOURCE=bigquery`, an explicit
  project/dataset, and the mandatory `PLATFORM_BILLING_INCLUDED_PROJECTS`
  allowlist. It does not query tenant operational data.
- Payments are disabled during the extended beta; the runtime has no checkout or
  payment-webhook route and the deployment does not accept payment secrets.
- Direct/Routed preflight uses `VIDEO_ICE_SERVERS_JSON`. The pilot obtains
  short-lived, organization-tagged TURN credentials from
  `CLOUDFLARE_TURN_KEY_ID` and `CLOUDFLARE_TURN_API_TOKEN` when configured.

### FAA and bounded uploads

The FAA proxy reads `FAA_NOTAM_CLIENT_ID`, `FAA_NOTAM_CLIENT_SECRET`, optional
API/token URL overrides, request timeout, cache size/TTL/grid limits, and the
upstream concurrency bound. Archive restore limits are controlled by
`MAX_ARCHIVE_UPLOAD_BYTES`, `MAX_ARCHIVE_MEMBERS`,
`MAX_FLIGHT_LOG_MEMBERS`, `MAX_FLIGHT_LOG_BYTES`, and
`MAX_ARCHIVE_EXPANDED_BYTES`.

The full deployment mapping and guarded pilot defaults are authoritative in
[`deploy.sh`](deploy.sh) and [`deploy_pilot.sh`](deploy_pilot.sh).

## Organization records and migration

Organization owners and records administrators control their own records:

1. Sign in at `/<designator>/admin` and open **Manage flight records**.
2. Export the full organization CSV and flight-log archive.
3. Delete or retain records according to the organization's policy.
4. Restore raw logs with **Import from Flight Log Archive**.
5. Restore matching DB-only weather and metadata with
   **Backfill Weather and Metadata from CSV**.

Every query is filtered by organization ID, and restored logs remain in the
organization's namespaced storage path. The platform administrator does not
approve or perform routine organization export, deletion, or restoration.

Legacy non-organization recovery routes remain in the source for historical
installations but are disabled by default in the shared pilot.

## Tests and security gate

Useful focused commands are:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m unittest tests.test_r2c_scenarios
./scripts/security_checks.sh
```

The security gate runs authorization regression tests, dependency auditing,
medium/high static analysis, tracked-source secret scanning, and CycloneDX SBOM
generation. It does not replace independent adversarial review or physical
Android/Apple field qualification.

## Guarded pilot releases

Do not deploy the hosted pilot with `deploy.sh` or `deploy_pilot.sh` directly.
The supported workflow keeps production on the current revision while testing
a tagged candidate against the real Google Cloud dependencies:

```bash
./release_check.sh
# Commit and tag the exact source first.
./deploy_candidate.sh APP_VERSION_CODE
./test_candidate.sh
./promote_candidate.sh
```

`deploy_candidate.sh`:

1. refuses a dirty or untagged worktree;
2. reads the protected production activity gate and stops when coordination,
   dashboard, stream, or video-request activity is present;
3. deploys the candidate with zero production traffic;
4. tests liveness, both databases, mounted-storage write/read/delete, public
   routes, authorization rejection, and the coordination WebSocket against the
   candidate URL; and
5. records the candidate and previous revision locally under the ignored
   `.release-state/` directory.

`promote_candidate.sh` repeats the regression, waits for its synthetic
heartbeat to expire, checks production activity again, and atomically routes
100 percent to the candidate. If immediate post-promotion health or version
verification fails, it automatically restores the prior revision.

For a later operational problem, explicitly run:

```bash
./rollback_release.sh
```

The `--bootstrap` option was used only to install the deployment gate in
v1.4.25. It must not be used now that the live service exposes
`/deployment-readiness`.

Cloud Run remains limited to one instance because coordination and browser
connection state are process-local. Percentage traffic splitting is therefore
not approved. Candidate releases may start against the shared databases only
when the activity gate is idle, and startup migrations must remain compatible
with the prior revision. The local release gate rejects table/column drops and
renames; destructive changes require a separately reviewed
expand/migrate/contract maintenance plan.

See [the pilot environment guide](PILOT_SETUP.md) and
[platform continuity runbook](docs/PLATFORM_CONTINUITY_RUNBOOK.md).

## Additional documentation

- [Release runbook](RELEASE.md)
- [R2C coordination protocol](R2C_PROTOCOL.md)
- [FAA proxy rollout and compatibility](FAA_PROXY_ROLLOUT.md)
- [Managed-video architecture](VIDEO_STREAMING_ARCHITECTURE.md)
- [Control-plane setup](CONTROL_PLANE_SETUP.md)
- [Authorization matrix](docs/SECURITY_AUTHORIZATION_MATRIX.md)
- [Incident response](docs/INCIDENT_RESPONSE_RUNBOOK.md)
- [Security monitoring standard](docs/SECURITY_MONITORING_STANDARD.md)
- [Platform continuity](docs/PLATFORM_CONTINUITY_RUNBOOK.md)
- [Public-safety boundary review](docs/PUBLIC_SAFETY_BOUNDARY_REVIEW.md)
- [Security policy](SECURITY.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).

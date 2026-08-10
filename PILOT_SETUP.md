# R2C Tracker Pilot Environment

The pilot environment is isolated from `tracker.kjt.us`.

## Fixed identity

- Google Cloud project: `r2c-tracker-pilot`
- Google Cloud organization: `kjt.us`
- region: `us-west1`
- Cloud Run service: `r2c-tracker-pilot`
- runtime service account:
  `r2c-tracker-runtime@r2c-tracker-pilot.iam.gserviceaccount.com`
- PostgreSQL host: isolated databases on the existing free-tier `e2-micro` in
  `shaped-splicer-482602-v1`
- network path: Cloud Run Direct VPC egress through the dedicated
  `r2c-pilot-vpc` subnet, peered with the database VM's VPC
- Cloud Storage bucket: `r2c-tracker-pilot-flightlogs`

The project has a monthly USD 25 budget alert. A budget sends notifications; it
does not stop resources or cap spending.

## Provisioned data resources

The pilot uses separate `r2c_pilot_tracker` and
`r2c_pilot_control_plane` databases and a dedicated least-privilege role on the
existing PostgreSQL VM. The production databases used by `tracker.kjt.us` are
not shared with the pilot. Cloud Run reaches the VM's private address through
Direct VPC egress and bidirectional VPC peering. The isolated pilot proxy is
reachable only from `172.20.0.0/26` on TCP 5433.

Flight logs are stored in the private, uniform-access
`r2c-tracker-pilot-flightlogs` bucket. Cloud Run mounts that bucket at
`/flightlogs-vol`, matching the existing application filesystem contract.

## Secret names

Generated secrets:

- `r2c-tracker-database-url`
- `r2c-tracker-admin-password`
- `r2c-deployment-gate-key`
- `r2c-release-device-token` (dedicated credential for the `RELEASECHECK`
  organization; read by the release workstation, not mounted in Cloud Run)
- `r2c-tracker-secret-key`
- `r2c-super-admin-identity` (email and display name only; dynamically read)
- `r2c-control-plane-database-url`
- `r2c-control-plane-signing-key`
- `r2c-managed-request-ingest-key`
- `r2c-google-oauth-client-id`
- `r2c-google-oauth-client-secret`
- `r2c-platform-email-gmail-refresh-token`
- `r2c-cloudflare-turn-key-id`
- `r2c-cloudflare-turn-api-token`

FAA proxy secrets, created during the FAA credential migration:

- `r2c-faa-notam-client-id`
- `r2c-faa-notam-client-secret`

Secret values must not be committed, copied into documentation, or passed as
command-line arguments.

## Prepare the local workstation

Run:

```bash
./setup_pilot_local.sh
```

The script creates or updates a named `gcloud` configuration called
`r2c-tracker-pilot`. It does not replace the default configuration used for the
legacy tracker. It verifies the pilot database VM and bucket and writes generated
runtime values to the ignored, mode-0600 file `.env.pilot.local`.

Local development intentionally uses separate SQLite tracker and control-plane
files. The VM PostgreSQL port is private to the deployed pilot network and is
not exposed for workstation access.

In another shell, load the private environment and start the tracker:

```bash
set -a
. ./.env.pilot.local
set +a
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port "${TRACKER_PORT}"
```

## Deploy the pilot

Do not deploy until both FAA secrets, `r2c-super-admin-identity`, and the
dedicated `r2c-release-device-token` have an enabled version. The latter must
contain an active device credential enrolled to the `RELEASECHECK`
organization; the release guard uses it only against
`/releasecheck/ws/r2c`. Configure or rotate the administrator without
restarting the service:

```bash
./set_super_admin.sh kjtsar@kjt.us "R2C Platform Administrator"
```

Run the local release gate, commit and tag the exact source, then use the
guarded candidate workflow:

```bash
./release_check.sh
./deploy_candidate.sh R2C_RECOMMENDED_APP_VERSION_CODE
./test_candidate.sh
./promote_candidate.sh
```

The candidate command refuses a dirty or untagged worktree, blocks while the
live service reports operational activity, and deploys the new revision with
zero production traffic. Cloud regression checks both databases, the mounted
bucket, public routes, authorization rejection, and the R2C WebSocket before
promotion is allowed. Promotion repeats the checks and activity gate before an
atomic 100-percent traffic switch. Use `./rollback_release.sh` to restore the
recorded prior revision.

The underlying pilot wrapper pins the project, region, service, dedicated VPC
network and subnet, bucket, service account, and Secret Manager names. It
explicitly clears any Cloud SQL attachment and refuses to deploy if the project
or service name is changed away from the pilot identifiers. Do not invoke
`deploy_pilot.sh` or `deploy.sh` directly for a routine hosted release because
that bypasses the activity and candidate-regression workflow.

The pilot also defaults to the FAA staging API and token endpoints used by the
currently qualified RID2Caltopo configuration. Production FAA endpoints remain
an explicit later cutover after proxy qualification.

Cloud Run accepts unauthenticated HTTPS transport so Android and Apple clients
can reach it without Google identity tokens. Tracker upload, coordination, FAA
proxy, and admin protections remain enforced by the application.

The shared `deploy.sh` retains its legacy environment-variable path for the
existing tracker. Cloud SQL attachment, secret-backed application settings,
and the Flight Logs bucket mount are enabled only when their corresponding
pilot variables are supplied.

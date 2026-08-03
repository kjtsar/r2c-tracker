# R2C Tracker Pilot Environment

The pilot environment is isolated from `tracker.kjt.us`.

## Fixed identity

- Google Cloud project: `r2c-tracker-pilot`
- Google Cloud organization: `kjt.us`
- region: `us-west1`
- Cloud Run service: `r2c-tracker-pilot`
- runtime service account:
  `r2c-tracker-runtime@r2c-tracker-pilot.iam.gserviceaccount.com`
- Cloud SQL instance: `r2c-tracker-pilot:us-west1:r2c-pilot-pg`
- Cloud Storage bucket: `r2c-tracker-pilot-flightlogs`

The project has a monthly USD 25 budget alert. A budget sends notifications; it
does not stop resources or cap spending.

## Provisioned data resources

The pilot database is PostgreSQL 17 Enterprise on the shared-core
`db-f1-micro` tier. It is zonal, has 10 GB SSD storage, seven retained daily
backups, and deletion protection. This is a test configuration without a Cloud
SQL SLA, not the eventual production sizing.

Flight logs are stored in the private, uniform-access
`r2c-tracker-pilot-flightlogs` bucket. Cloud Run mounts that bucket at
`/flightlogs-vol`, matching the existing application filesystem contract.

## Secret names

Generated secrets:

- `r2c-tracker-database-url`
- `r2c-tracker-admin-password`
- `r2c-tracker-api-key`
- `r2c-tracker-secret-key`
- `r2c-super-admin-identity` (email and display name only; dynamically read)
- `r2c-google-oauth-client-id`
- `r2c-google-oauth-client-secret`

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
legacy tracker. It verifies the pilot database and bucket and writes generated
runtime values to the ignored, mode-0600 file `.env.pilot.local`.

For local access to the Cloud SQL database, install the Cloud SQL Auth Proxy and
run:

```bash
cloud-sql-proxy --port 5433 r2c-tracker-pilot:us-west1:r2c-pilot-pg
```

In another shell, load the private environment and start the tracker:

```bash
set -a
. ./.env.pilot.local
set +a
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port "${TRACKER_PORT}"
```

## Deploy the pilot

Do not deploy until both FAA secrets and `r2c-super-admin-identity` have an
enabled version. Configure or rotate the administrator without restarting the
service:

```bash
./set_super_admin.sh kjtsar@kjt.us "R2C Platform Administrator"
```

Then run:

```bash
./deploy_pilot.sh R2C_RECOMMENDED_APP_VERSION_CODE
```

The wrapper pins the project, region, service, database, bucket, service
account, and Secret Manager names. It refuses to deploy if the project or
service name is changed away from the pilot identifiers.

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

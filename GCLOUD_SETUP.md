# Reproducing `r2c-tracker` for Another Organization

This is a practical setup guide for standing up the current
`r2c-tracker` implementation for another SAR organization.

This repo's deployment flow is built around Google Cloud Run and a SQL database 
referenced by `DATABASE_URL`.

## What this deployment needs

- a Google Cloud project
- a signed-in `gcloud` account with permission to deploy Cloud Run services
- a SQL database reachable by SQLAlchemy via `DATABASE_URL`
- a shared tracker token for uploads and `/ws/r2c`
- an admin password for the tracker web UI

## Runtime architecture

The repo currently deploys:

- FastAPI app from this repo
- Cloud Run service named `r2c-tracker` by default
- Secret Manager secret for the session `SECRET_KEY`
- externally supplied env vars for `DATABASE_URL`, `TRACKER_ADMIN_PASS`, and
  `TRACKER_API_KEY`

`deploy.sh` handles the Cloud Run deployment and the Secret Manager bootstrap.

## 1. Create or choose a Google Cloud project

From a workstation with the Google Cloud SDK installed:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

Confirm the active account and project:

```bash
gcloud auth list
gcloud config get-value project
```

## 2. Provision a database

The app reads `DATABASE_URL` from the environment. In practice that means one of
these:

- Cloud SQL Postgres
- self-managed Postgres reachable from Cloud Run
- local SQLite for development only

Recommended production shape:

- create a Postgres database
- create an application user with normal DML privileges
- percent-encode any reserved password characters before placing them in
  `DATABASE_URL`

Example form:

```text
postgresql+asyncpg://tracker_user:ENCODED_PASSWORD@HOSTNAME:5432/tracker_db
```

The deploy script validates that the URL has a scheme, host, port, and database
name before deploying.

## 3. Choose organization secrets

You need three values before deployment:

```bash
export DATABASE_URL='postgresql+asyncpg://...'
export TRACKER_ADMIN_PASS='choose-a-strong-admin-password'
export TRACKER_API_KEY='choose-a-shared-r2c-token'
```

What they do:

- `DATABASE_URL`: application database connection
- `TRACKER_ADMIN_PASS`: password for `/admin`
- `TRACKER_API_KEY`: required for uploads and `/ws/r2c` via `X-SAR-Token`

Optional but useful:

```bash
export REGION='us-west1'
export SERVICE_NAME='r2c-tracker'
export SECRET_KEY_SECRET_NAME='r2c-tracker-secret-key'
```

## 4. Deploy the service

Run:

```bash
./deploy.sh
```

What the script does:

- verifies required env vars exist
- validates `DATABASE_URL`
- determines the active project and account from `gcloud`
- creates or reuses the Secret Manager secret for `SECRET_KEY`
- grants the Cloud Run runtime service account access to that secret
- deploys the current repo to Cloud Run with the chosen env vars

## 5. Point RID2Caltopo zones at the tracker

Each RID2Caltopo instance should be configured with:

- the tracker base URL
- the shared tracker token
- a stable `zoneId`
- a stable `guid`
- the target `mapId`

The coordination websocket endpoint is:

```text
wss://YOUR_TRACKER_HOST/ws/r2c
```

Required header:

```http
X-SAR-Token: <TRACKER_API_KEY>
```

## 6. Verify a fresh org deployment

Before broad field use, validate these behaviors with two or more test zones:

1. both zones connect and receive `hello_ack`
2. each zone appears in a `zone_update`
3. one zone claims first sighting for a test `remoteId`
4. the tracker broadcasts `owner_assigned`
5. the non-owner zone relays sightings and only the owner processes them
6. stopping owner heartbeats eventually emits `owner_expired`

This validation is more important than a generic smoke test because it exercises
the exact multi-zone path you care about operationally.

## 7. Minimum organization runbook

For another team to reproduce this setup cleanly, give them:

- the repo revision or release tag
- the exact Cloud Run service URL
- their own `TRACKER_API_KEY`
- naming rules for `zoneId` and `guid`
- the target Caltopo map IDs they are allowed to coordinate on
- a short operator note that the tracker assigns only one owner per
  `mapId + remoteId`

## Recommended conventions for other organizations

These help keep multi-zone behavior predictable:

- keep `guid` stable across reconnects for a given physical zone
- make `zoneId` human-readable, like `base-north` or `command-post`
- use one shared tracker token per organization or incident, not per operator
- do not let two different organizations share the same tracker deployment
  unless you also want them sharing the same coordination namespace

## Local development option

For local testing:

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

If `DATABASE_URL` is omitted, the app defaults to local SQLite in `test.db`.
That is fine for development and unit tests, but not a production substitute for
the shared coordination service.

# R2C Tracker release runbook

This runbook is the authoritative procedure for releasing the hosted
multi-organization R2C Tracker pilot. The release owner should copy the final
checklist into the release issue and record links to the pull request, tag,
candidate revision, validation evidence, production revision, and any follow-up
work.

The hosted service is best effort and currently runs one Cloud Run instance.
Every release uses a zero-production-traffic candidate and the operational
activity gate. Do not use percentage traffic splitting.

## Release roles and access

One person is the **release owner** from version preparation through promotion
or rollback. Other developers may review, build, and test, but only one person
should run the stateful candidate/promotion sequence.

The release owner needs:

- push and tag permission for this repository;
- the `r2c-tracker-pilot` Google Cloud configuration and permission to deploy
  the `r2c-tracker-pilot` service in `us-west1`;
- read access to the deployment-gate secret; and
- a working project virtual environment installed from `requirements.lock`.

Staging preparation requires Cloud SQL administration in `r2c-tracker-pilot`,
IAP tunnel access to the pilot PostgreSQL VM, and local `pg_dump`, `pg_restore`,
and `cloud-sql-proxy` commands. Review the fixed instance, database, role,
service-account, secret, firewall-rule, and bucket names before releasing.

Use individual Google Cloud identities and repository accounts. Do not share
credentials or copy Secret Manager values into tickets, chat, shell history, or
the repository.

## 1. Prepare the release

1. Merge only reviewed changes intended for this release.
2. Add a new top section to `changes.txt` using `vMAJOR.MINOR.PATCH:` followed
   by concise, operator-visible bullets. Do not add `deployed` yet.
3. Confirm that migrations are additive and compatible with both the current
   and candidate revision. Table or column removal/rename requires a separately
   reviewed expand/migrate/contract maintenance plan.
4. Identify the minimum RID2Caltopo Android `versionCode` compatible with this
   tracker release. This positive integer is passed to the deployment command
   and advertised by the service.
5. Review the complete diff and untracked-file list. A release must not include
   unrelated local work or secret material.

```bash
git status --short
git diff --check
git diff --stat
```

## 2. Run the local release gate

From the repository root:

```bash
.venv/bin/python -m pip install -r requirements.lock
./release_check.sh
./scripts/security_checks.sh
```

`release_check.sh` creates an isolated test organization and device credential,
runs the unit suite, isolated local HTTP checks, protected route checks, the
organization-scoped R2C WebSocket smoke test, and migration compatibility checks.
The security script adds authorization, dependency, static-analysis,
secret-scanning, and SBOM checks.

Investigate every failure. A rerun is acceptable only when the reason is
understood and recorded; a passing rerun does not erase an unexplained failure.

## 3. Review, commit, and tag the exact source

Obtain pull-request approval before tagging. After merge, update the local main
branch and rerun the gate if the commit differs from the reviewed candidate.
The final worktree must be clean.

Create an annotated tag on the exact release commit:

```bash
R2C_TRACKER_RELEASE=v1.4.26
git status --short
git tag -a "${R2C_TRACKER_RELEASE}" -m "R2C Tracker ${R2C_TRACKER_RELEASE}"
git push origin main
git push origin "${R2C_TRACKER_RELEASE}"
git describe --exact-match --tags HEAD
```

Choose the next version rather than copying the example literally. The existing
`tag_release.sh` helper can commit and tag, but it stages every tracked and
untracked file. Use it only from an intentionally clean, fully reviewed
worktree.

## 4. Test the immutable image in staging and deploy a production candidate

Authenticate the named Google Cloud configuration, then run:

```bash
R2C_MINIMUM_ANDROID_BUILD=126
./deploy_candidate.sh "${R2C_MINIMUM_ANDROID_BUILD}"
./test_candidate.sh
```

Replace the example with the minimum compatible Android build selected during
release preparation.

`deploy_candidate.sh` refuses a dirty or untagged checkout and stops when the
live activity gate reports operational use. It then:

1. creates an ephemeral PostgreSQL 15 Cloud SQL staging instance and refreshes
   `r2c_stage_tracker` and `r2c_stage_control_plane` from production database
   dumps transferred through authenticated IAP and Cloud SQL proxies;
2. deploys the tagged source to the IAM-protected `r2c-tracker-staging` service
   with staging-only roles, secrets, and Cloud Storage;
3. creates a one-use `RELEASECHECK` fixture only inside the cloned control-plane
   database and runs health, database, storage, authorization, and authenticated
   organization-scoped WebSocket regression;
4. resolves the tested staging revision to its immutable Artifact Registry
   `sha256` digest; and
5. deploys that exact digest as a zero-traffic production candidate, verifies
   the digest match, and runs non-mutating production readiness checks.

Production data is never used to create a test credential, staging is not
publicly invokable, outbound email and payment integrations are disabled, and
production flight-log objects are not copied into the staging bucket.

The command records the candidate and previous production revision in the
ignored `.release-state/pilot.json`. That file contains no application secret,
but it is required by promotion and rollback. Keep the release on the same
workstation. If ownership must change, transfer that file through an approved
private channel and have the new owner inspect it before proceeding.

After automation passes, inspect the candidate URL printed by the command and
perform focused browser checks for every changed user workflow. Use only
synthetic data and do not send real organization invitations or payment events.

Do not use `--bootstrap`; it was only for the first deployment-gate release.
Do not invoke `deploy.sh` or `deploy_pilot.sh` directly for a routine hosted
release.

## 5. Promote and verify production

Choose an operationally quiet period. Immediately before promotion:

```bash
./test_candidate.sh
./promote_candidate.sh
```

Promotion repeats cloud regression and the live activity check, then atomically
routes 100 percent of production traffic to the candidate. Failed immediate
health or version verification automatically restores the prior revision.

After promotion, verify:

- `/livez`, `/readyz`, and `/versions` on the public service;
- one public or restricted organization route appropriate for the test;
- the changed user workflows; and
- Cloud Run error logs and basic latency for the new revision.

Do not create, modify, or delete tenant records merely to prove a release.
Organization administrators own their record lifecycle.

## 6. Close the release

1. Change the release heading in `changes.txt` to
   `vMAJOR.MINOR.PATCH: deployed`.
2. Confirm the public `/versions` page renders the complete intended bullets.
3. Commit and push the deployment-evidence update.
4. Record the production revision and verification evidence in the release
   issue.
5. Monitor the new revision during the agreed observation window.
6. Run `./cleanup_pilot_staging.sh` after the observation window, and within 24
   hours of cloning, to delete the staging service and ephemeral Cloud SQL
   instance.

## Rollback and recovery

For a post-promotion application problem, run from the same release checkout:

```bash
./rollback_release.sh
```

Verify `/livez`, `/readyz`, the restored version, and the affected workflow.
Then open an incident record. Rollback changes Cloud Run traffic only: it does
not reverse database migrations, storage writes, email, billing events, or
other external side effects. If backward compatibility is uncertain, stop and
use the incident and continuity runbooks rather than improvising a redeploy.

## Release checklist

- [ ] Release owner and reviewers named
- [ ] Intended changes and release notes reviewed
- [ ] Compatible RID2Caltopo minimum `versionCode` selected
- [ ] Worktree contents and secret hygiene reviewed
- [ ] Local release and security gates passed
- [ ] Pull request approved and exact commit tagged/pushed
- [ ] Production activity gate idle
- [ ] Staging database clones refreshed and authenticated regression passed
- [ ] Production candidate digest matches the tested staging image digest
- [ ] Zero-traffic candidate regression passed
- [ ] Changed workflows checked at the candidate URL
- [ ] Candidate retested immediately before promotion
- [ ] Production health, version, workflows, and logs verified
- [ ] `/versions` and `changes.txt` marked and verified
- [ ] Evidence recorded and observation window completed
- [ ] Staging service and ephemeral Cloud SQL instance removed within 24 hours

Related documentation: [pilot setup](PILOT_SETUP.md),
[incident response](docs/INCIDENT_RESPONSE_RUNBOOK.md), and
[platform continuity](docs/PLATFORM_CONTINUITY_RUNBOOK.md).

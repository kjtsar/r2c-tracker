# r2c-tracker Platform Continuity Runbook

Status: reconstruction specification; recovery test not yet completed

Organization administrators remain responsible for their own flight-record export, deletion, and restoration. This procedure is for reconstructing the hosted platform and does not insert the site administrator into that workflow.

## Recovery objectives to approve

The Board must approve recovery-time and recovery-point objectives after a measured exercise. Until then, recovery is best effort with no guaranteed restoration time or maximum data-loss interval.

## Assets required for reconstruction

- Apache-2.0 source repository, reviewed release/tag, dependency lock, SBOM, test results, container digest, and deployment command.
- Google Cloud project/billing ownership, Cloud Run service configuration, service accounts/IAM, VPC connector/subnet/firewall definitions, Artifact Registry, DNS/domain control, and monitoring.
- Database schema and platform-level backup containing organization lifecycle, members/roles, enrollment and device revocation state, audit history, and tenant records retained under approved policy.
- Secret Manager references and separately held recovery access for application signing/session keys, database credentials, OAuth, email, FAA, TURN, Stripe if used, and platform-admin identity.
- Android and Apple store/signing ownership, website hosting/domain ownership, and release records for the RID2Caltopo companion apps.

Never place secret values in this repository or the recovery checklist. Record secret names, custodians, rotation dates, and recovery method in the Board-controlled asset register.

## Reconstruction sequence

1. Declare the recovery event, preserve the failed environment, and select a known-good source revision and image digest.
2. Establish a clean project or isolated recovery environment under an NCSSAR-controlled owner and billing account.
3. Recreate least-privilege runtime, deployment, monitoring, and recovery identities; require separate MFA-protected administrators.
4. Recreate the private database network path. Do not expose PostgreSQL or administrative access broadly to the internet.
5. Restore the database into isolation, validate schema/integrity and organization counts, and rotate all credentials reachable from the failed environment.
6. Deploy the pinned container, reference secrets by version/name, and run migrations only after a backup and rollback point exist.
7. Run the full security gate and adversarial organization-boundary tests. Validate archived organizations, revoked devices, enrollment redemption, exports/restores, FAA degraded state, and managed-video authorization.
8. Restore traffic gradually; verify runtime revision, health, logs, alerts, and public versions/release notes.
9. Have organization administrators verify their own records and capabilities. Record missing or stale data without silently reconstructing it.
10. Close only after the incident commander and Board witness record elapsed time, backup age, unrecovered data, manual steps, and corrective actions.

## Backup separation and validation

- A principal able to destroy the live environment must not be the only principal able to recover backups.
- Encrypt backups, restrict access, log reads/restores/deletes, and keep at least one recovery copy outside the live deployment's failure boundary.
- Test restoration into isolation; a successful backup job is not proof of recoverability.
- Never restore over the live database as the first test.
- Organization self-service exports are valuable records copies but are not a complete platform backup.

## Required exercise evidence

A backup administrator, without the primary developer performing the steps, must reconstruct a non-production environment. Retain the start/end times, source revision and image digest, backup timestamp, commands/configuration references, test results, alert delivery proof, unrecovered data, and all undocumented dependencies discovered.

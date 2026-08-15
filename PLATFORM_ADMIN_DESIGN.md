# R2C Tracker Platform Control Plane

## Purpose

The platform control plane manages organization provisioning, aggregate usage,
Google Cloud cost allocation, extended-beta allowances, organizations, and platform
auditing. It is separate from every organization's operational tracker data.

The website super-admin must not have an application-level path to:

- flight records or incident metadata;
- flight-log files or archive contents;
- aircraft identifiers or map identifiers;
- organization user activity;
- live operational status or video content.

Only delayed, aggregate resource usage needed for billing enters the control
plane.

## Service boundary

The production design uses separate credentials and a separate database:

- `CONTROL_PLANE_DATABASE_URL` stores commercial and provisioning data.
- Tenant databases store operational records.
- Tenant object storage holds flight logs and archives.
- Platform-admin authentication cannot be used on tenant admin endpoints.
- Organization roles cannot be used on platform-admin endpoints.

Infrastructure operators may still have cloud-level emergency access. That
access must be handled through Google Cloud IAM, audited independently, and is
not exposed through the R2C website.

## Super-admin identity

The sole authorized super-admin identity is the `latest` version of the
`r2c-super-admin-identity` Secret Manager secret. It contains an email address
and display name, not a password. The application reads it through its runtime
service account only when a login or privileged request needs it, and caches a
successful read for no more than 30 seconds. There is no timer or background
polling, so an idle service consumes no Secret Manager reads.

Creating a new secret version changes the administrator without a Cloud Run
restart. Every privileged request compares its session with the current secret
version. A changed version invalidates existing sessions; a changed email also
disables the former database account and creates an uninitialized replacement.
Passwords are never transferred or restored when a former address is selected
again. Secret read or validation failures deny platform administration while
leaving tracker operations available.

Google authentication uses a server-side authorization-code exchange with
state, PKCE, a one-time nonce, signed ID-token verification, and an exact
verified-email match to the infrastructure identity. The Google `sub` claim is
retained in the signed administrator session; no Google access or refresh token
is stored.

The non-Google fallback sends an intentionally generic response, rate-limits
delivery, and issues a single-use password token valid for five minutes. Only a
SHA-256 token hash is stored. The raw token is placed in the email URL fragment
and moved by same-origin JavaScript into the HTTPS form body, preventing it
from appearing in Cloud Run access logs or browser history after the page
loads. A secret-version rotation makes all links issued for the former
identity generation unusable.

## Control-plane entities

### organizations

- immutable organization ID;
- official legal/display name;
- normalized, unique designator;
- assigned tenant path;
- lifecycle state;
- provisioning state;
- open-ended extended-beta lifecycle state;
- tenant database/storage resource references, but no tenant credentials;
- retention-policy identifiers.

### organization_contacts

- organization ID;
- contact role: primary administrator, billing administrator, or postal contact;
- name;
- email;
- postal address;
- notification preferences.

The first contact may hold all roles, but each role can later be delegated.

### extended_beta_allowances

- organization ID;
- calendar billing month;
- $10.00 platform-funded allowance;
- allocated actual and projected cost;
- billing-data cutoff;
- month-end boundary and video-disabled timestamp.

The platform has no payment credentials, checkout route, or payment webhook.

### usage_daily

Delayed daily aggregates only:

- compute allocation units and cost;
- network ingress/egress bytes and cost;
- storage byte-days and cost;
- database allocation units and cost;
- FAA proxy requests and allocated cost;
- TURN/video relay bytes and allocated cost;
- other directly attributable cost.

No incident, flight, aircraft, user, map, or file identifiers are allowed.

### billing_ledger

The historical append-only ledger remains available for audit compatibility,
but the extended beta has no payment, checkout, webhook, credit, trial, or grace
workflow. Organizations receive open-ended access and a platform-funded $10.00
calendar-month usage allowance.

The allowance worker reconciles current, non-stale Google Cloud billing export
data to privacy-safe organization allocation weights. It sends a one-time monthly
notice when projected usage exceeds $10.00 and another when actual allocated
usage exceeds $10.00. At $9.00 actual allocated usage, it terminates active remote
video requests, rejects new streaming requests through month end, and sends a
separate notice. Flight logs, recording downloads, and R2C-based drone-owner
arbitration remain enabled. Notices go to the first active billing administrator,
falling back to the primary organization administrator.

Archiving is a platform-administrator action only. Before the archive route can
disable a tenant, the platform administrator must affirm direct contact with
the organization administrator and record when and how contact occurred. That
contact record is retained in the control-plane audit event.

### provisioning_jobs

Each organization onboarding request records independently retryable steps:

1. reserve designator and tenant path;
2. create tenant database boundary;
3. create tenant object-storage boundary and retention rules;
4. create tenant secrets;
5. configure tenant path routing;
6. run health checks;
7. issue a short-lived activation link;
8. start open-ended extended-beta access when the administrator activates the account.

### managed_access_requests

The public managed-access form requires an affirmative acknowledgement that
RID2Caltopo and R2C Tracker provide supplemental situational awareness, may be
unavailable or incomplete, and are not the sole source for navigation, flight
safety, communications, or incident-command decisions. The website worker and
control-plane intake endpoint both enforce the current acknowledgement version;
the request stores its version and acknowledgement timestamp for review.

### control_plane_audit_events

Records commercial and provisioning changes without tenant content. Sensitive
values such as activation tokens and credentials are never written to the
audit trail. The platform-administrator organizations page shows only a small
30-day administrative summary. The dedicated audit view uses server-side
pagination and date, organization, category, actor, and exact-event filters;
views, exports, and retention-hold changes are themselves audited.

## Organization roles

Tenant authorization is administered by the organization:

- `organization_owner`;
- `billing_admin`;
- `user_admin`;
- `records_admin`;
- `records_viewer`;
- `video_requester`.

`video_requester` permits command staff to request a stream. It never bypasses
the pilot's approval.

## Default retention proposal

- Flight-record summaries: two years.
- Raw flight logs: thirty days.
- Pinned or archived logs: retained longer and billed as storage.
- Organization-controlled export before scheduled deletion.
- Extended-beta access has no expiry. Allowance notices explain that flight logs
  and R2C-based owner arbitration continue if remote video is disabled.
- Optional record-specific incident/legal hold.
- Backups are disaster recovery, not permanent customer archives.

Application audit events use a separate operational schedule:

- Retain events for 365 days from the event timestamp.
- Default searches to the newest 90 days and allow filtered review across the
  retained period.
- Delete expired, unheld events through the daily retention job.
- Preserve event-specific incident, investigation, grant/contract, or legal
  holds until an authorized platform administrator explicitly releases them.
- Keep accounting source records and the billing ledger on their separately
  approved financial records schedule rather than extending all audit events.
- Review the audit schedule annually and when governing requirements change.

## Delivery sequence

1. Review the read-only platform-admin prototype.
2. Enable Google Cloud detailed billing export and begin collecting history.
3. Add application-level aggregate usage meters.
4. Create the independent control-plane database and immutable billing ledger.
5. Implement organization onboarding in simulation mode.
6. Connect provisioning steps to Google Cloud resources.
7. Reconcile the $10.00 monthly allowance from current billing exports.
8. Enforce and notify the video-only $9.00 cutoff.

## Billing export integration

The dashboard supports an opt-in, read-only BigQuery source. Live mode requires
an explicit allowlist of R2C Google Cloud project IDs; it never defaults to all
projects on the billing account. Until Google's first standard or detailed
export table arrives, the dashboard reports an export-pending state with zero
values rather than presenting illustrative costs as live.

The live dashboard and allowance worker allocate costs. Compute, network (including
TURN relay), storage, and database costs are distributed in proportion to their
matching privacy-safe organization meters. A category with no measured usage,
and the billing export's unclassified `other` cost, is divided equally among
fully provisioned, non-archived organizations. Pending applicants and archived
organizations do not share platform costs. Sub-micro-dollar rounding is
reconciled deterministically so attributed plus unallocated cost always matches
the Google bill. Allocations are not written to the billing ledger and are not
charges; they measure consumption of the platform-funded monthly allowance.

## Device enrollment QR boundary

Organization owners and user administrators can create enrollment campaigns
with an expiration time, maximum redemption count, and explicit revocation
state. The rendered QR contains an HTTPS enrollment URL with a signed campaign
locator. The capability is bound to the organization and campaign and is
checked against current database state.

The QR does not contain FAA credentials, user passwords, or a tracker
credential. When issuance is enabled, the app redeems the signed locator once
over HTTPS. The server consumes one campaign use and returns a revocable,
expiring device credential; only its SHA-256 hash is stored. This pilot
credential accesses the shared tracker. True tenant isolation still requires
the later tenant provisioning work.

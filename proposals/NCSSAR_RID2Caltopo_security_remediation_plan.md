# RID2Caltopo Program Security Remediation Plan

Prepared for the Nevada County Sheriff's Search and Rescue Board technical team
Updated: August 8, 2026
Scope: RID2Caltopo Android and Apple apps, public websites, and the companion r2c-tracker service

## Objective

Reduce risks that could expose one organization to another, compromise privileged access, cause unrecoverable platform loss, or encourage unsafe operational reliance. Work is ordered by consequence and exposure, not by convenience.

The current service remains a controlled, best-effort pilot. This plan does not create a service-level agreement or a guarantee of suitability, reliability, availability, accuracy, or completeness.

## Progress update — August 8, 2026

| Workstream | Status | Evidence and remaining work |
|---|---|---|
| Database-host exposure | Partially remediated | Live controls now include deletion protection, removal of unused HTTP/HTTPS tags, a logged public-RDP deny, and an additive IAP SSH path. PostgreSQL 5433 remains subnet-limited. Public SSH remains temporarily until a brief restart repairs guest key/OS Login handling and proves IAP access; public-IP removal, Secure Boot evaluation, backup/restore evidence, and alerting remain open. |
| Tenant authorization | Implemented and tested; independent review open | A documented authorization matrix and executable route inventory cover organization/platform routes and state-changing interfaces. R2C coordination now derives organization scope from authenticated device credentials and applies it to runtime groups, broadcasts, standalone matching, ownership, confirmation replay, recent sightings, persistence, cleanup, and legacy public snapshots. Additive migration and same-map/same-Remote-ID cross-tenant regression tests are included. The public directory lists only organizations explicitly marked public; restricted organizations remain available only through their direct sign-in URLs and authenticated access. Archive restore limits upload size, total entries, flight-log count, individual log size, and expanded bytes. Focused independent adversarial review and additional replay/race/resource-exhaustion testing remain required. |
| Server and browser-edge assurance | Implemented and live-verified; independent review open | The retired global administrator is disabled by default, missing tracker API credentials fail closed, browser CORS is origin/method/header restricted, production HSTS is enabled, outbound weather requests are bounded, and billing table identifiers are validated. Live verification confirmed HSTS and rejection of an untrusted CORS preflight without an allow-origin header. Independent edge/header review and rate-limit/load testing remain open. |
| Server software assurance | Implemented; cross-platform expansion open | The container installs an exact dependency lock and the repository now carries an Apache 2.0 license. CI runs the full tests, dependency audit, medium/high static analysis, tracked-source secret-baseline enforcement, and CycloneDX SBOM generation; Dependabot is configured. The latest audit reported no known dependency vulnerabilities. Hash pinning, container/history/license-policy scanning, protected release governance, and Android/Apple/website/bundled-media SBOMs remain open. |
| Incident response and continuity | Drafted; institutional approval/exercises open | Interim `SECURITY.md`, incident-response, monitoring, platform-continuity, cloud-hardening, authorization, and public-safety review documents now exist. Board approval, NCSSAR-controlled contacts, two tested alert destinations, backup separation, tabletop, and independent reconstruction exercise remain open. |
| Release continuity | Implemented and live-verified; rollback exercise open | A protected activity gate, zero-production-traffic candidate URL, cloud-backed HTTP/database/storage/WebSocket regression, atomic promotion, explicit rollback, Cloud Run process probes, and rollback-compatibility migration check are implemented. Tracker v1.4.25 passed the first guarded Cloud Run candidate exercise and was promoted only after a second idle check. A witnessed rollback exercise and independent review remain open. HA Cloud SQL and multi-instance traffic splitting are intentionally deferred to avoid material pilot cost and because coordination state remains process-local. |
| Public-safety acknowledgments | Published and source/test verified; field review open | Android and Apple build 125 source is tagged as 2.0.3, and managed-access acknowledgment tests passed. Both public RID2Caltopo domains render the versioned best-effort/as-is/as-available and independent-verification language. Public examples use `mySAR`; the apps, website, and tracker state that RID2Caltopo is independent, is not affiliated with or endorsed by CalTopo, and uses the CalTopo Teams API. Store-distributed physical-device review, degraded-state exercises, Board training language, and managed-video physical qualification remain open. |
| External product name and API relationship | Disclosure published; direct review open | Public surfaces now disclose independence from CalTopo, use of its Teams API, and appreciation for the CalTopo product and API support. The developer plans to brief CalTopo founder Matt Jacobs and request direct review. Written confirmation of acceptable naming, API use, and any preferred attribution remains open and should be retained with the program records. |

The current tracker release source is commit `cb1655c`, tagged v1.4.25, with RID2Caltopo Android and Apple build 125 still recommended. Zero-traffic candidate revision `r2c-tracker-pilot-00100-bav` passed cloud database, mounted-storage, public-route, authorization-rejection, and coordination-WebSocket checks before receiving production traffic. After a second idle check, it was promoted atomically to 100 percent. Live `/livez` and `/readyz` reported v1.4.25 with both databases ready, the protected activity gate reported no active coordination, dashboard, stream, or video-request activity, and the revision had no Error-severity Cloud Run log entries during final verification. The prior production revision remains recorded as the explicit rollback target; a witnessed rollback exercise is still open. RID2Caltopo 2.0.3 build 125 and the website publication remain checkpointed as previously reported at website commit `de3a473`, Cloudflare Worker version `8c95a50d-9941-4c06-920c-d9837639df52`, and Sites version 16.

## Accepted organization records model

Routine organization records management is not a platform-administrator function.

- An `organization_owner` or `records_admin` can export the organization's full flight CSV and flight-log archive.
- The same roles can delete only that organization's flight records and restore from the organization's CSV or archive.
- Current automated tests exercise CSRF protection, organization scoping, namespaced archive storage, and rejection of cross-organization record/log access.
- Each organization determines its own export frequency, retention, deletion, and restoration practices.
- The platform administrator does not approve, perform, or mediate routine organization export, deletion, or restoration.

This closes organization-level flight-record recovery as a high-risk remediation item. A separate, lower-priority platform-continuity task remains because an organization export does not reconstruct the hosted service, organization accounts and roles, enrollment/revocation state, audit history, or the database host itself.

## Priority 0 — Immediate exposure reduction

Complete these items before onboarding another external organization.

### P0-1 Harden the database host

Status: **Partially complete.** No-outage controls are live. A maintenance-window restart and independent host review remain.

Risk addressed: public administrative exposure and avoidable host compromise.

Actions:

1. Confirm which services on the database VM are actually required.
2. Remove the public IP if operationally practical; otherwise restrict all administrative ingress to an approved path such as Identity-Aware Proxy or a narrowly controlled administrator source.
3. Remove the `0.0.0.0/0` RDP rule and restrict SSH. Remove the VM's public HTTP/HTTPS tags unless a documented service requires them.
4. Preserve the existing PostgreSQL rule that limits TCP 5433 to the Cloud Run subnet; verify PostgreSQL is not listening on an unintended public interface or port.
5. Enable deletion protection. Evaluate and enable Secure Boot if the installed operating system and agents are compatible.
6. Record OS version, patch state, running services, local accounts, SSH/OS Login configuration, disk encryption, and effective firewall rules.
7. Alert on unexpected firewall, public-IP, privileged-account, and VM-state changes.

Exit evidence:

- Sanitized VM, firewall, route, and listening-port exports reviewed by a second person.
- No unnecessary public service remains reachable.
- A documented administrative access path works without broad public ingress.

Owner: primary developer plus independent cloud reviewer.
Target: within 7 days.

### P0-2 Remove single-person control of critical accounts

Status: **Open; Board action required.** Technical documentation cannot replace organizational ownership, billing, recovery identities, or independent custody.

Risk addressed: loss, compromise, incapacity, or departure of the primary developer.

Actions:

1. Inventory Google Cloud projects and billing, repositories, domains, app stores/signing, OAuth, FAA access, email sender, TURN, monitoring, and recovery methods.
2. Assign an NCSSAR-controlled owner and at least two separately held recovery identities for critical services.
3. Require phishing-resistant MFA where supported and eliminate shared administrator credentials.
4. Grant least privilege to deployment, runtime, billing, security review, and recovery roles.
5. Preserve the developer's technical-administrator role without making the developer the sole owner, recovery holder, or personal financial guarantor.

Exit evidence:

- Board-designated custodian signs the account inventory.
- A backup administrator can authenticate, view billing, inspect deployment state, and reach recovery procedures without the primary developer.

Owner: Board officer plus technical lead.
Target: begin immediately; complete before ownership transfer or paid service.

### P0-3 Preserve the public-safety boundary

Status: **Source and automated acknowledgment review complete; operational evidence remains open.** Android, Apple, and website acknowledgment tests passed on August 8, 2026.

Risk addressed: high-consequence reliance on incomplete, stale, unavailable, or incorrect information.

Actions:

1. Retain the Android, Apple, and managed-access acknowledgements describing best-effort, as-is, and as-available operation.
2. Keep stale, unavailable, unqualified, and revoked states explicit in the user interface.
3. Require independent operational verification of navigation, airspace, communications, incident-command information, and flight safety.
4. Keep unqualified managed-video paths disabled or conspicuously pilot-labeled.
5. Do not claim certification, guaranteed availability, suitability for life-safety reliance, or a committed service level.

Exit evidence:

- Cross-platform review of all launch/consent gates and degraded-state indicators.
- Board-approved training statement and public capability language.

Owner: operational lead plus primary developer.
Target: continuous; verify before each public release.

## Priority 1 — Prevent cross-organization harm and prepare response

Complete these items before a second external organization or any paying customer.

### P1-1 Prove tenant boundaries

Status: **Implemented in the pending review source; independent review and remaining adversarial cases are open.** The route matrix and executable inventory are in `docs/SECURITY_AUTHORIZATION_MATRIX.md` and `tests/test_security_authorization_inventory.py`. Coordination-specific tests prove that organizations sharing an external map ID and Remote ID do not share zones, owners, confirmations, broadcasts, or persisted lookup scope; an additive migration test proves existing rows enter the legacy namespace. The unscoped `/ws/r2c` and `/upload` compatibility routes, the legacy `/r2c` snapshot, the dormant `/ws` dashboard-refresh socket, and unused public API-documentation routes have been removed. Release regression now creates or uses a dedicated organization-scoped device credential.

Risk addressed: one organization reading, changing, deleting, restoring, or controlling another organization's data or devices.

Actions:

1. Build an authorization matrix covering every HTTP route, WebSocket message, database query, storage path, archive/import path, export, enrollment campaign, device credential, member role, stream request, and audit view.
2. Retain negative tests for cross-organization identifiers, path traversal, stale sessions, role downgrade/removal, archived organizations, revoked devices, and bounded archive restore; extend coverage for replay, concurrent changes, and sustained resource exhaustion.
3. Test every role, especially `organization_owner`, `records_admin`, billing, viewer, user, and video requester.
4. Obtain a focused independent review of organization filters and storage-path confinement.

Exit evidence:

- The authorization matrix maps every sensitive action to an enforced organization and role check.
- All negative tests pass from a clean build.
- Independent findings are closed or explicitly accepted by the Board.

Owner: primary developer plus independent application reviewer.

### P1-2 Establish incident response and security monitoring

Status: **Draft procedures complete; approval, alert delivery, and exercise remain open.** See `SECURITY.md`, `docs/INCIDENT_RESPONSE_RUNBOOK.md`, and `docs/SECURITY_MONITORING_STANDARD.md`. The cloud project currently has no notification channel, alert policy, or custom log metric.

Risk addressed: delayed containment or notification after credential compromise, tenant exposure, or service abuse.

Actions:

1. Publish a security contact and define severity, triage authority, evidence handling, containment, credential rotation, vendor escalation, agency communication, and counsel/insurer notification.
2. Alert on repeated administrative login failures, suspicious enrollment redemption, cross-tenant authorization failures, unusual exports/downloads, secret access, IAM/firewall changes, and database/storage errors.
3. Define log access and retention while excluding secrets, OAuth codes, reset tokens, raw media, and unnecessary precise location.
4. Tabletop compromised device, organization administrator, platform administrator, deployment identity, database host, and tenant-data exposure scenarios.

Exit evidence:

- Board-approved incident runbook and current contact tree.
- One completed tabletop with actions, owners, and deadlines.
- Test alerts reach two authorized responders.

Owner: Board security owner plus technical administrator.

### P1-3 Provide minimum platform continuity

Status: **Reconstruction procedure drafted; backup separation and recovery exercise remain open.** See `docs/PLATFORM_CONTINUITY_RUNBOOK.md`.

Risk addressed: inability to reconstruct the hosted service after VM/disk loss or privileged compromise. This does not replace or involve the organization-admin records workflow.

Actions:

1. Document how to rebuild the service, database host, network path, runtime identity, and secret references from a clean environment.
2. Protect a platform-level backup of control-plane state, organization accounts/roles, enrollments/revocations, and audit history as appropriate.
3. Keep platform recovery access separate from the same identity that could destroy the live environment.
4. Perform one isolated reconstruction exercise and record elapsed time and unrecovered data.

Exit evidence:

- A backup administrator reconstructs a non-production environment without the primary developer.
- Organization self-service exports and restores remain unchanged and require no site-administrator participation.

Owner: technical administrator plus Board witness.
Priority: after P0 host hardening and P1 tenant-boundary work; not a routine organization support task.

### P1-4 Guard routine cloud releases

Status: **Implemented and live-verified; witnessed rollback exercise pending.** Tracker v1.4.25 was deployed to a zero-traffic candidate, passed the cloud regression, and was promoted atomically after two idle checks.

Risk addressed: a release that passes local tests but fails against Google Cloud networking, database, storage, routing, secrets, or runtime behavior.

Actions:

1. Block candidate deployment and production promotion while recent coordination, dashboard, or managed-video activity is present.
2. Deploy the candidate to a tagged Cloud Run URL with zero production traffic and run cloud-backed HTTP, database, authorization, and coordination-WebSocket checks against it.
3. Promote with an atomic 100-percent traffic switch only after tests and a second activity check pass; retain an explicit command for immediate rollback to the recorded revision.
4. Keep startup migrations backward-compatible with the prior revision and require a separate expand/migrate/contract maintenance plan for destructive schema work.
5. Defer HA Cloud SQL and multi-instance traffic splitting until use and funding justify the cost and process-local coordination state has been replaced.

Exit evidence:

- A candidate revision fails without receiving production traffic when a cloud dependency or regression check is unhealthy.
- An idle release is promoted and the public version is verified; a witnessed exercise successfully rolls traffic back to the recorded prior revision.

Owner: primary developer plus independent release witness.

## Priority 2 — Software assurance before broad use

### P2-1 Secure the software supply chain

Status: **Server baseline implemented; full program coverage remains open.** The server gate passed 228 tests, dependency audit, medium/high static analysis, secret-baseline enforcement, and SBOM generation under Python 3.12 on August 8, 2026. The repository now includes the Apache 2.0 license stated for the open-source program.

1. Replace floating production dependencies with reviewed locked or hash-pinned sets.
2. Generate SBOMs for the server, Android, Apple, websites, container, and bundled media components.
3. Retain dependency and static-analysis gates; add container, secret-history, and license-policy scans with documented severity thresholds.
4. Protect release branches/tags and retain exact source, build inputs, test results, image digest, and rollback target for each deployment.

Exit evidence: reproducible release record plus reviewed scan/SBOM results.

### P2-2 Obtain independent assessment

Status: **Open.** The internal evidence package is better prepared for an independent reviewer, but no independent assessment has been represented as complete.

1. Commission a scoped application and cloud review after P0 and P1 controls are in place.
2. Include authentication, tenant isolation, archive/import handling, WebSockets, enrollment, managed video signaling, public cloud edge, database host, secrets, and administrative recovery.
3. Track every finding to closure, mitigation, time-bounded acceptance, or feature deferral.

Exit evidence: independent report and Board-approved disposition register.

### P2-3 Qualify managed video separately

Status: **Open.** Source/server tests do not establish physical Android or Apple readiness.

1. Test direct and TURN paths, role enforcement, operator consent, one-viewer behavior, teardown, reconnection, privacy, data use, thermal load, and battery impact.
2. Report Android and Apple readiness separately and require physical-device evidence.
3. Do not advertise production readiness until both the operational and security leads approve the tested scope.

Exit evidence: signed platform-by-platform qualification matrix.

### P2-4 Confirm the CalTopo name and Teams API relationship

Status: **Public disclosure implemented; direct vendor review open.** Public materials describe RID2Caltopo as independent and not affiliated with or endorsed by CalTopo, identify use of the CalTopo Teams API, and thank the CalTopo team for its product and API support.

1. Provide CalTopo founder Matt Jacobs a demonstration of the RID2Caltopo and r2c-tracker ecosystem.
2. Request written confirmation regarding the RID2Caltopo name, Teams API use, attribution, branding, and any applicable terms or limits.
3. Preserve the response and any resulting commitments in the Board's program records.
4. Update public language or product naming promptly if CalTopo requests a reasonable change or counsel identifies a material trademark or contract risk.

Exit evidence: retained written vendor response and Board disposition of any requested changes.

## Program stop conditions

Pause affected onboarding or capability use when any of the following occurs:

- suspected cross-organization access or missing organization authorization;
- loss or compromise of a privileged, deployment, signing, or recovery identity;
- unexpected public exposure of the database service or host administration;
- inability to communicate stale, unavailable, or unqualified operational state truthfully;
- a critical exploitable dependency or secret disclosure without an effective mitigation;
- managed video operating outside its approved consent, viewer, or routing constraints.

## Board reporting

Until Priority 0 and Priority 1 are complete, provide a short monthly report listing:

- open P0/P1 actions, owner, target, and evidence;
- security events and near misses;
- privileged-access and firewall changes;
- backup/reconstruction exercise status;
- tenant-boundary test status;
- deferred or disabled capabilities;
- risks accepted by the Board and their expiration dates.

Afterward, quarterly reporting is appropriate while the service remains a best-effort public-service program.

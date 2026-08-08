# r2c-tracker Incident Response Runbook

Status: interim pilot procedure, pending program-operator approval
Security contact: `kjtsar@kjt.us` (replace with a program-operator-controlled address)

This runbook governs suspected compromise, cross-organization access, material service abuse, or loss of trustworthy operational state. Response is best effort; these targets are not a service-level agreement.

## Authority and roles

| Role | Interim assignment | Authority |
|---|---|---|
| Incident commander | Board-designated security owner; primary developer until assigned | Declares severity, pauses onboarding/capabilities, coordinates agencies |
| Technical responder | Primary developer plus backup cloud administrator | Preserves evidence, contains systems, rotates credentials, restores service |
| Agency liaison | Board-designated officer | Contacts affected organizations and incident command |
| Legal/privacy liaison | Board officer and counsel/insurer as appropriate | Determines legal, insurance, privacy, and notification duties |
| Scribe | Person not making the primary technical changes | Maintains the event timeline, decisions, evidence references, and follow-up register |

No responder should investigate with an affected user's identity or access another organization's operational records unless specifically authorized and necessary for containment. Preserve original evidence; work from copies.

## Severity and first actions

| Severity | Examples | Immediate action |
|---|---|---|
| Critical | Cross-organization access; platform-admin or cloud compromise; public database exposure; leaked signing/deployment credential; falsified safety state | Page two responders, preserve logs, contain immediately, pause affected onboarding/capability |
| High | Active organization-admin/device credential compromise; destructive abuse; material outage during operations | Notify two responders promptly, revoke affected access, contact the organization |
| Medium | Repeated attack attempts; security control degradation without known access; exploitable defect with meaningful prerequisites | Create case, preserve evidence, mitigate on a defined schedule |
| Low | Hardening improvement or unsuccessful low-impact probe | Track for normal remediation |

For Critical or High events:

1. Start a UTC timeline and assign incident commander and scribe.
2. Record who reported the issue, affected component/organization, current operations, and the least sensitive reproduction evidence.
3. Preserve relevant Cloud Audit Logs, Cloud Run logs, VM serial/system logs, application audit records, and deployment/image identifiers. Do not paste secrets or raw flight/media data into the timeline.
4. Contain the narrowest affected boundary: revoke a device, end sessions, disable an enrollment campaign, archive an organization, disable a capability, remove a revision from traffic, or restrict network ingress.
5. If tenant separation or displayed operational truth cannot be trusted, stop the affected service or capability and tell impacted organizations to use independent methods.
6. Rotate exposed credentials only after preserving enough evidence to determine scope. Treat all credentials reachable by a compromised identity as exposed.
7. Notify the Board liaison, affected agencies, counsel, insurer, vendors, or law enforcement as the facts and applicable duties require.

## Scenario containment

| Scenario | First containment | Required follow-up |
|---|---|---|
| Device credential | Revoke the device credential and active enrollment campaign; preserve credential ID and audit events | Confirm uploads, FAA proxy, WebSocket, and video signaling reject the credential |
| Organization administrator | Disable/replace the member, invalidate sessions, preserve role and audit history | Review exports, deletes/restores, member and enrollment changes for that organization |
| Platform administrator | Disable the authoritative identity, revoke sessions/OAuth grants, restrict deployment access | Review all organization lifecycle, IAM, secret, billing, and deployment changes |
| Deployment identity or source release | Disable the identity/token, stop releases, retain image digests and build provenance | Rebuild from reviewed source and rotate reachable runtime secrets |
| Database host | Restrict ingress or stop the VM if necessary; snapshot evidence before repair when feasible | Rebuild from trusted configuration; rotate database and reachable service credentials |
| Suspected tenant exposure | Freeze destructive maintenance, preserve queries/logs, identify exact organizations and time range | Validate access boundaries, notify affected organizations through the liaison, independently review the fix |
| Incorrect or stale public-safety output | Mark unavailable/disable the capability and communicate independent-verification guidance | Requalify Android and Apple behavior separately before restoring claims |

## Evidence and privacy

- Store the incident register in a program-operator-controlled location with access limited to responders and the governance liaison.
- Use UTC timestamps and record original log/query references, hashes for exported evidence, and every containment change.
- Do not collect raw media, precise locations, flight records, passwords, OAuth codes, reset links, API tokens, or full session cookies unless essential. Redact before sharing.
- Retain security/audit evidence according to the Board-approved records schedule and any legal hold. Do not invent a retention period before that schedule exists.

## Recovery and closure

Recovery requires: the exploited path is contained; privileged and reachable credentials are rotated; tenant and role tests pass; monitoring is restored; affected agencies receive usable status; and the incident commander approves service restoration. Critical cross-tenant, cloud-admin, or safety-state incidents also require an independent reviewer before normal onboarding resumes.

Within ten business days of containment, document root cause, affected scope, evidence limits, notification decisions, remediation owners/dates, and changes to tests, monitoring, training, and this runbook. The Board explicitly accepts or closes every residual Critical/High risk.

## Tabletop checklist

At least annually and before paid or broad external service, exercise: a stolen device credential; an organization administrator takeover; platform/cloud administrator compromise; database-host loss; and suspected cross-tenant disclosure. Confirm that two people receive the test alert and that a backup administrator can perform the documented containment without the primary developer.

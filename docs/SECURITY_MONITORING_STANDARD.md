# r2c-tracker Security Monitoring Standard

Status: minimum design; alert delivery is not complete until two program-operator-controlled responders are configured and tested

## Required detections

| Signal | Minimum response |
|---|---|
| Repeated platform or organization login failures; password reset abuse | Rate-limit or investigate source/account; escalate on success following failures |
| Cross-organization authorization or credential/designator mismatch | High-priority investigation; pause onboarding if enforcement may have failed |
| Enrollment redemption bursts, replay, expiry, or revoked-device use | Disable campaign/credential and contact organization administrator |
| Unusual record exports, archive downloads, deletes, restores, member/role changes | Verify with organization administrator; preserve application audit trail |
| Secret Manager access, IAM/service-account changes, OAuth changes | Verify change ticket/actor; rotate or contain if unexplained |
| Firewall, public IP, VM metadata, serial console, OS Login, or VM-state changes | Verify immediately; check database-host exposure |
| Cloud Run revision/traffic changes or unexpected image digest | Stop release/rollback and validate provenance |
| Database errors, storage confinement errors, backup/restore failures | Protect data integrity; communicate degraded capability |
| Safety-data upstream failure or stale/unavailable state | Confirm the UI remains explicit; disable misleading output |
| Managed-video request/consent/signaling anomalies | Stop the request/session and verify organization/requester/device ownership |

## Logging and privacy

Security events should include UTC time, event type, outcome, organization ID/designator where necessary, actor/credential identifier, request or correlation ID, source network metadata where justified, and affected resource ID. Logs must not contain passwords, API/device tokens, session cookies, OAuth codes, reset/activation links, raw video, or unnecessary precise location. Credential diagnostics may record length and a short non-reversible suffix only when necessary and reviewed.

Access to security logs is limited to authorized responders and is itself audited. The Board must approve retention periods based on response, privacy, grant/contract, and legal needs.

## Alert operation

- Every Critical/High alert routes to at least two separately held program-operator-controlled destinations.
- Alerts include the relevant runbook, project/service, UTC window, and safe investigation query; never include a secret or raw operational record.
- Test delivery quarterly and after channel/IAM changes. Record receipt by both responders.
- Review alert rules monthly during the pilot for blind spots and noise. A noisy alert must be tuned, not silently disabled.
- Record planned administrative changes so responders can distinguish authorized work.

## Current implementation status

As of August 7, 2026, the Google Cloud project has no configured notification channel, alert policy, or custom log-based metric. Application logs already emit several authentication, device mismatch, video lifecycle, and error events, but alert delivery and retention approval remain open. Creating useful notification channels requires governance-designated responder addresses or a program-operator-controlled group.

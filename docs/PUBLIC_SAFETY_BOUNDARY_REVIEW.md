# Public-Safety Boundary Review

Reviewed August 7, 2026. This is source and automated-test evidence, not a field qualification, certification, or guarantee.

## Consent and public language

| Surface | Evidence | Result |
|---|---|---|
| Android launch gate | `LaunchDisclaimer.kt` and `LaunchDisclaimerTest.kt` | Requires an explicit I agree/disagree decision and states best-effort, as-is/as-available operation; no express or implied warranties/guarantees including suitability, reliability, availability, accuracy, or completeness; independent verification required |
| Apple launch gate | `ApplicationLaunchDisclaimer.swift`, app launch integration, and R2CCore test | Materially identical safety language and explicit decision path |
| Managed-access request | RID2Caltopo website managed-pilot form and rendered-HTML test | Requires versioned acknowledgment before submission; states best-effort/as-is/as-available limitations and supplemental-only use |
| r2c-tracker intake | `/managed-access-requests` and control-plane record | Requires authenticated site-to-site intake and stores acknowledgment value/version/time for administrator review |
| Public project language | RID2Caltopo website and release notes | Personal donations paused; no guaranteed service or payment prerequisite represented |

## Automated validation performed

- Android focused disclaimer unit test: passed.
- Apple focused disclaimer test: passed under the Swift Testing runner.
- RID2Caltopo website build and seven rendered-page tests: passed, including the managed-access acknowledgment assertions.
- r2c-tracker production-runtime security gate: passed, including request-ingest and control-plane tests.

## Operational boundary

RID2Caltopo and r2c-tracker provide supplemental situational awareness. Operators must independently verify navigation, airspace/NOTAMs, flight safety, communications, incident-command information, and other safety-critical facts. Unavailable, stale, unqualified, revoked, or archived states must remain visible and must not be converted into apparently valid data.

Managed video remains separately qualified by platform, network path, device, consent behavior, and field evidence. Passing server signaling tests does not establish Android or Apple physical-device readiness, radio coverage, media reliability, thermal/battery suitability, or operational approval.

## Evidence still required before broad external use

- Manual Android and Apple review of the complete first-launch agree/disagree flow on current release builds.
- Degraded-state exercises for loss of FAA/NOTAM, tracker enrollment, network, Remote ID source, and managed-video relay.
- Board-approved operator training statement and capability claims.
- Platform-by-platform physical-device qualification for any managed-video scope presented as available.

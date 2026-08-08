# Security Policy

## Scope

This policy covers the r2c-tracker service and its organization control plane. RID2Caltopo Android, Apple, and website findings that affect the hosted service may also be reported here until NCSSAR establishes a program-wide security address.

The software is a supplemental, best-effort public-safety tool. It is not a certified dispatch, navigation, collision-avoidance, aviation, or life-safety system and must not be the sole source for operational decisions.

## Reporting a vulnerability

Report suspected vulnerabilities privately to `kjtsar@kjt.us`. Do not include live credentials, unnecessary precise incident locations, flight records, or personal information in the initial message. If sensitive evidence is necessary, request a protected transfer method first.

Include:

- affected component and version or deployed URL;
- steps to reproduce and expected impact;
- whether another organization, credential, or operational record may be affected;
- any logs or screenshots with secrets and operational data redacted;
- a safe way to contact the reporter.

Do not test against another organization's data, interfere with active incident operations, degrade service, or retain data obtained unintentionally. Stop testing and report immediately if cross-organization or privileged access is observed.

## Initial handling targets

These are best-effort response targets, not a service-level agreement:

- Critical: acknowledge as soon as practical; immediately evaluate containment.
- High: acknowledge within two business days.
- Medium or Low: acknowledge within five business days.

Critical examples include cross-organization access, active credential disclosure, unauthorized platform administration, public database exposure, or a defect likely to create unsafe operational reliance.

## Disclosure

Please allow time for validation, affected-agency coordination, remediation, and safe deployment before public disclosure. NCSSAR or the current maintainer will coordinate disclosure timing in good faith but cannot promise a fixed embargo or bounty.

## Supported versions

Only the currently deployed pilot revision and current mobile releases are supported. Older builds may be unable to receive server-side security improvements and should be upgraded or retired.

## Security ownership transition

This contact and policy are interim. Adoption should replace the personal contact with an NCSSAR-controlled security address, at least two authorized responders, and a Board-approved incident-response and notification process.

# r2c-tracker Authorization Matrix

This matrix records the intended trust boundary for tenant and platform interfaces. The executable inventory test in `tests/test_security_authorization_inventory.py` fails when a new organization or platform route has no declared guard.

## Actors

| Actor | Authentication | Intended scope |
|---|---|---|
| Public visitor | None | Public organization dashboard only when the organization selects public records visibility |
| Activation or recovery user | Expiring activation/reset state plus CSRF and identity verification | One organization and one activation/reset transaction |
| Organization member | Organization session; active user; designator and organization ID match | Assigned organization and role set |
| Records administrator | Organization session with `organization_owner` or `records_admin` | That organization's flight records and namespaced flight-log files |
| Video requester | Organization session with `video_requester` | Own organization's streams and the requester's own video request lifecycle |
| Enrolled device | Revocable per-device credential whose stored hash resolves to one organization | Upload, coordination, FAA proxy, and signaling for the credential's organization |
| Platform administrator | Separate platform-admin authentication and CSRF for state changes | Organization lifecycle and service metadata, not tenant flight content |

## Route families

| Interface | Required enforcement | Data boundary |
|---|---|---|
| `/{designator}` dashboard | Public only when `records_visibility == public`; otherwise organization session with a records role | Queries filter by `organization_id` |
| `/{designator}/admin` and settings/members/enrollments | `require_organization_user`; route-specific role; CSRF on mutations | Organization returned by the authenticated designator/session match |
| `/{designator}/admin/flights/**` | `require_organization_records_admin`; CSRF on mutations | Database queries include `Flight.organization_id`; archive paths require `organizations/{designator}/` confinement |
| `/{designator}/upload` | Device credential plus `require_scoped_upload_credential` | URL designator must match credential designator; created record receives credential organization ID |
| `/{designator}/ws/r2c` | Device credential resolved by `authenticate_tracker_session` | Credential designator must match URL designator before socket acceptance; the resolved organization ID namespaces all runtime groups, broadcasts, ownership/confirmation keys, standalone proximity matching, and persisted coordination queries |
| `/{designator}/streams/events` | Active organization session, matching organization ID/designator, `video_requester` role | Event hub and status queries keyed by organization ID and requester ID |
| `/{designator}/streams/requests/**` | Organization session, `video_requester`, CSRF on mutations | Store methods require organization ID and requester user ID to match the request |
| `/api/v1/device-enrollment/redeem` | Signed locator, active bounded campaign, redemption checks | Returns a new credential bound to the campaign organization |
| `/platform-admin/**` | Separate platform authentication; CSRF on authenticated mutations | Organization lifecycle, contacts, subscription/usage metadata, provisioning, and audit state |
| Retired `/admin/**` mutations | Disabled by default by `LEGACY_ADMIN_ENABLED=false`; legacy HTTP Basic only if deliberately re-enabled | Only historical records whose `organization_id` is null |

## Explicit public/bootstrap routes

Activation, login, password reset, Google authorization start/callback, and enrollment landing routes are intentionally reachable before an organization session exists. They must remain transaction-scoped, rate-limited where applicable, CSRF-protected on browser mutations, and unable to return tenant records.

## Required negative tests

- Substitute another organization's designator, record ID, campaign ID, request ID, session ID, archive path, or WebSocket URL.
- Reuse a stale session after role removal, organization archival, or administrator replacement.
- Present a revoked, expired, wrong-organization, or replayed device/enrollment credential.
- Attempt path traversal, absolute paths, symlink escape, oversized archives, archive bombs, and malformed GeoJSON/CSV.
- Race delete/import, role changes, enrollment redemption, and video request/stop operations.
- Confirm platform-admin routes cannot read tenant flight content and tenant roles cannot call platform lifecycle actions.

## Evidence status

Current tests cover organization-scoped flight listing/export/deletion/import, namespaced archive restoration, cross-tenant log denial, organization archive/unarchive behavior, device credential scoping, coordination isolation for identical map and Remote IDs, coordination-schema migration, and video request ownership checks. The route-inventory test is structural regression evidence; it does not replace adversarial review or a penetration test.

# FAA NOTAM Proxy Rollout

The proxy must be deployed and verified before releasing RID2Caltopo builds that
depend on it. Existing app releases continue to query FAA directly, so the
server-first rollout is backward compatible.

## 1. Configure the tracker

Set these without printing their values:

```bash
export FAA_NOTAM_CLIENT_ID='...'
export FAA_NOTAM_CLIENT_SECRET='...'
```

Set endpoint overrides when qualifying against FAA staging:

```bash
export FAA_NOTAM_API_BASE_URL='https://api-staging.cgifederal-aim.com/nmsapi'
export FAA_NOTAM_TOKEN_URL='https://api-staging.cgifederal-aim.com/v1/auth/token'
```

`deploy.sh` creates or reuses separate Secret Manager secrets for the FAA client
ID and client secret. These environment variables are required only when their
Secret Manager secrets do not exist. Updating the shell variables does not
rotate an existing secret version; rotate the corresponding Secret Manager
secret explicitly.

## 2. Verify the deployed proxy

Use a known test coordinate and an enrolled organization device credential:

```bash
curl --fail-with-body \
  -H "X-SAR-Token: ${TRACKER_DEVICE_TOKEN}" \
  "${TRACKER_URL}/faa/notams?latitude=39.153&longitude=-121.133&radius=2"
```

Verify:

- the body has the FAA response shape with `data.geojson`
- `X-R2C-FAA-Cache` is `MISS` on the first request
- an immediate identical request reports `HIT`
- a missing or incorrect tracker token is rejected
- no FAA client ID, client secret, or bearer token appears in tracker logs

## 3. Release the clients

The migrated Android and Apple clients use the tracker URL and
`X-SAR-Token`. They no longer request FAA OAuth tokens or send FAA bearer
tokens. Organization configuration therefore must include a working tracker URL
and tracker credential before NOTAM monitoring can become configured.

Existing FAA QR/config parsing remains temporarily for compatibility with
already-issued organization bundles, but it is not used by the NOTAM network
path. Remove the legacy distribution and local-storage machinery only after the
proxy has passed field qualification and older app releases have aged out.

## 4. Field qualification

Exercise at least:

- two devices querying the same incident area to prove cache reuse
- devices in widely separated California locations
- one query outside California
- a tracker-token rejection
- an FAA authentication rejection
- an FAA timeout or network interruption
- recovery after each failure while the app retains prior notices and shows an
  unavailable or stale state rather than clear
- a NOTAM near the selected-radius boundary

## Cache behavior

Defaults:

- fresh TTL: 90 seconds
- maximum entries: 512 per tracker process
- maximum cache memory: 64 MiB per tracker process
- maximum cacheable response: 8 MiB
- geographic grid: 0.002 degrees
- maximum concurrent upstream FAA requests: 8

The cache is not California-specific. Each full request maps to a small
geographic cell, and the upstream radius is expanded to cover the cell's
furthest corner. The client still computes notice distance against its actual
position. At the FAA 100 NM maximum, the proxy uses the exact coordinate rather
than a cell because it cannot safely expand the upstream radius.

Android incremental requests containing `lastUpdatedDate` bypass the cache.
The proxy does not return expired entries as successful responses.

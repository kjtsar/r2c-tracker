# RID2Caltopo Multi-Zone Coordination Protocol

This document describes the coordination behavior implemented by
`r2c-tracker` on `/ws/r2c`. The goal is to make RID detection and reporting
predictable when several RID2Caltopo zones are watching the same Caltopo map.

This is the path that matters most for release confidence:

1. zones connect to the same `mapId`
2. a zone claims first sighting of a drone (`remoteId`)
3. the tracker assigns exactly one current owner for that `mapId + remoteId`
4. non-owner zones relay sightings to the current owner
5. the owner writes the canonical reporting output into Caltopo

The tracker is a rendezvous/lease service. It does not replace the owner-side
RID2Caltopo logic that actually writes ordered track state into Caltopo.

## Scope and assumptions

- Ownership is isolated by `mapId`. The same `remoteId` can legitimately have a
  different owner on another map.
- A "zone" is one RID2Caltopo client instance connected to `/ws/r2c`.
- A zone should present a stable `guid` for its lifetime. Using the same value
  as `zoneId` is acceptable and is the common case in this implementation.
- The tracker expects a small active fleet. Your stated target of about 6 zones
  with 2-4 drones per zone is well within the shape this protocol is designed
  for.

## Authentication

The websocket requires the `X-SAR-Token` header. The value must match the
server's `TRACKER_API_KEY`.

Example request headers:

```http
X-SAR-Token: <shared tracker token>
User-Agent: RID2Caltopo/coordination
```

## Message flow

### 1. Zone registration

Client sends:

```json
{
  "type": "hello",
  "mapId": "MAP1",
  "zoneId": "zone-alpha",
  "guid": "zone-alpha",
  "name": "Alpha",
  "lat": 39.1,
  "lng": -121.1,
  "caltopoRttMs": 1800
}
```

Server responds:

```json
{
  "type": "hello_ack",
  "serverTime": 1710000000000,
  "heartbeatSec": 15,
  "leaseSec": 45
}
```

Effects:

- zone presence is registered under `mapId`
- zone state is mirrored into SQL
- all connected zones on the map receive a `zone_update`

### 2. Zone heartbeats

Client sends:

```json
{
  "type": "heartbeat",
  "seq": 17,
  "lat": 39.1,
  "lng": -121.1,
  "caltopoRttMs": 1750
}
```

Server responds:

```json
{
  "type": "heartbeat_ack",
  "serverTime": 1710000005000,
  "mapId": "MAP1",
  "zoneId": "zone-alpha",
  "guid": "zone-alpha",
  "leaseSec": 45,
  "ownerLeaseExpireTs": 1710000050000,
  "clientSeq": 17
}
```

Effects:

- zone position and `lastSeenMs` are refreshed
- if this zone currently owns any drones, their leases are extended
- the heartbeat only extends leases for drones owned by this zone's `guid`

### 3. First sighting / owner claim

Client sends:

```json
{
  "type": "first_sighting",
  "mapId": "MAP1",
  "remoteId": "RID-123",
  "zoneId": "zone-alpha",
  "guid": "zone-alpha",
  "droneTs": 1710000001000,
  "distanceFromZoneM": 32.5,
  "mappedId": "1SAR7DJ"
}
```

Server broadcasts:

```json
{
  "type": "owner_assigned",
  "remoteId": "RID-123",
  "ownerGuid": "zone-alpha",
  "ownerZoneId": "zone-alpha",
  "leaseSeq": 1,
  "leaseExpireTs": 1710000046000
}
```

Ownership is determined independently for each `mapId + remoteId`.

## Owner selection rules

When multiple zones claim the same drone, the tracker chooses the better owner
 using this ordering:

1. earlier `droneTs`
2. smaller `distanceFromZoneM`
3. non-empty `mappedId`
4. lexical `guid` tie-breaker

That last step is important: if two zones are otherwise identical, ownership is
still deterministic.

## Relay behavior

If a non-owner zone sees a drone that already has an owner, it sends:

```json
{
  "type": "sighting",
  "mapId": "MAP1",
  "remoteId": "RID-123",
  "zoneId": "zone-bravo",
  "guid": "zone-bravo",
  "droneTs": 1710000003000,
  "lat": 39.3,
  "lng": -121.3,
  "altM": 120.0
}
```

The tracker forwards the payload only to the current owner as:

```json
{
  "type": "relay_sighting",
  "mapId": "MAP1",
  "remoteId": "RID-123",
  "zoneId": "zone-bravo",
  "guid": "zone-bravo",
  "fromZoneId": "zone-bravo",
  "droneTs": 1710000003000,
  "lat": 39.3,
  "lng": -121.3,
  "altM": 120.0
}
```

Notes:

- the owner does not receive its own `sighting` echoed back
- if the owner zone is currently disconnected, the relay is skipped
- recent relay breadcrumbs are mirrored into SQL for troubleshooting

## Ownership release and expiry

There are three ways an owner stops owning a drone:

1. the owner sends `drone_lost`
2. the owner stops heartbeating and the lease expires
3. another zone later wins a fresh `first_sighting` comparison

Explicit release:

```json
{
  "type": "drone_lost",
  "mapId": "MAP1",
  "remoteId": "RID-123",
  "zoneId": "zone-alpha",
  "guid": "zone-alpha"
}
```

Server broadcast:

```json
{
  "type": "owner_expired",
  "remoteId": "RID-123",
  "prevOwnerGuid": "zone-alpha"
}
```

Important behavior:

- only the current owner zone can release ownership with `drone_lost`
- a websocket disconnect marks a zone offline immediately, but ownership is not
  dropped until the lease expires
- the default lease window is 45 seconds and the default heartbeat interval is
  15 seconds

## Zone status broadcasts

Every zone on a map receives `zone_update` payloads like:

```json
{
  "type": "zone_update",
  "zones": [
    {
      "zoneId": "zone-alpha",
      "guid": "zone-alpha",
      "name": "Alpha",
      "lat": 39.1,
      "lng": -121.1,
      "caltopoRttMs": 1800,
      "lastSeenMs": 1710000005000,
      "online": true
    }
  ]
}
```

This is the server's current view of map membership, not a durable audit log.

## Persistence model

The tracker mirrors live coordination state into SQL:

- `r2c_zone_state`: active/recent zone presence
- `r2c_drone_owner_state`: active owner leases
- `r2c_recent_sighting`: recent relayed sightings for debugging

Live routing still happens in memory. Persisted state is there so a process
restart does not fully erase recent coordination context.

## Suggested robustness test matrix

To keep future releases honest, treat the flow as three separable slices.

### 1. Deterministic ownership

Verify that owner assignment is stable across:

- earlier-vs-later `droneTs`
- equal timestamp but different distance
- equal timestamp and distance but only one zone has `mappedId`
- full ties resolved by lexical `guid`
- same `remoteId` claimed on different maps

### 2. Lease continuity

Verify:

- owner heartbeat extends only that owner's leases
- disconnect marks zone offline without immediate ownership loss
- expired leases emit `owner_expired`
- non-owner `drone_lost` does not clear ownership

### 3. Relay correctness

Verify:

- only non-owner sightings are forwarded
- relay goes only to the current owner
- relay is skipped if the owner is disconnected
- recent sightings are recorded for diagnostics

## Operational guidance for 2-6 zones

For the deployment size you described, the main failure modes to protect against
are not raw throughput problems. They are consistency problems:

- two zones claiming the same drone differently
- a stale owner holding a lease after disconnect
- a non-owner accidentally writing to Caltopo
- reconnect churn causing ownership flaps

The best release gate is a compact, deterministic test suite around those cases
plus one operator-readable protocol document. That combination makes regressions
visible before field use and easier to diagnose when something unusual happens.

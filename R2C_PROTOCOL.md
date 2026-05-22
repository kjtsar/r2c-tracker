# RID2Caltopo Multi-Zone Coordination Protocol

This document is the authoritative protocol guide for RID2Caltopo instances
coordinating through `r2c-tracker`.

There is only one R2C instance coordination protocol: the authenticated
websocket at `/ws/r2c`. The separate `/ws` websocket is the web dashboard
live-refresh channel; it is not used by RID2Caltopo clients for drone ownership,
relay, or confirmation coordination.

The current implementation defaults to:

- `R2C_HEARTBEAT_SEC=15`
- `R2C_LEASE_SEC=45`

## Purpose

The tracker is a rendezvous and lease service. It makes RID detection and
reporting predictable when several RID2Caltopo zones are watching the same
CalTopo map, but it does not replace the owner-side RID2Caltopo logic that
writes ordered track state into CalTopo.

The release-critical flow is:

1. zones connect to the same effective coordination map
2. a zone reports the first sighting or confirmation of a drone
3. the tracker assigns exactly one current owner for that map and `remoteId`
4. non-owner zones relay sightings to the current owner
5. the owner writes the canonical reporting output into CalTopo

## Scope and assumptions

- A "zone" is one RID2Caltopo client instance connected to `/ws/r2c`.
- A zone should present a stable `guid` for its lifetime. Using the same value
  as `zoneId` is acceptable and is the common case.
- Ownership is isolated by the effective coordination key. For mapped clients,
  this is the real CalTopo `mapId`. For standalone clients, the tracker either
  adopts a nearby group or assigns a `Standalone_<key>` group.
- Standalone grouping is proximity-based. It ignores incident, op-period,
  profile, and other local RID2Caltopo configuration metadata.
- A mapped instance never changes its coordination key because a standalone
  instance is nearby. Proximity adoption only moves standalone instances.
- The same `remoteId` can legitimately have a different owner on another
  coordination key.
- The target deployment size is small: roughly 2-6 zones and a few active drones
  per zone.

## Authentication

`/ws/r2c` requires the `X-SAR-Token` header. The value must match the server's
`TRACKER_API_KEY`.

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
  "mapId": "MAP1",
  "heartbeatSec": 15,
  "leaseSec": 45,
  "idleRecommended": true,
  "idleParkSec": 120
}
```

Effects:

- the zone is registered under the effective coordination map
- zone state is mirrored into SQL
- all connected zones on that effective map receive `zone_update`
- the joining zone receives any recent, still-valid `drone_confirmed` events
- `idleRecommended` and `idleParkSec` are advisory; older clients can ignore
  them and newer clients can use them to park without keeping Cloud Run active

The `mapId` in `hello_ack` is the effective coordination key, which can differ
from the reported `mapId` for standalone clients.

### 2. Standalone coordination map resolution

Mapped clients use their reported CalTopo `mapId`.

Standalone clients are resolved as follows:

1. if no usable client location is available, use `Standalone_<zone-or-guid>`
2. if a nearby online mapped zone is within two miles, adopt that mapped key
3. otherwise, if a nearby online standalone group is within two miles, join it
4. otherwise, use `Standalone_<zone-or-guid>`

Standalone clients can be rehomed on heartbeat. A parked or isolated standalone
client may later adopt a nearby mapped group from live memory or recent
persisted zone state. When this happens, the tracker moves that zone's state,
replays relevant confirmations, and re-evaluates any owner state from the old
group.

### 3. Zone heartbeats

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

- zone position, CalTopo RTT, and `lastSeenMs` are refreshed
- standalone zones may rehome to a better effective coordination map
- leases are extended for drones owned by this zone's `guid`
- `ownerLeaseExpireTs` reports the furthest lease expiry extended by this
  heartbeat, or `0` if no owner lease was extended
- all connected zones on affected maps receive `zone_update`

Idle clients may intentionally park their websocket when they have no active
drones, no owner leases, and no queued confirmation events. Parked clients
reconnect immediately before sending a new `first_sighting`, `sighting`,
`drone_lost`, or `drone_confirmed`.

Before closing an idle websocket, newer clients may send:

```json
{
  "type": "idle",
  "reason": "no_active_drones"
}
```

Effects:

- the tracker records the zone as `idle` instead of `online`
- `/r2c` can show an `IDLE` badge while the parked zone state remains recent
- the client may then close the websocket to let Cloud Run stop servicing an
  active request

The `idle` message is backwards-compatible. Older trackers ignore the unknown
message type, and older clients ignore the new `hello_ack` advisory fields.

### 4. First sighting / owner claim

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

Server broadcasts when ownership is assigned or changed:

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

Ownership is determined independently for each effective `mapId + remoteId`.

When multiple zones claim the same drone, the tracker chooses the better owner
using this ordering:

1. earlier `droneTs`
2. smaller `distanceFromZoneM`
3. non-empty `mappedId`
4. lexical `guid` tie-breaker

That final tie-breaker keeps full ties deterministic.

If the current owner came from an unexpired `drone_confirmed` event,
`first_sighting` does not steal ownership. Once that confirmation-sourced owner
lease expires, a later `first_sighting` can assign a new owner.

### 5. Sighting relay

Client sends ongoing sighting messages:

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

If the sender is the current owner, the tracker refreshes that owner's lease
and does not echo the sighting.

If the sender is not the current owner, the tracker forwards the payload only to
the connected current owner as:

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

- non-owner `sighting` messages do not refresh the owner lease; they prove drone
  activity, not owner app health
- RID2Caltopo throttles non-owner `sighting` sends per drone, currently to one
  update every three seconds by default
- if the owner zone is disconnected, the relay is skipped
- recent relay breadcrumbs are mirrored into SQL for troubleshooting

### 6. Drone confirmation broadcast and ownership

When an operator presses Save in the Drone Confirmation panel, the Android app
sends:

```json
{
  "type": "drone_confirmed",
  "mapId": "MAP1",
  "remoteId": "RID-123",
  "zoneId": "zone-alpha",
  "guid": "zone-alpha",
  "mappedId": "1SAR7DJ",
  "trackLabel": "1SAR7DJ",
  "org": "NCSSAR",
  "model": "Mavic 3",
  "ownerName": "Pilot"
}
```

The tracker:

- records and persists the confirmation event
- broadcasts `drone_confirmed` to every online zone on that effective map,
  including the sender
- assigns the confirming zone as the owner
- broadcasts `owner_assigned` for the confirmation-sourced owner

The broadcast event includes tracker-populated fields such as
`confirmedByGuid`, `confirmedAtMs`, and `confirmationEventId`.

Clients treat the `remoteId` as already handled only for the current active
tracker flight, so they dismiss any matching active confirmation panel without
suppressing prompts for later flights.

Confirmation events are retained for replay to late joiners, deduped by remote
ID and event identity, and loaded from persisted state after tracker restart
when the confirming zone is still active. They are forgotten when the owner
sends `drone_lost`, when the confirming zone disconnects, or when the retention
window expires.

### 7. Ownership release and expiry

There are four ways an owner stops owning a drone:

1. the owner sends `drone_lost`
2. the owner stops sending heartbeat or owner-originated sighting updates and
   the lease expires
3. another zone later wins a fresh `first_sighting` comparison
4. a confirmation-sourced owner disconnects, which clears that confirmation and
   emits owner expiry immediately

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
- a websocket disconnect marks a zone offline immediately
- normal first-sighting owners are retained until the lease expires
- confirmation-sourced owners are cleared immediately when the confirming zone
  disconnects
- heartbeat is a backup lease signal while an owner is connected but not sending
  accepted owner telemetry; standby clients should park instead of heartbeating
  forever

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

- `r2c_zone_state`: active/recent zone presence, reported map, and coordination
  mode
- `r2c_drone_owner_state`: active owner leases
- `r2c_drone_confirmation_state`: recent Drone Confirmation Save events for
  replay and restart continuity
- `r2c_recent_sighting`: recent relayed sightings for debugging

Live routing still happens in memory. Persisted state exists so a process
restart does not fully erase recent coordination context and so `/r2c` can show
recent state.

## Robustness test matrix

To keep releases honest, treat the flow as four separable slices.

### 1. Deterministic ownership

Verify that owner assignment is stable across:

- earlier-vs-later `droneTs`
- equal timestamp but different distance
- equal timestamp and distance but only one zone has `mappedId`
- full ties resolved by lexical `guid`
- same `remoteId` claimed on different maps
- active confirmation-sourced owner resisting a later `first_sighting`

### 2. Lease continuity

Verify:

- owner heartbeat extends only that owner's leases
- owner-originated `sighting` extends only that owner's lease
- non-owner relayed `sighting` does not extend the owner lease
- disconnect marks zone offline
- normal first-sighting owners expire by lease timeout
- confirmation-sourced owners expire on confirming-zone disconnect
- non-owner `drone_lost` does not clear ownership

### 3. Relay correctness

Verify:

- only non-owner sightings are forwarded
- relay goes only to the current owner
- relay is skipped if the owner is disconnected
- recent sightings are recorded for diagnostics

### 4. Confirmation replay

Verify:

- `drone_confirmed` broadcasts to all online zones on the effective map
- Save-driven confirmation also broadcasts `owner_assigned`
- late joiners receive recent confirmations
- confirmation replay does not cross maps except through standalone rehome
- `drone_lost` and confirming-zone disconnect clear replay state

## Operational guidance for 2-6 zones

For the deployment size this protocol targets, the main failure modes to protect
against are consistency problems:

- two zones claiming the same drone differently
- a stale owner holding a lease after disconnect
- a non-owner accidentally writing to CalTopo
- parked or reconnected clients missing a confirmation
- standalone clients unintentionally sharing or failing to share a coordination
  group

The best release gate is a compact deterministic test suite around those cases
plus this single operator-readable protocol document.

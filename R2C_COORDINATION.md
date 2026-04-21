# RID2Caltopo Coordination Hub

`r2c-tracker` now exposes a lightweight websocket coordination endpoint for
distributed RID2Caltopo zones:

- dashboard websocket: `/ws`
- zone coordination websocket: `/ws/r2c`

The coordination endpoint is authenticated with the existing `X-SAR-Token`
header. Live websocket routing stays in memory, but zone presence, owner leases,
and recent sighting breadcrumbs are also mirrored into the existing SQL backend
so the service does not depend on filesystem persistence.

Its job is intentionally small:

- register active zones for a `mapId`
- broadcast current zone presence
- assign one owner per `remoteId`
- relay non-owner sightings to the current owner
- expire ownership when the owner disconnects, stops heartbeating, or releases the drone

## Owner selection

For a given `mapId + remoteId`, the tracker picks the better candidate using:

1. earliest `droneTs`
2. smallest `distanceFromZoneM`
3. zone with a non-empty `mappedId`
4. lexical GUID tie-breaker

## Important limits of this slice

- The tracker is not the authoritative ordered waypoint store
- No accepted-waypoint log on the server
- Owner-side sequencing still lives in RID2Caltopo

That is intentional. The tracker is acting as a
public rendezvous and relay service, not the primary waypoint store.

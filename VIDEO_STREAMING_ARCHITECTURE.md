# Managed organization video streaming

Status: milestones 1 through 4 are implemented for the tracker, Android, and
Apple clients. The selection and approval portion of milestone 5 is also in
source; no remote media path is enabled by this work.

## Product contract

An authenticated organization member with the `video_requester` role may ask
to view a currently advertised stream. By default, a request never starts
video until the RID2Caltopo operator sees the requester's verified organization
email, reviews the link estimate and data-rate choices, and explicitly selects
Start.

Android and Apple also provide a persistent, device-local `Remote Video
Control` setting, off by default. When it is on, the browser requester sees the
measured route and bandwidth-safe quality choices and selects Start directly.
The setting is advertised by the enrolled device; it is not an organization or
platform-administrator bypass. Each request snapshots the advertised setting
so changing it does not alter a request already in progress.

The browser and tablet must display:

- `Direct` when the selected ICE candidate pair is host, server-reflexive, or
  peer-reflexive and media does not traverse an R2C relay.
- `Routed` when the selected candidate pair uses an R2C TURN relay.

The platform control plane may store organization membership, active-stream
metadata, request state, route type, aggregate byte counts, billing events, and
consent audit events. Live and playback media stays on WebRTC and is never
ingested or transcoded by the tracker. An explicitly approved Download may use
the tracker's private temporary archive as a bounded handoff: the tablet uploads
one recording, the tracker streams it to the requesting browser, and the copy is
deleted after successful delivery (with TTL cleanup for interrupted transfers).
MediaMTX source paths and controller URLs never appear in browser HTML or
platform-admin views.

## Request state machine

```text
pending -> probing -> awaiting_approval -> approved -> streaming -> stopped
   |          |              |               |           |
   +----------+--------------+---------------+-----------> declined
   +----------+--------------+---------------+-----------> expired
                                             +-----------> redirected
```

- `pending`: the website accepted an authorized user's request.
- `probing`: a consent-safe WebRTC data channel is measuring the candidate
  browser-to-tablet path. No controller frame may be attached.
- `awaiting_approval`: route and quality estimates are ready for the pilot/VO,
  or for the requester when the request's remote-control snapshot is enabled.
- `approved`: the authorized decision-maker selected a quality and tapped
  Start.
- `streaming`: media is flowing and both ends show a persistent viewer/data-use
  indicator.
- `stopped`: pilot/VO, viewer, timeout, or connectivity ended the session.
- `declined`: pilot/VO rejected the request. With no current viewer, the
  requester sees `insufficient bandwidth`; while another viewer is active,
  the requester sees `App already streaming to <email-addr>`.
- `expired`: the next required technical or human response did not arrive
  within 60 seconds, or an approved ten-minute media authorization ended.
- `redirected`: the pilot/VO approved a higher-priority request. The displaced
  browser tears down its media peer and shows
  `Stream redirected to <email-addr>`.

Only one request per R2C device may be `approved` or `streaming`. In the normal
pilot/VO-controlled mode, one pending replacement may be reviewed while that
session remains active. In remote-control mode, the session page identifies
the current consumer and disables Request Video until that session ends.

Every transition is server-validated, organization-scoped, and auditable.

## Presence and ordering

An enrolled device advertises at most four active streams over its existing
authenticated `/<designator>/ws/r2c` connection. Each advertisement contains:

```json
{
  "type": "video_stream_advertisement",
  "incidentName": "Training 2026-07-30",
  "streams": [
    {
      "sessionId": "generated-uuid",
      "droneDesignator": "NCS1m3",
      "sourceWidth": 1920,
      "sourceHeight": 1080,
      "sourceFps": 30,
      "sourceBitrateBps": 4000000,
      "sourceCodec": "h264"
    }
  ]
}
```

Presence expires 45 seconds after the last advertisement. The organization
page orders active records case-insensitively by incident name and then drone
designator. A focused page refreshes from lifecycle events, reconnect, or
explicit user demand; it does not periodically poll the catalog.

Presence does not depend on a stream-to-telemetry association. A newly started
camera therefore appears immediately in the authenticated tablet inventory
reached through its ephemeral `/t/<code>` link. RID2Caltopo adds CalTopo Live
Track video metadata only after a telemetry identity matches the stream, and
adds an Archived Track `/s/<code>` link only when the matching stream was
captured locally. An unmatched stream remains available through the tablet
inventory without being attached to either track type.

## Request delivery

The website creates a durable request before trying to notify the tablet. A
locally connected tablet receives:

```json
{
  "type": "video_stream_request",
  "requestId": "generated-uuid",
  "requesterEmail": "command@example.org",
  "streamSessionId": "generated-uuid",
  "incidentName": "Training 2026-07-30",
  "droneDesignator": "NCS1m3",
  "expiresAt": "2026-07-30T18:10:00+00:00",
  "consentRequired": true,
  "remoteControlEnabled": false
}
```

For a request created while Remote Video Control is advertised, both booleans
are inverted. The device performs the same consent-safe preflight but does not
show an approval prompt. After the requester selects a server-provided quality
choice, the media offer includes that selection and the device announces
`Now sharing video stream with <emailaddr>` as the existing stream/microphone
legend becomes active.

Cloud Run can place the browser request and tablet WebSocket on different
instances. The database request is therefore the source of truth. In-process
WebSocket delivery is the fast path; while streams are active, the tablet's
existing 15-second presence advertisement also replays pending requests from
the database. This provides cross-instance and reconnect delivery without a
continuously running event service. Request IDs are idempotency keys, so apps
must suppress duplicate prompts.

The first authoritative advertisement after every tablet websocket reconnect
also emits one catalog-change event even if its session IDs still fit inside
their prior presence lease. This repairs a missed browser notification without
introducing periodic tracker work.

`pending`, `probing`, and `awaiting_approval` share one maximum 60-second
request-response window. Approval starts a separate ten-minute media
authorization, so active playback is not constrained to the response deadline.

## Two-second link preflight

The estimate must measure the eventual browser-to-tablet ICE path. Measuring
browser-to-Cloud-Run HTTP traffic would answer the wrong question.

The preflight creates an encrypted WebRTC data channel without a video track:

1. ICE gathers the configured candidates. Production currently requests a TURN
   relay so setup behavior is predictable.
2. The selected candidate pair determines `Direct` or `Routed`.
3. The tablet sends paced, synthetic payload for approximately two seconds;
   the browser acknowledges sequence numbers and received bytes.
4. The tablet computes usable uplink from delivered bytes, loss, and RTT. The
   tracker applies the same bounded Android/Apple quality policy and presents
   choices to the pilot/VO or, for an enabled remote-control request, directly
   to the requester.
5. Probe payload and ICE credentials are discarded when the request ends.

No image, audio, controller packet, flight record, or location is part of the
probe.

The tracker accepts an optional `VIDEO_ICE_SERVERS_JSON` configuration
containing browser-compatible `RTCIceServer` records. The pilot deployment
uses Cloudflare's public STUN endpoint so browsers that hide host addresses
behind mDNS can still discover a direct path to the tablet. A managed TURN
service and short-lived TURN credentials remain required before Routed can be
relied upon across arbitrary cellular, Starlink, and agency firewall
combinations. Cloudflare credentials are generated per organization with an
opaque organization identifier so provider analytics can be reconciled with
the tracker's organization-scoped byte counters.

## Quality choices

The tablet should obtain actual width, height, frame rate, codec, and recent
encoded bitrate for each source. The first choice is source passthrough when it
is browser-compatible and comfortably below measured capacity. Lower-frame-rate
choices at the same resolution require a hardware encode branch; MediaMTX
relays encoded media and cannot safely create those variants by dropping
arbitrary H.264 packets.

Initial color thresholds:

- green: usable estimate is at least 1.35 times required bitrate;
- orange: usable estimate is at least required bitrate;
- red: usable estimate is below required bitrate.

Each choice also shows estimated data per minute. Red choices remain visible
for diagnosis but Start is disabled. The pilot/VO may choose lower resolution
options later; source-resolution/lower-frame-rate remains the first target.

## Media signaling and transport

The tracker relays short-lived authenticated WebRTC signaling messages only.
The browser offer is forwarded to the enrolled tablet, which attaches the
approved tablet-local source. Live and playback media currently use a forced
TURN path for fast, predictable establishment. A request authorizes one viewer
for ten minutes.
The current implementation never serves two viewers concurrently from one R2C
device; a newly approved higher-priority viewer replaces the existing viewer.

### Relay-first direct upgrade exploration

A continuous relay-to-direct transition is technically possible on the same
`RTCPeerConnection`, but it is an ICE restart rather than a second connection:

1. establish TURN-only media and wait for the first decoded frame;
2. retain the same transceivers and tracks, change the browser ICE policy to
   `all`, and create an ICE-restart offer;
3. relay that restart through the existing authenticated request to the same
   Android or Apple peer;
4. accept the answer and let ICE nominate a direct candidate pair;
5. keep the existing TURN candidate available until direct media has remained
   healthy through a bounded observation window.

This should preserve media continuity when browser and mobile WebRTC stacks
renominate successfully. It requires a versioned restart offer/answer exchange,
idempotency, rollback, and physical Android/iOS qualification across Wi-Fi,
cellular, and changing networks. It is therefore not enabled by this change.
The initial TURN route remains the production default; a later opt-in field
trial should record restart latency, selected candidate type, interruption
duration, and TURN bytes avoided before broad activation.

Android and Apple both support the production routed viewer flow. Direct
renomination, interruption/reconnect, thermal load, and sustained cellular data
still require separate physical-device qualification before relay-first direct
upgrade can be enabled.

## Incremental delivery

1. Durable org-isolated presence and request records; protected streams page.
2. Device advertisement and audible request prompt on Android and Apple.
3. Cross-instance request delivery and reconnect replay over active presence.
4. Consent-safe data-channel preflight with Direct/Routed classification.
5. Pilot quality chooser and explicit consent/stop controls.
6. WHEP signaling relay and one-viewer media session.
7. Hardware frame-rate adaptation, TURN metering, credits, and field
   qualification.

Milestone 4 is physically qualified through the routed preflight path. Browser signaling is durable across Cloud
Run instances, Android and Apple answer with a no-media peer, the tablet sends
approximately two seconds of synthetic data, and the resulting route and
usable uplink are shown at both ends. Milestone 5 now records a separate pilot
or visual-observer choice using original-size estimated frame-rate/bitrate
options. This approval does not claim that video is flowing; WHEP media relay,
viewer playback, and Stop remain milestone 6.

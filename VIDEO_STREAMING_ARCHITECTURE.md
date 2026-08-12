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
consent audit events. It must not ingest, transcode, record, or expose the media
itself. MediaMTX source paths and controller URLs never appear in browser HTML
or platform-admin views.

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
- `expired`: no decision was made within ten minutes.
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
designator. It refreshes only on user demand; it does not poll.

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

## Two-second link preflight

The estimate must measure the eventual browser-to-tablet ICE path. Measuring
browser-to-Cloud-Run HTTP traffic would answer the wrong question.

The preflight creates an encrypted WebRTC data channel without a video track:

1. ICE gathers direct candidates and the configured TURN fallback.
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
combinations.

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

The tracker relays short-lived authenticated WHEP/ICE signaling messages only.
The browser offer is forwarded to the enrolled tablet, which exchanges it with
the tablet-local MediaMTX WHEP endpoint. Media then follows the selected ICE
path directly or through TURN. A request authorizes one viewer for ten minutes.
The current implementation never serves two viewers concurrently from one R2C
device; a newly approved higher-priority viewer replaces the existing viewer.

Android already enables MediaMTX WebRTC, but its host/candidate and TURN
configuration is not yet field-qualified. Apple currently disables MediaMTX
WebRTC and needs equivalent configuration plus physical-device qualification.
Neither platform should advertise production remote video until direct NAT,
TURN fallback, interruption/reconnect, thermal load, and sustained cellular
data tests pass.

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

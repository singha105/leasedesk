# Protocol notes

What the live Switch server actually does, recorded from the Part 1 spikes. Each entry has the
observation time (ET), a raw example (secrets redacted, long strings cut with `…`) and the
takeaways. Where an entry still says **PENDING**, it has not been seen on the wire yet; do not
build on it.

Server under test: switch-core 0.31.0, run natively by Switch Console 0.40.1 on macOS
(`get_my_context` reports `server.kind = "managed_by_console"`).
Bridge base URL: `http://localhost:57637` (the `api` port in Console's ports.json).
Raw sources: `logs/frames.jsonl` (every WebSocket frame), `logs/http.jsonl` (every HTTP call),
`docs/switch-ops.json` (live `GET /agents/{id}/ops`). The logs are gitignored.

## Health and base URL

### GET /health
Observed: 2026-11-03 23:26
Raw: `GET http://localhost:57637/health` -> `200 application/json {"status":"ok"}` (server `uvicorn`).
Takeaways: 57637 is the bridge. 57636 (Console calls it `gateway`) answers 404 text/plain to
everything; 57638 is Mattermost (HTML). `GET /` on the bridge is 401. The Gateway is an API
under `/gateway` on the bridge port with cookie auth; there is no Gateway web page on a native
local server (see DECISIONS 2026-11-03).

## Registration

### POST /agents, success
Observed: 2026-11-03 23:43
Raw request: `{"name": "leasedesk-probe", "description": "LeaseDesk protocol probe (Part 1 spikes)",
"connector_type": "leasedesk-python", "integration_profile": {"connection_model": "always_on",
"message_exchange": true, "pre_invocation_mediation": [], "post_invocation_mediation": [],
"event_reporting": []}}` with `Authorization: Bearer <registration key>`
Raw response: `200 {"id": "23e2b36d-e384-42bd-87f0-751d6fccf81b", "api_key": "[REDACTED]"}`
Takeaways: status is 200, not 201. Exactly two keys. The key is 43 chars (token_urlsafe(32)).

### POST /agents, name taken
Observed: 2026-11-03 23:43
Raw: `409 {"detail": "Agent already exists: 'leasedesk-probe'. Pass overwrite=True to re-register (rotates API key, replaces integration profile)."}`
Takeaways: matches the docs. Registration is a one-time step; Part 2's `make register` must
treat 409 as "already registered, use the saved key".

### Side effects of registering
Observed: 2026-11-03 23:43
Raw (`read_context` in Mattermost Off-Topic): `room_join` by `@switch-agent-21ae68a7:localhost`
("leasedesk-probe joined the room"), `room_join` by `@switch-admin:localhost`, then a message
from the probe's own id: `"Hey! Ping @leasedesk-probe and I'll jump in."`
Takeaways: a new agent is auto-added to the bundled Mattermost rooms and Switch posts a greeting
in its name. `list_rooms` shows both Mattermost rooms with `connected: false`.

### Addressing (who may instruct the agent)
Observed: 2026-11-03 23:43
Raw: `POST /agents/{id}/ops/update_agent_detail {"agent_id": "…", "addressing": "anyone"}` ->
`403 {"detail": "An agent can't change its own settings, permissions or services. Your owner changes them on your page in Switch Console."}`
Default after registration (`get_agent_detail`): `"addressing_policy": {"rules": [{"rooms": "*",
"room_groups": "*", "users": [], "agents": [], "owner": true, "owner_agents": true, "platform": false}]}`,
`"owner_name": "Admin"`.
Takeaways: **differs from the docs**, which say the agent's own key can set addressing. The owner
must do it: Console's agent page, or `PUT /gateway/agents/{id}/addressing-policy {"policy": null}`
(null = open to anyone), wrapped by `scripts/gateway.py addressing`.

## Operations

### GET /agents/{id}/ops
Observed: 2026-11-03 23:44
Raw: `200 {"operations": {"<name>": {"description": "…", "input_schema": {…}}, …}}`, 57 operations,
saved verbatim to `docs/switch-ops.json`.
Takeaways: every operation the spec names exists. Ops the docs catalog doesn't list:
`create_agent`, `create_room_from_yaml`, `delete_template`, `get_advanced_config`,
`get_my_context`, `get_template`, `get_template_guide`, `list_all_references`, `list_machines`,
`list_managed_agents`, `list_templates`, `remove_agent_from_room`, `remove_agent_service`,
`request_agent_deletion`, `run_template`, `save_template`, `set_agent_service`,
`update_template`. Every op in the docs catalog is present (including `create_room`,
`delete_room_document`). There is **no op that lists documents** (see Documents).

### Calling an op
Observed: 2026-11-03 23:45
Raw: `POST /agents/{id}/ops/connect_to_room` with `X-Switch-Connection-Id: <live connection id>`
and body `{"room_id": "6cc9…"}` -> `200 {"result": {…}}`. Errors seen: 400 `{"detail": "…"}` for bad
targets or unknown document ids, 403 for self-settings.
Takeaways: arguments are unwrapped; results are wrapped in `result`; errors are FastAPI
`{"detail": …}`.

### Input schemas we depend on
Generated from docs/switch-ops.json by `scripts/spikes/ops.py summarize`.

#### `post_message`
Send a message to the connected room (broadcast, no specific recipient).

| arg | type | required | default |
| - | - | - | - |
| `body` | string | yes |  |
| `thread_id` | string or null |  | null |

#### `send_targeted_message`
Send a message addressed to specific agents/users and/or roles.

| arg | type | required | default |
| - | - | - | - |
| `body` | string | yes |  |
| `target_names` | array[string] or null |  | null |
| `target_roles` | array[string] or null |  | null |
| `thread_id` | string or null |  | null |

#### `read_context`
Get a room's conversation timeline, grouped into threads.

| arg | type | required | default |
| - | - | - | - |
| `before` | string or null |  | null |
| `limit` | integer |  | 50 |
| `room_id` | string or null |  | null |
| `since` | string or null |  | null |

#### `list_participants`
List agents and users in the connected room.

(no arguments)

#### `connect_to_room`
Connect this session to a room. The agent must be assigned to the room.

| arg | type | required | default |
| - | - | - | - |
| `include_general_instructions` | boolean |  | true |
| `room_id` | string | yes |  |

#### `list_rooms`
List rooms this agent is assigned to.

| arg | type | required | default |
| - | - | - | - |
| `include_archived` | boolean |  | false |

#### `create_room_document`
Create a new internal document scoped to the connected room.

| arg | type | required | default |
| - | - | - | - |
| `content` | string | yes |  |
| `description` | string | yes |  |
| `instructions` | string | yes |  |
| `name` | string | yes |  |

#### `update_room_document`
Update a room-scoped document you created in the connected room.

| arg | type | required | default |
| - | - | - | - |
| `content` | string or null |  | null |
| `description` | string or null |  | null |
| `document_id` | string | yes |  |
| `instructions` | string or null |  | null |
| `name` | string or null |  | null |

#### `load_internal_documents`
Load the content of internal documents attached to the connected room.

| arg | type | required | default |
| - | - | - | - |
| `ids` | array[string] | yes |  |

#### `assume_role`
Assume a role in the connected room and receive its instructions.

| arg | type | required | default |
| - | - | - | - |
| `role` | string | yes |  |

#### `release_role`
Release the role you currently hold, freeing it for others. Idempotent.

(no arguments)

#### `list_roles`
List the roles defined in the connected room.

(no arguments)

#### `define_role`
Define a new role in the connected room. Requires write access to the room.

| arg | type | required | default |
| - | - | - | - |
| `exclusive` | boolean |  | false |
| `instructions` | string | yes |  |
| `name` | string | yes |  |

#### `link_rooms`
Create a directed link from one room to another.

| arg | type | required | default |
| - | - | - | - |
| `label` | string | yes |  |
| `source_room_id` | string | yes |  |
| `target_room_id` | string | yes |  |

#### `invite_agent_to_room`
Invite an agent into an existing room by name.

| arg | type | required | default |
| - | - | - | - |
| `agent_name` | string | yes |  |
| `include_subagents` | boolean |  | false |
| `room_id` | string | yes |  |

### Replying inside a thread
Observed: 2026-11-03 23:46
Raw: `post_message {"body": "…top-level…"}` -> `{"result": {"event_id": "sw_8988…"}}`, then
`post_message {"body": "LeaseDesk probe: reply inside the thread", "thread_id": "sw_8988…"}` ->
`{"result": {"event_id": "sw_a3e4…"}}`. `read_context` then shows the reply under that root:
`{"threads": [{"root": {"id": "sw_8988…", "kind": "message", …}, "replies": [{"id": "sw_a3e4…", …}]}]}`.
Takeaways: `thread_id` is the root message's event id (`sw_…`), and the field is `thread_id`
(not `reply_to`). keymaster opens a lease thread by posting the lease card top-level and
keeping its `event_id`.

### send_targeted_message targets
Observed: 2026-11-03 23:46
Raw: `{"body": "…", "target_names": ["admin"], "thread_id": "sw_8988…"}` ->
`400 {"detail": "Targets not in room: admin. Room participants: leasedesk-probe."}`
Takeaways: targets must be current room participants by name. A person who has only a
`room_join` in the history is not necessarily a participant.

### POST /agents/{id}/message (no connection)
Observed: 2026-11-03 23:46
Raw: `{"room_id": "6cc9…", "content": "…"}` -> `200 {"ok": true, "event_id": "sw_dc9f…"}`
Takeaways: this route uses `content`, while the `post_message` op uses `body`. Works with no
connection id. No `thread_id` was tried here.

## WebSocket frames

Open: `ws://localhost:57637/agents/{id}/connection/ws?connection_id=<uuid>&scope=all&filter=all`
with `Authorization: Bearer <agent key>`. Every frame is `{"event": "<kind>", "data": {…}}`;
domain events add a top-level `"id"`.

### connection_state
Observed: 2026-11-03 23:44
Raw: `{"event": "connection_state", "data": {"connection_id": "4255…", "agent_id": "23e2…",
"generation": 8589934592, "scope": "all", "filter": "all", "spawn_capable": false, "rooms": [],
"cursor": 0, "position": {"s": 4294967296, "r:6cc9…": 4, "r:52f7…": 4}, "protocol": 9,
"heartbeat_interval_seconds": 2.0, "server": {"version": "0.31.0", "contracts": {"agent-protocol":
{"speaks": 9, "accepts": 1}}}, "client": {"speaks": null, "accepts": null, "artifact": null, "version": null}}}`
Takeaways: first frame, as documented. Extra field `position` (per-room read positions, `r:<room_id>`)
is not in the docs. Protocol revision 9. Generations are large ints and go up by one per attach.
`rooms` is empty with scope=all until a room is claimed.

### ping and pong
Observed: 2026-11-03 23:44
Raw: server `{"event": "ping", "data": {}}` every 2 s; client `{"type": "pong", "cursor": 0}`.
Takeaways: the documented pong shape keeps the connection alive (30 s runs with no eviction).

### evicted: heartbeat_lapsed
Observed: 2026-11-03 23:44 (pongs muted on purpose)
Raw: `{"event": "evicted", "data": {"code": "heartbeat_lapsed", "reason": "heartbeat lapsed; reopen the stream and resume from your cursor", "room_id": null}}`
Takeaways: arrived about 7 s after the last pong. Reopening with the same connection id,
`start_from=<cursor>` and `expected_generation=<last generation>` worked and returned a new
`connection_state` with generation + 1.

### evicted: taken_over
Observed: 2026-11-03 23:44 (second socket opened with the same connection id)
Raw: `{"event": "evicted", "data": {"code": "taken_over", "reason": "another stream attached to this connection and took it over", "room_id": null}}`
Takeaways: final; the listener stops and does not reconnect.

### gap
Observed: 2026-11-03 23:44 (`start_from=999999`, far past the head)
Raw: `{"event": "gap", "data": {"from_sequence": 999999, "resumed_at": 9007194959773696,
"resumed_position": {"s": 4294967296, "r:6cc9…": 4, "r:52f7…": 4}, "rooms": [], "all_rooms": true,
"reason": "the server restarted since your last connection; events from before the restart are gone in every room, including any this connection has yet to claim — re-read room context"}}`
Takeaways: a cursor the server doesn't know is reported as a restart gap with `all_rooms: true`.
`resumed_at` is a large number (2^53 − 2^32), so cursors are opaque ints: store them, never do
arithmetic on them. After a gap, re-read room history with `read_context`.

### subscription_changed
Observed: 2026-11-03 23:45 (after `connect_to_room` with the listener's connection id)
Raw: `{"event": "subscription_changed", "data": {"rooms": ["6cc94957-397c-4b5d-aa98-c4252bf9bb79"], "reason": "subscription updated"}}`
Takeaways: claiming a room through the op shows up on the socket as this frame.

### An agent's own messages
Observed: 2026-11-03 23:46
Takeaways: the probe's own `post_message` and `/message` posts were **not** delivered back on its
own socket. keymaster doesn't need to filter its own echoes, but must not rely on seeing them.

### Being invited is not being subscribed
Observed: 2026-11-04 00:15 (`!invite-agent @leasedesk-probe` typed in Discord #leasedesk-scratch)
Raw: server log `Added 1 agents to room a4580f7c…`; nothing arrived on the probe's socket (no
`room_join`, no `message`). `list_rooms` then showed the room with `"connected": false`.
After `connect_to_room {"room_id": "a4580f7c…"}`: `{"event": "subscription_changed", "data":
{"rooms": ["a4580f7c-088f-4a09-bc04-a1049657d105"], "reason": "subscription updated"}}`.
Takeaways: `scope=all` does not mean "every room I'm in". A socket hears only rooms it has
claimed. `connect_to_room` **replaces** the subscription: the Mattermost room the probe had
claimed dropped out of `rooms`. An agent that serves several rooms needs either one connection
per room or the `rooms=` query parameter at open (to try in Part 2). It also has to poll
`list_rooms` (or re-connect on a timer) to notice a new invitation, because no frame announces it.

### Not seen yet
PENDING: `command`, `room_join` on the socket, `refused`, `room_released`, and close codes
4401/4409. `room_join` shows up only in `read_context` history (kind `room_join`).

## Message variants (Discord)

Room `a4580f7c-088f-4a09-bc04-a1049657d105` ("LeaseDesk Demo: leasedesk-scratch", bridge
`36e05d34-8d5d-4564-9f88-86e9775fb038`). Agent name autocomplete is on, so `@leasedesk-probe`
in Discord is a role mention (role 1567406527541805096) that the bridge rewrites to the
agent's name in `body`.

### Creating the room
Observed: 2026-11-04 00:15
Raw: the first message in a fresh channel auto-creates the room (`Auto-created channel_public
room … for LeaseDesk Demo channel 1567404542956539976`). The bridge posts "I've linked this
channel to a new Switch room, but there are no agents in it yet…". `!invite-agent @leasedesk-probe`
is run as the linked person (`admin`) and answered in a Discord thread: "Added
**leasedesk-probe** to this room." In history the command message has `"body": null`. Joining
makes the server post the agent's greeting "Hi! I'm here to help — mention @leasedesk-probe
anytime." in the agent's name (as on Mattermost).
Takeaways: the room id is only known after the first message. Part 2's setup script should
read it with `list_rooms` instead of hardcoding it. Expect the automatic greeting in the room.

### Mentioned, top level
Observed: 2026-11-04 00:16
Raw: `{"event": "message", "id": 9007194959773697, "data": {"type": "message", "room_id": "a4580f7c…",
"bridge_id": "36e05d34…", "channel_type": "channel_public", "payload": {"addressed": true,
"sender": "@switch-discord-36e05d34-8d5d-4564-9f88-86e9775fb038-piggyback101:localhost",
"sender_name": "piggyback101.", "sender_kind": null, "on_behalf_of": null,
"message_id": "sw_d8dc9d3bb835a777136eeb3e9b8cf00c", "body": "@leasedesk-probe hello from Arnab",
"timestamp": 1793769373247, "thread_id": null, "attachments": [], "opening_message": null,
"sender_role": "owner"}, "sequence": 9007194959773697, "missed": {"count": 0, "reason": null}}}`
Takeaways: `addressed` is the mention flag. `sender_role: "owner"` marks the agent's owner (the
linked account). `data.sequence` equals the top-level `id` (the cursor). `missed` appears only
on addressed messages.

### Not mentioned
Observed: 2026-11-04 00:16
Raw: same shape with `"addressed": false`, `"body": "just chatting, no mention here"`,
`"sender_role": null`, no `missed` key.
Takeaways: with `filter=all` every message arrives, so agents must check `addressed`
themselves. `sender_role` is filled in **only on addressed messages**, so ownership can't be
read off a plain message.

### Thread reply (person)
Observed: 2026-11-04 00:17 (Discord "Create Thread" on the mentioned message, first post inside)
Raw: `"addressed": true, "message_id": "sw_bf1b880bfcb86873169640c552c61cf8",
"body": "@leasedesk-probe reply in thread", "thread_id": "sw_d8dc9d3bb835a777136eeb3e9b8cf00c",
"sender_role": "owner", "missed": {"count": 1, "reason": null}`
Takeaways: a Discord thread maps to `thread_id` = the root message's `message_id`. `missed.count`
was 1 here. It looks like the number of room messages since the agent's last addressed one
("just chatting"), but that is not confirmed.

### Thread reply (agent) and top-level post
Observed: 2026-11-04 00:17
Raw: `post_message {"body": "probe: thread reply via post_message (spike)", "thread_id": "sw_d8dc…"}`
-> `{"event_id": "sw_0597…"}` and it appeared **inside** the Discord thread. Without `thread_id`
it appeared in the channel. Neither came back on the probe's socket.
Takeaways: keymaster can keep a lease's whole conversation in one Discord thread by passing
the root's `message_id`.

### Targeted message to a person
Observed: 2026-11-04 00:18
Raw: `send_targeted_message {"body": "…", "target_names": ["piggyback101."]}` ->
`200 {"result": {"event_id": "sw_16ab…", "target_statuses": {}}}`. Discord shows it as
"@piggyback101 probe: targeted message…" (a real user mention).
Takeaways: the target name is the Switch display name, **trailing dot included**
(`piggyback101.`), not the Discord username. Read names from `list_participants`; don't type them.

### With a file
Observed: 2026-11-04 00:17 (spike-attachment.txt, 89 bytes, plus a mention)
Raw: `"body": "@leasedesk-probe here is a file", "attachments": [{"filename": "spike-attachment.txt",
"mimetype": "text/plain", "size": 89, "mxc": "switch-media://52cff66aea0c41c5b3a61f6bd1783dd6",
"msgtype": "m.file"}]`. Bytes: `GET /agents/{id}/rooms/{room_id}/media?mxc=switch-media://…` ->
`200 text/plain; charset=utf-8`, 89 bytes, `Content-Disposition: inline; filename="spike-attachment.txt"`
(this route is not in GET /ops; found in switch-core's agent API, `scripts/spikes/post.py media`).
Takeaways: attachments are delivered as references, never inline. **The `room_id` in the media
path is not checked against where the file was posted**: the same `mxc` fetched through the
Mattermost room (also a probe room) returned the file too. LeaseDesk should never pass an `mxc`
it read in one room into another room's flow.

### From a second person
NOT DONE in Part 1: there is no second Discord account in LeaseDesk Demo yet. The probe's
addressing is now `null` (anyone), so a second person's mention should arrive with
`addressed: true` and some other `sender_role` (to record when a second account joins;
the skeptic/approver tests in Part 3 need it anyway).

## Documents

### create_room_document
Observed: 2026-11-03 23:47
Raw: `{"name": "probe-ledger", "description": "…", "instructions": "…", "content": "| lease | state |…"}`
-> `200 {"result": {"document_id": "19e918d7-ec99-4bb1-8f7b-125e7086ae34"}}`
Takeaways: all four fields are required.

### update_room_document
Observed: 2026-11-03 23:47
Raw: `{"document_id": "19e9…", "content": "…ACTIVE…"}` -> `200 {"result": {"document_id": "19e9…", "status": "ok"}}`.
Unknown id -> `400 {"detail": "Room-scoped document 00000000-… not found in room 6cc9…"}`.
Takeaways: partial update (only the fields you pass). It replaces the content; an append-only
ledger means keymaster rewrites the full table each time.

### Listing and loading documents
Observed: 2026-11-03 23:47
Raw: `connect_to_room` result `"documents": [{"id": "19e9…", "name": "probe-ledger", "description": "…",
"instructions": "…", "scope": "room", "created_by_agent_id": "23e2…"}]`;
`load_internal_documents {"ids": ["19e9…"]}` -> `{"result": [{"room_seq": 0, "source_event_id": null,
"id": "19e9…", "name": "probe-ledger", "description": "…", "content": "…"}]}`
Takeaways: the document list (metadata only) comes from `connect_to_room` or from
`list_references` (added 2026-11-04: it returns `{reference_types, references, documents,
packages, linked_rooms}` for the connected room). Then `load_internal_documents` fetches
content by id.

### An agent updating a person-written document
Observed: 2026-11-04 00:23 (library doc `human-note`, id `7bc268d5-dddf-4841-b985-d536683db4fb`,
written by the admin and attached to the Discord room)
Raw: to the probe it is `{"id": "7bc2…", "name": "human-note", "scope": "global",
"created_by_agent_id": null, …}` in `connect_to_room` and `list_references`, and
`load_internal_documents` returns its content. Both
`update_room_document {"document_id": "7bc2…", "content": "…"}` and
`{"document_id": "7bc2…", "description": "…"}` ->
`400 {"detail": "Room-scoped document 7bc268d5-… not found in room a4580f7c-…"}`. A re-read showed
the content and description unchanged.
Takeaways: agents can read a person's library doc but can't write it, because the only write
ops (`update_room_document`, `delete_room_document`) act on room-scoped docs the agent
created. The refusal is a 400 "not found", not a 403, so code must not treat the
error text as a permission signal. `scope` (`global` vs `room`) and `created_by_agent_id`
(null = a person) say who owns a doc. Attaching the doc produced no frame on the socket.
Not tried: `delete_room_document` on it (a working delete would be permanent).

### How a person creates a library document and attaches it
Observed: 2026-11-04 00:19 (Console 0.40.1, room Configuration tab)
Raw: the room page offers Description, Instructions, Participants (+ Add agent, From a template),
Capture as template and Delete room. There is **no documents section**, and Console hides the
Gateway admin UI on a native local server.
Takeaways: a person creates and attaches a library doc through the Gateway API:
`POST /gateway/documents {name, description, instructions, content, read_visibility,
write_visibility}` then `POST /gateway/rooms/{room_id}/documents/{document_id}`, signed in as
the Gateway admin. `scripts/gateway.py library-doc` wraps both.
Observed: 2026-11-04 00:23
Raw: `POST /gateway/documents {"name": "human-note", "description": "…", "instructions": "…",
"content": "…", "read_visibility": "public", "write_visibility": "private"}` -> `201 {"id": "7bc2…",
"owner_id": "28cff49c-1376-45e0-8d41-9489104d35d0", "owner_name": "Admin", "read_visibility": "public",
"write_visibility": "private", "name": …, "content": …, "attached_rooms_count": 0, "packages": [],
"scope": "global", "room_id": null, …}`; `POST /gateway/rooms/a4580f7c…/documents/7bc2…` -> `201`
with the same document and `attached_rooms_count: 1`.
Takeaways: Part 4's person-made library docs (policy text, runbooks) follow this path. The
admin user id `28cff49c-…` is also the probe's `owner_id`.

### Addressing set to anyone (owner step)
Observed: 2026-11-04 00:23
Raw: `PUT /gateway/agents/23e2…/addressing-policy {"policy": null}` -> `200 {…, "owner_id": "28cff49c…",
"owner_name": "Admin", "addressing_policy": null, "agent_type": "always_on", …}`
Takeaways: the owner path works, as planned for `make register` in Part 2.

## Participant ids

Two id spaces, and messages use only one of them:

| who | `list_participants` / `connect_to_room` id | message `sender` | name |
| - | - | - | - |
| leasedesk-probe (agent) | `23e2b36d-e384-42bd-87f0-751d6fccf81b` (agent UUID) | `@switch-agent-21ae68a7:localhost` | `leasedesk-probe` |
| Arnab on Discord (linked to admin) | `1f8da64d-f8d2-49d0-8ac7-6ff1853e11a6` | `@switch-discord-36e05d34-8d5d-4564-9f88-86e9775fb038-piggyback101:localhost` | `piggyback101.` |
| Gateway admin (Console) | not a participant | `@switch-admin:localhost` | `admin` |
| Discord bridge | not a participant | `@switch-bridge-discord-leasedesk-demo-edddeb86:localhost` | `bridge-discord-LeaseDesk Demo` |

Observed: 2026-11-04 00:18.
Takeaways for the approver rule (spec: "a named human approves"): match the approver on the
message `sender` string exactly, as an opaque id. **That string embeds the bridge id**:
disconnecting and reconnecting Discord (done once on 2026-11-04 after a bad token) made a new
bridge `36e05d34…` in place of `b45dbd45…`, so every Discord sender id changes when the app is
reconnected. LeaseDesk's approver list must be re-read after any reconnect, never baked into config.
Participant UUIDs from `list_participants` don't appear in message frames.
PENDING: the second account's ids.

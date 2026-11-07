# Building a just-in-time AWS access desk on Switch: four agents, two rooms, one template

*Draft. To be rewritten in my own voice before publishing.*

LeaseDesk lets an engineer ask for AWS access in a chat room in plain English. One agent drafts the IAM policy, two others attack it, a named person approves, and the access ends at a time written into the policy, so AWS cuts it off even if every agent is down. I built it on Switch during Flip the Switch. This post is what I wish I'd known on day one.

## What Switch is

Switch puts AI agents and people in the same chat rooms. A **room** is a channel in Discord, Slack, Mattermost, Telegram or Teams, bridged to a Switch server. An **agent** is any program that joins rooms: a coding agent such as Claude Code or OpenCode that Switch Console runs for you, or your own code speaking the agent protocol. Agents act only on messages that address them, and they hand work to each other the same way people do, by mentioning a name. A **role** is a job in a room (exclusive or shared) that an agent takes, so you can address `@reviewer` instead of a particular agent. **Documents** carry the room's shared context: library documents written by people in the Gateway, room documents an agent writes, references to things like a GitHub repo, and packages that bundle them. A **template** recreates all of it in one run.

## Running Switch locally and connecting Discord

Switch Console 0.40 runs a server on your computer with no Docker. Docker only adds the bundled Mattermost. Console signs you in to the Gateway and keeps the admin password itself.

Connecting Discord takes a bot token, and two things caught me:

- **Link your own account.** When you add Discord, Console offers to link your Discord account to your Switch user ("This is me"). Do it. Without the link, an agent that answers only its owner can't tell that your Discord messages come from you, and it looks like the agent is ignoring you.
- **Reconnecting changes everyone's ids.** A Discord sender id embeds the bridge id, so if you disconnect and reconnect Discord (Console can't edit a token, so a bad paste forces a reconnect), every person's id changes. LeaseDesk's approver list keys on those ids, so I added `@keymaster whoami` and re-read the policy after any reconnect. Check the token before you paste it into Console.

## Writing a custom agent on the agent protocol

keymaster and sentinel are plain Python, no LLM: keymaster holds the only AWS write credentials, and sentinel is a rules engine. They speak the agent protocol directly: a WebSocket for events, HTTP for operations. Six rules kept them alive.

**Answer pings first.** The server pings every 2 seconds and evicts you about 7 seconds after the last pong. If your handler is slow and the same task reads the socket, you get evicted mid-work. LeaseDesk's reader task answers pings the moment it reads them and puts domain events on a queue that a separate worker drains:

```python
if isinstance(frame, Ping):
    await ws.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
    return None
if isinstance(frame, DomainEvent):
    self._queue.put_nowait(frame)
    return None
```

**Reuse the connection id.** After a `heartbeat_lapsed` eviction, reopen with the same `connection_id`, `start_from=<cursor>` and `expected_generation=<last generation>`, and you resume where you stopped. Store the cursor as an opaque number; it can be around 2^53, so never do arithmetic on it.

**Treat `taken_over` as final.** If a second socket attaches with your connection id, the first one gets `evicted` with code `taken_over`. Don't reconnect, or two copies of your agent fight forever:

```python
if frame.code == "taken_over":
    self.log.error("evicted_taken_over", reason=frame.reason)
    return ExitReason.TAKEN_OVER
```

**Act only on addressed messages.** Every message frame has an `addressed` flag. keymaster reads every message in its rooms but acts only on the addressed ones. One surprise: when an agent sends a targeted message, Switch prefixes `@name ` to the body, so a sender that also writes the mention produces `@sentinel @sentinel check`. Our parser strips repeated leading mentions.

**Send op arguments unwrapped.** An operation is `POST /agents/{id}/ops/{op}` with the connection id in a header, and the body is the arguments themselves, not `{"arguments": …}`. Results come back wrapped in `result`:

```python
headers = {"X-Switch-Connection-Id": connection_id}
r = await client.post(f"/agents/{agent_id}/ops/{op}", json=args, headers=headers)
return r.json().get("result")
```

**Trust the live `GET /ops` over the docs.** `GET /agents/{id}/ops` lists every operation with its input schema. On my server it had 57, including 18 the docs catalog didn't list, and some docs details were off (there's no op that lists documents; `list_references` returns them). I saved the output to the repo and wrote the client against it.

Three more things I learned the hard way:

- **One connection per room.** `connect_to_room` replaces a connection's subscription, so an agent in two rooms needs two connections.
- **Invitations are silent.** Being invited to a room sends no frame. Poll `list_rooms` to notice new rooms, and re-read `list_participants` before deciding whether a sender is an agent.
- **Agents run by Console wake only when addressed.** When keymaster refused scribe's lease with a plain thread post, scribe never saw it and waited forever. Replies to an agent now go to it by name, in the same thread.

## People-owned documents and swappable roles

Two Switch features carry LeaseDesk's security model.

**Documents a person wrote are a trust boundary.** The access policy (who may lease what, who approves) and the resource catalog (what's a crown jewel) are library documents written by a person in the Gateway. Agents can read library documents but can't edit them: the only write operations work on room documents the agent created itself. Each document says who made it, so keymaster and sentinel read only documents no agent created:

```python
for d in refs.all_documents():
    if d.name == name and d.created_by_agent_id is None:
        found.setdefault(d.id, session.connection_id)
```

A prompt-injected scribe can write whatever it likes in the room, and it still can't change who may approve. keymaster also writes a room document of its own, `lease-ledger`, as an audit trail.

**Roles survive swapping agents.** keymaster sends review requests to `@reviewer`, not to sentinel by name. sentinel and skeptic share the reviewer role, so either can be replaced, or a third reviewer added, without touching keymaster. The executor role is exclusive, so only one agent can ever be the one that touches AWS. One catch: an agent can hold a role in only one room at a time, so LeaseDesk defines roles only in #access-desk.

## Templates as one-click setup

`switch/leasedesk.template.yaml` creates a room group, #access-desk (public) and #security-review (private), the `escalation` link between them, the three roles, the room instructions, a how-to-ask document, a kickoff message, and the two agents Console runs (scribe on Claude Code with Sonnet, skeptic on OpenCode with a free model), each with its brief.

Things to know:

- The file is generated from the agent briefs (`make template`), and `make doctor` checks that the copy stored in Switch is byte-identical to the one in the repo.
- `POST /gateway/templates/validate` checks a template without storing it, but it doesn't flag unknown keys, so validation passing isn't the same as the run doing what you meant.
- Packages and library documents aren't part of the template; attach the governance package after the run.
- Console's Use form wants every agent provider signed in. The server's own run endpoint is more lenient, which is what `scripts/gateway.py template-run` uses.

## What I'd do next

- Lease IAM Identity Center permission sets instead of writing inline policies on IAM users.
- Run the same rooms on Slack and Teams through Switch's other bridges.
- Put Switch Trust guardrails on scribe's traffic.
- Build periodic access reviews from the lease ledger.
- Use native Switch approval cards for custom agents, if the protocol exposes them.

The code, the protocol notes and every spec-versus-server difference I hit are in the repo: https://github.com/singha105/leasedesk

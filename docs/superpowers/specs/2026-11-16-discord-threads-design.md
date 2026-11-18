# Discord bots answer in threads

Approved by Spyros on 2026-11-16.

## Intent

In `#media` and `#windmill`, every new message becomes its own conversation. People find a topic again by its thread instead of scrolling one long channel. When someone asks about a topic that already has a thread, the bot sends them to that thread instead of starting a second conversation about it.

## Behavior

1. Each new top-level message in the channel opens a thread on that message. The bot answers only inside that thread.
2. The bot never posts in the main channel, never posts in another thread and never creates a thread itself.
3. A topic is a thread, whoever started it. Counting starts at the deploy: both channels had 0 open and 0 closed threads on 2026-11-16, so every thread that exists later counts, open or closed.
4. On the first message of a new thread, the bot checks the channel's other threads. If one already covers the same topic, it replies once with a clickable link (`<#thread-id>`) to that thread and says it will not reply here.
5. After that redirect, the bot stays silent in that thread for every later message.
6. In every other thread, the bot answers every message, as it does today.

Same topic means:

- `#media`: the same movie, series, album or artist.
- `#windmill`: the same Windmill script or flow.

## Non-goals

- No locking, archiving or deleting of threads. That needs Discord's Manage Threads permission, which the bots do not get.
- No deterministic duplicate check. Deciding that two messages share a topic needs the model either way.

## Approaches

1. **OpenClaw settings plus `AGENTS.md` rules (picked).** `autoThread` already creates one thread and one session per message. The model runs the duplicate check with the `openclaw message thread list` and `openclaw message read` commands. One `silentReply` setting allows silence.
2. **A custom OpenClaw plugin or hook.** More code to own through every OpenClaw upgrade, and it still needs a model call to compare topics.
3. **A separate duplicate-check service.** A new deployment and a new failure point for the same model judgment.

## Design

- `media/openclaw.json` and `windmill/openclaw.json`, channel entry: `"autoThread": true`, `"autoThreadName": "generated"`, `"autoArchiveDuration": 10080`.
- Both files: `agents.defaults.silentReply.group: "allow"`. Without it OpenClaw requires a visible reply to every accepted channel message, and `NO_REPLY` cannot waive it.
- Both `AGENTS.md` files get a "Threads" section with the rules above and the exact list and read commands. The commands run in the Gateway container, which holds `DISCORD_GUILD_ID` and `DISCORD_CHANNEL_ID`.
- `tools/manifests.test.py` pins the settings and the section.
- `README.md` documents the thread behavior and the owner's permission step.

## Owner step

Give both bots **Create Public Threads** and **Send Messages in Threads** in their channel.

## Risks

- R1: with silence allowed, the model could skip a message it should answer. The `AGENTS.md` rule limits silence to redirected threads.
- R2: the duplicate check is model judgment. It will sometimes miss a duplicate or flag one wrongly.
- R3: `autoThreadName: "generated"` costs one small model call per thread through CLIProxy.

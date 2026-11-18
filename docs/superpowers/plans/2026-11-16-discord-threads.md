# Discord bots answer in threads: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `#media` and `#windmill` open one thread per message, answer only inside it, and send duplicate topics to the existing thread once, then stay silent there.

**Architecture:** OpenClaw's Discord `autoThread` setting creates the thread and a separate session per thread. A "Threads" section in each `AGENTS.md` tells the model how to find duplicates with the `openclaw message` CLI. `silentReply.group: "allow"` lets it stay silent after a redirect.

**Tech Stack:** OpenClaw 2026.11.3 config (JSON), Markdown agent rules, Python `unittest` in `2-k3s/35.discord/tools/manifests.test.py`.

**Spec:** `docs/superpowers/specs/2026-11-16-discord-threads-design.md`

## Global constraints

- Change only `2-k3s/35.discord/` and nothing else. The spec and this plan are already written.
- The repository is public. No Discord IDs (17 to 20 digit numbers), names or secrets in any file. `test_no_discord_ids_in_plaintext` enforces the IDs.
- `autoArchiveDuration` is the number `10080`, not a string.
- Keep the existing JSON style: two-space indent, one-line channel entry objects.

## Review focus

1. A redirected thread gets a later message: the bot must stay silent. The `AGENTS.md` copy in Task 2 pins it, and its test checks the copy.
2. A normal thread gets an unclear or off-topic message: the bot must still answer. The copy says `NO_REPLY` only in redirected threads, and the test checks that sentence.
3. More than one page of closed threads: the copy pages with `--before <nextBefore>` while `hasMore` is true, and the test checks it.
4. The bot's own current thread shows up in the open list: the copy says to skip it.
5. OpenClaw rejects an unknown key and the agent stays down (`render-config` fails). Task 1 validates the rendered config with the live image's `openclaw config validate`.

---

### Task 1: Thread settings in both `openclaw.json` files

**Files:**
- Modify: `2-k3s/35.discord/media/openclaw.json`
- Modify: `2-k3s/35.discord/windmill/openclaw.json`
- Test: `2-k3s/35.discord/tools/manifests.test.py` (`AgentChecks.test_openclaw_config`)

**Interfaces:**
- Produces: channel entry `{"enabled": true, "requireMention": false, "roles": ["${DISCORD_ROLE_ID}"], "autoThread": true, "autoThreadName": "generated", "autoArchiveDuration": 10080}` and `agents.defaults.silentReply = {"group": "allow"}` in both files.

- [ ] **Step 1: Write the failing test.** At the end of `test_openclaw_config`, add:

```python
        self.assertEqual(config["agents"]["defaults"]["silentReply"], {"group": "allow"})
        channel = channels["__DISCORD_CHANNEL_ID__"]
        self.assertIs(channel["autoThread"], True)
        self.assertEqual(channel["autoThreadName"], "generated")
        self.assertEqual(channel["autoArchiveDuration"], 10080)
```

- [ ] **Step 2: Run it and see it fail.** `python3 2-k3s/35.discord/tools/manifests.test.py -v 2>&1 | tail -5` gives FAIL on `MediaTests.test_openclaw_config` and `WindmillTests.test_openclaw_config` with `KeyError: 'silentReply'`.

- [ ] **Step 3: Add the settings** to both JSON files, as in Interfaces. Put `"silentReply": { "group": "allow" }` after `"skipBootstrap": true`.

- [ ] **Step 4: Run the tests.** `python3 2-k3s/35.discord/tools/manifests.test.py -v 2>&1 | tail -3` gives `OK`.

- [ ] **Step 5: Validate against the live OpenClaw.** For each app, copy the file into a scratch directory in the running pod, render it and validate it. Change nothing in the pod's own state:

```sh
for app in media windmill; do
  ns=discord-$app; c=gateway; [ $app = windmill ] && c=openclaw
  kubectl -n $ns exec -i deploy/openclaw -c $c -- sh -c 'set -eu; d=$(mktemp -d); mkdir $d/config $d/home; cat > $d/config/openclaw.json; echo x > $d/config/AGENTS.md; node /opt/openclaw-discord/render.mjs $d/config $d/home/.openclaw; HOME=$d/home OPENCLAW_STATE_DIR=$d/home/.openclaw OPENCLAW_CONFIG_PATH=$d/home/.openclaw/openclaw.json openclaw config validate --json 2>/dev/null | node -e "const j=JSON.parse(require(\"fs\").readFileSync(0,\"utf8\"));console.log(j.valid===true?\"valid\":JSON.stringify(j.issues??j))"; rm -rf $d' < 2-k3s/35.discord/$app/openclaw.json
done
```

Expected: `valid` twice. If it prints issues, report them and stop. Do not change any key name.

- [ ] **Step 6: Commit.** `git add 2-k3s/35.discord && git commit -m "discord: open one thread per message in both bot channels"`

### Task 2: Thread rules in both `AGENTS.md` files and the README

**Files:**
- Modify: `2-k3s/35.discord/media/AGENTS.md`
- Modify: `2-k3s/35.discord/windmill/AGENTS.md`
- Modify: `2-k3s/35.discord/README.md`
- Test: `2-k3s/35.discord/tools/manifests.test.py`

**Interfaces:**
- Consumes: Task 1's settings (the thread and silence behavior the rules rely on).

- [ ] **Step 1: Write the failing test.** Add to `AgentChecks`:

```python
    THREAD_MARKERS = [
        "## Threads",
        "never create a thread",
        "A topic is a thread, whoever started it.",
        'openclaw message thread list --channel discord --guild-id "$DISCORD_GUILD_ID" --channel-id "$DISCORD_CHANNEL_ID" --json',
        'openclaw message thread list --channel discord --guild-id "$DISCORD_GUILD_ID" --channel-id "$DISCORD_CHANNEL_ID" --include-archived --json',
        "--before <nextBefore>",
        "Skip the current thread.",
        "openclaw message read --channel discord --target channel:<thread id> --limit 20 --json",
        "I won't reply in this thread.",
        "with `NO_REPLY` only",
        "Never answer `NO_REPLY` anywhere else.",
    ]

    def test_agents_md_threads(self):
        text = (DISCORD / f"{self.APP}/AGENTS.md").read_text()
        positions = [text.index(marker) for marker in self.THREAD_MARKERS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(re.findall(r"^## .*", text, re.M)[0], "## Threads")
        self.assertIn(self.SAME_TOPIC, text)
        self.assertNotIn("in the channel", text)
```

Add `SAME_TOPIC = "the same movie, series, album or artist"` to `MediaTests` and `SAME_TOPIC = "the same Windmill script or flow"` to `WindmillTests`.

- [ ] **Step 2: Run it and see it fail.** `python3 2-k3s/35.discord/tools/manifests.test.py -v 2>&1 | tail -5` gives FAIL on both `test_agents_md_threads` with `ValueError: substring not found`.

- [ ] **Step 3: Add the section.** In each `AGENTS.md`, insert this section right after the first paragraph, before the first `## ` heading. Replace `<CHANNEL>` with `#media` or `#windmill`, and `<SAME TOPIC>` with that bot's `SAME_TOPIC` value. Copy the rest exactly:

````markdown
## Threads

Each new message in `<CHANNEL>` opens its own thread, and you answer inside it. Reply only in the current thread. Never post in the main channel or in another thread, and never create a thread.

A topic is a thread, whoever started it. The same topic here means <SAME TOPIC>.

On the first message of a thread, before anything else, check whether an earlier thread already covers the same topic. List the open threads, then the closed ones:

```sh
openclaw message thread list --channel discord --guild-id "$DISCORD_GUILD_ID" --channel-id "$DISCORD_CHANNEL_ID" --json
openclaw message thread list --channel discord --guild-id "$DISCORD_GUILD_ID" --channel-id "$DISCORD_CHANNEL_ID" --include-archived --json
```

While the closed list reports `"hasMore": true`, run it again with `--before <nextBefore>`. Skip the current thread. When a thread's name looks like the same topic, read it with `openclaw message read --channel discord --target channel:<thread id> --limit 20 --json`.

If an earlier thread covers the same topic, reply with only this line, with that thread's ID inside `<#...>` so Discord shows a clickable link: `This is already covered in <#thread id>. Continue there, I won't reply in this thread.` Do not handle the request. Answer every later message in this thread with `NO_REPLY` only.

Otherwise, handle the message and answer every message in this thread as usual. Never answer `NO_REPLY` anywhere else.
````

- [ ] **Step 4: Replace "in the channel"** in `windmill/AGENTS.md` (steps 3 and 7 and "When you need Spyros") with "in the thread". `media/AGENTS.md` has none.

- [ ] **Step 5: Update the README.** In `2-k3s/35.discord/README.md`, add a `## Threads` section after the channel table:

```markdown
## Threads

Each new message in `#media` or `#windmill` opens a thread on that message, and the bot answers only inside it. Each thread is a separate OpenClaw session. Threads close after 7 days without activity, and a new message in a closed thread reopens it. OpenClaw names each thread from its first message with one small model call.

A topic is a thread, whoever started it. On the first message of a new thread, the bot lists the channel's open and closed threads. If one already covers the topic, it replies once with a link to that thread and stays silent in the new thread from then on. That check is the model's judgment, set in each `AGENTS.md`, so it can miss a duplicate or flag one wrongly. `agents.defaults.silentReply.group: "allow"` lets the bot stay silent.
```

In "One-time owner setup", add a step 5: `Give both bots **Create Public Threads** and **Send Messages in Threads** in their channel.`

- [ ] **Step 6: Run the tests.** `python3 2-k3s/35.discord/tools/manifests.test.py -v 2>&1 | tail -3` gives `OK`.

- [ ] **Step 7: Commit.** `git add 2-k3s/35.discord && git commit -m "discord: send repeated topics to their existing thread"`

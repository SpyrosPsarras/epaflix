# Discord media subtitle source implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `#media` agent says whether a subtitle was translated by Lingarr or downloaded.

**Architecture:** Instruction-only change. The agent reads Bazarr history through the API it already uses.

**Tech Stack:** OpenClaw agent instructions (Markdown), Python unittest.

**Spec:** `docs/superpowers/specs/2026-11-14-discord-media-subtitle-source-design.md`

## Global constraints

- No new egress, secret, mount or image.
- `action` 6 = Lingarr translation; 1 = download; 3 = upgrade; 4 = manual upload.

## Review focus

1. Several languages in history: the agent must pick the newest record for the asked language, not the newest record overall.
2. Re-translated subtitle: history holds older records too; newest wins.
3. No history record: the agent says the source is unknown, never guesses.
4. Episode question: the agent must use `sonarrEpisodeId`, not the series ID.
5. Newest record has an action code not listed above: the agent reads its `description` and looks at older records for the source.

These are agent behaviour, not testable in unittest; the `AGENTS.md` wording must state 1 to 5.

---

### Task 1: Bazarr history instructions

**Files:**
- Modify: `2-k3s/35.discord/media/AGENTS.md` (Bazarr section)
- Test: `2-k3s/35.discord/tools/manifests.test.py` (`MediaTests`)

- [ ] **Step 1: Write the failing test** `test_agents_md_subtitle_source`:

```python
def test_agents_md_subtitle_source(self):
    text = (DISCORD / "media/AGENTS.md").read_text()
    for marker in ["GET /movies/history?radarrid=<id>", "GET /episodes/history?episodeid=<sonarrEpisodeId>", "`action` 6", "any other `action`"]:
        self.assertIn(marker, text)
```

- [ ] **Step 2: Run** `python3 2-k3s/35.discord/tools/manifests.test.py -v`. Expected: the new test fails.
- [ ] **Step 3: Add** a "Subtitle source" bullet group to the Bazarr section covering the two endpoints, the action meanings and Review focus 1 to 5.
- [ ] **Step 4: Run** the same command. Expected: all tests pass.
- [ ] **Step 5: Commit** after the review gate passes.

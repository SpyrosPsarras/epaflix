import assert from "node:assert/strict"
import { mkdir, mkdtemp, writeFile, rm, readFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "./jev-auto.js"

const dir = await mkdtemp(join(tmpdir(), "jev-auto-"))
const saved = { fetch: globalThis.fetch, state: process.env.XDG_STATE_HOME, key: process.env.JEV_OPENROUTER_KEY_FILE, config: process.env.XDG_CONFIG_HOME }
process.env.XDG_STATE_HOME = dir
process.env.XDG_CONFIG_HOME = dir
await mkdir(join(dir, "opencode"))
await writeFile(join(dir, "opencode/jev-checks.md"), "JEV CHECKS RULE")
process.env.JEV_OPENROUTER_KEY_FILE = join(dir, "key")
await writeFile(process.env.JEV_OPENROUTER_KEY_FILE, "test-key", { mode: 0o600 })
try {
  let parentID, history = [], calls = 0, getFails = false
  const client = { session: {
    get: async () => { if (getFails) throw new Error("down"); return { data: { parentID } } },
    messages: async () => ({ data: history }),
  } }
  const hooks = await plugin({ client, directory: "/test", project: { id: "test" } })
  const catalog = () => ({ "gpt-6-luna": { id: "codex/gpt-6-luna" }, "gpt-6-astra": { id: "codex/gpt-6-astra", limit: { context: 872000, output: 128000 } },
    "or-glm-5.3-flash": { id: "openrouter/or-glm-5.3-flash" }, "claude-opus-5-5": { id: "claude/claude-opus-5-5" },
    "gpt-6-sol": { id: "codex/gpt-6-sol" }, "claude-sonnet-5": { id: "claude/claude-sonnet-5" }, "claude-fable-5-1": { id: "claude/claude-fable-5-1" },
    "claude-haiku-4-5-20251001": { id: "claude/claude-haiku-4-5-20251001" } })
  const config = { provider: { cliproxy: { options: { baseURL: "http://proxy/v1" }, models: catalog() } }, enabled_providers: ["cliproxy"] }
  await hooks.config(config)
  assert.equal(config.provider["jev-auto"].models.auto.id, "codex/gpt-6-sol")
  assert.ok(config.enabled_providers.includes("jev-auto"))
  const fresh = () => ({ message: { id: "test", model: { providerID: "jev-auto", modelID: "auto" }, system: "original" }, parts: [{ type: "text", text: "Fix the typo: deploymnet" }] })
  const input = { sessionID: "test", model: { providerID: "jev-auto", modelID: "auto" } }
  const response = (choice, confidence) => async () => {
    calls++
    return Response.json({ id: "request", answers: { route: { choice, confidence } }, usage: { cost: 0.00002 } })
  }
  globalThis.fetch = response("trivial", 0.95)
  let output = fresh()
  await hooks["chat.message"](input, output)
  assert.equal(output.message.model.modelID, "gpt-6-luna")
  assert.ok(output.message.system.startsWith("original"))
  assert.deepEqual(output.parts, fresh().parts)
  for (const [route, confidence] of [["trivial", 0.89], ["strong", 1], ["invalid", 1], ["trivial", 2]]) {
    globalThis.fetch = response(route, confidence)
    output = fresh()
    await hooks["chat.message"](input, output)
    assert.equal(output.message.model.modelID, "gpt-6-sol")
  }
  for (const route of ["deep", "dialogue"]) {
    globalThis.fetch = response(route, 0.9)
    output = fresh()
    await hooks["chat.message"](input, output)
    assert.equal(output.message.model.modelID, "claude-opus-5-5", `${route} uses Opus`)
    globalThis.fetch = response(route, 0.3)
    output = fresh()
    await hooks["chat.message"](input, output)
    assert.equal(output.message.model.modelID, "gpt-6-sol", "uncertain classification keeps Sol")
  }
  globalThis.fetch = response("trivial", 1)
  const before = calls
  history = [{ info: { role: "user" } }]
  output = fresh()
  await hooks["chat.message"](input, output)
  assert.equal(output.message.model.modelID, "gpt-6-sol")
  history = []
  output = fresh(); output.parts.push({ type: "file" })
  await hooks["chat.message"](input, output)
  assert.equal(calls, before, "followups and files must not invoke Jev")
  output = fresh(); output.message.model = { providerID: "cliproxy", modelID: "gpt-6-astra" }
  const copy = structuredClone(output)
  await hooks["chat.message"](input, output)
  assert.deepEqual(output, copy, "manual selections unchanged")
  globalThis.fetch = async () => { throw new Error("secret error") }
  output = fresh()
  await hooks["chat.message"](input, output)
  assert.equal(output.message.model.modelID, "gpt-6-sol")
  const log = await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")
  assert.ok(!/test-key|deploymnet|secret error/.test(log))
  assert.equal(JSON.parse(log.split("\n")[0]).actualModel, "cliproxy/gpt-6-luna")

  // Auto is never registered unless both targets are OpenAI or Anthropic routes.
  const offRoute = { provider: { cliproxy: { options: {}, models: { ...catalog(), "gpt-6-sol": { id: "openrouter/gpt-6-sol" } } } } }
  await (await plugin({ client, directory: "/test", project: { id: "test" } })).config(offRoute)
  assert.equal(offRoute.provider["jev-auto"], undefined, "Auto must not target a non-OpenAI/Anthropic route")

  // Jev MCP tools run only in sessions whose current turn is an OpenAI or Anthropic model.
  const blocked = (sessionID, tool = "jev_jev_screen") => hooks["tool.execute.before"]({ tool, sessionID, callID: "c" })
    .then(() => false, e => { assert.match(e.message, /only on OpenAI and Anthropic/); return true })
  const turn = async (sessionID, providerID, modelID) => {
    const out = { message: { id: "m", model: { providerID, modelID }, system: "s" }, parts: [{ type: "text", text: "hi" }] }
    const copy = structuredClone(out)
    await hooks["chat.message"]({ sessionID }, out)
    assert.deepEqual(out, copy, "non-Auto turns are never modified")
  }
  // Fresh Auto sessions keep Jev on every path: classifier failure and a Luna route.
  globalThis.fetch = async () => { throw new Error("down") }
  await hooks["chat.message"]({ sessionID: "auto-fail" }, fresh())
  assert.equal(await blocked("auto-fail"), false, "Auto turn whose classifier failed keeps Jev")
  globalThis.fetch = response("trivial", 0.95)
  output = fresh()
  await hooks["chat.message"]({ sessionID: "auto-luna" }, output)
  assert.equal(output.message.model.modelID, "gpt-6-luna")
  assert.equal(await blocked("auto-luna"), false, "Auto turn routed to Luna keeps Jev")
  const jevCalls = calls
  await turn("glm", "cliproxy", "or-glm-5.3-flash")
  assert.equal(await blocked("glm"), true, "OpenRouter model loses Jev")
  assert.equal(await blocked("glm", "bash"), false, "other tools are untouched")
  await turn("claude", "cliproxy", "claude-opus-5-5")
  assert.equal(await blocked("claude"), false, "Anthropic via CLIProxyAPI keeps Jev")
  await turn("claude", "cliproxy", "or-glm-5.3-flash")
  assert.equal(await blocked("claude"), true, "switching a thread to OpenRouter removes Jev")
  await turn("direct", "anthropic", "claude-sonnet-5")
  assert.equal(await blocked("direct"), false, "direct Anthropic provider keeps Jev")
  await turn("direct-openai", "openai", "gpt-5")
  assert.equal(await blocked("direct-openai"), false, "direct OpenAI provider keeps Jev")
  await turn("unknown-model", "cliproxy", "not-in-catalog")
  assert.equal(await blocked("unknown-model"), true, "unknown catalog entry fails closed")
  assert.equal(await blocked("never-seen"), true, "unseen session fails closed")
  assert.equal(calls, jevCalls, "manual turns never call the Jev classifier")

  // The jev-checks rule reaches OpenAI and Anthropic models only.
  const prompt = async (providerID, apiId) => {
    const out = { system: ["base"] }
    await hooks["experimental.chat.system.transform"]({ sessionID: "x", model: { providerID, api: { id: apiId } } }, out)
    return out.system
  }
  assert.deepEqual(await prompt("cliproxy", "openrouter/or-glm-5.3-flash"), ["base"])
  assert.deepEqual(await prompt("cliproxy", "codex/gpt-6-luna"), ["base", "JEV CHECKS RULE"])
  assert.deepEqual(await prompt("cliproxy", "claude/claude-opus-5-5"), ["base", "JEV CHECKS RULE"])
  assert.deepEqual(await prompt("openai", "gpt-5"), ["base", "JEV CHECKS RULE"])
  // Subagents: Jev scores tier, effort and prose once per child session; parent's model when unsure.
  let body
  const answer = (answers) => async (url, init) => { calls++; body = JSON.parse(init.body); return Response.json({ id: "req", answers, usage: { cost: 0.00003 } }) }
  const pick = (tier, tierConfidence, effort, dialogue) => answer({ tier: { score: tier, confidence: tierConfidence }, effort: { score: effort, confidence: 0.5 }, dialogue: { noul: dialogue } })
  const child = async (sessionID, model, text = "Find where parseConfig is defined and list its callers", agent = "explore") => {
    const out = { message: { id: "m-" + sessionID, model: { ...model }, system: "s" }, parts: [{ type: "text", text }] }
    await hooks["chat.message"]({ sessionID, agent }, out)
    return out.message.model
  }
  const opus = { providerID: "cliproxy", modelID: "claude-opus-5-5" }
  const params = async (sessionID, api) => {
    const out = { temperature: 0, topP: 1, topK: 0, maxOutputTokens: undefined, options: {} }
    await hooks["chat.params"]({ sessionID, agent: "explore", model: { id: api.split("/")[1], api: { id: api } }, provider: {}, message: {} }, out)
    return out.options
  }
  parentID = "parent"
  globalThis.fetch = pick(0.1, 0.9, 0.2, 0.1)
  let n = calls
  assert.deepEqual(await child("c1", opus), { providerID: "cliproxy", modelID: "gpt-6-luna" })
  assert.equal(calls, n + 1)
  assert.deepEqual(Object.keys(body.questions).sort(), ["dialogue", "effort", "tier"])
  assert.equal(body.state.agent, "explore")
  assert.match(body.state.task, /parseConfig/)
  assert.deepEqual(await params("c1", "codex/gpt-6-luna"), { reasoningEffort: "low" })
  assert.deepEqual(await child("c1", opus, "continue"), { providerID: "cliproxy", modelID: "gpt-6-luna" }, "a resumed subagent keeps its route")
  assert.equal(calls, n + 1, "one Jev call per subagent session")
  assert.deepEqual(await child("c1", { providerID: "cliproxy", modelID: "or-glm-5.3-flash" }, "continue"), { providerID: "cliproxy", modelID: "gpt-6-luna" }, "the route holds when the parent switched models")
  assert.deepEqual(await params("c1", "openrouter/or-glm-5.3-flash"), {}, "effort only on the routed model")
  getFails = true
  assert.deepEqual(await child("c1", opus, "continue"), { providerID: "cliproxy", modelID: "gpt-6-luna" }, "a known route needs no lookup")
  getFails = false
  assert.equal(calls, n + 1)
  globalThis.fetch = pick(1.2, 0.8, 1.8, 0.9)
  assert.equal((await child("c2", opus)).modelID, "claude-opus-5-5")
  assert.deepEqual(await params("c2", "claude/claude-opus-5-5"), { effort: "high" })
  globalThis.fetch = pick(0.1, 0.3, 0, 0)
  assert.deepEqual(await child("c3", opus), opus, "unsure: parent's model")
  assert.deepEqual(await params("c3", "claude/claude-opus-5-5"), {})
  globalThis.fetch = pick("x", 0.9, 1, 0.5)
  assert.deepEqual(await child("c4", opus), opus, "malformed answers are rejected")
  for (const [answers, want] of [[[2, 0.9, 2, 0.8], "claude-opus-5-5:high"], [[1.9, 0.9, 1, 0.2], "claude-opus-5-5:medium"], [[0.9, 0.9, 0.4, 0.1], "gpt-6-sol:low"], [[0.2, 0.9, 1.5, 0.1], "gpt-6-luna:high"]]) {
    globalThis.fetch = pick(...answers)
    const sid = "map-" + want
    const m = await child(sid, opus)
    assert.equal(`${m.modelID}:${Object.values(await params(sid, `${m.modelID.startsWith("claude") ? "claude" : "codex"}/${m.modelID}`))[0]}`, want)
  }
  globalThis.fetch = async () => { throw new Error("down") }
  assert.deepEqual(await child("c5", opus), opus, "Jev down: parent's model")
  n = calls
  globalThis.fetch = pick(0, 0.9, 0, 0)
  assert.deepEqual(await child("c6", { providerID: "cliproxy", modelID: "or-glm-5.3-flash" }), { providerID: "cliproxy", modelID: "or-glm-5.3-flash" })
  assert.equal(calls, n, "no Jev for a non-OpenAI/Anthropic parent")
  globalThis.fetch = pick(0, 0.2, 0, 0)
  assert.deepEqual(await child("c7", { providerID: "jev-auto", modelID: "auto" }), { providerID: "cliproxy", modelID: "gpt-6-sol" }, "unsure under Auto: Sol")
  // A catalog without Sol keeps the parent's model for ordinary work.
  const noSol = await plugin({ client, directory: "/test", project: { id: "test" } })
  await noSol.config({ provider: { cliproxy: { options: {}, models: Object.fromEntries(Object.entries(catalog()).filter(([k]) => k !== "gpt-6-sol")) } } })
  globalThis.fetch = pick(1, 0.9, 2, 0.1)
  const nsOut = { message: { id: "m", model: { ...opus } }, parts: [{ type: "text", text: "design it" }] }
  await noSol["chat.message"]({ sessionID: "ns", agent: "general" }, nsOut)
  assert.deepEqual(nsOut.message.model, opus, "model missing from the catalog: parent's model")
  // A `route: <model>:<effort>` line pins the subagent without a Jev call (review gate).
  n = calls
  globalThis.fetch = pick(2, 0.9, 2, 0.9)
  const pinned = (sid, line) => child(sid, opus, `Review this diff for bugs.\n${line}\nReport file:line.`, "general")
  assert.deepEqual(await pinned("p1", "route: claude-sonnet-5:high"), { providerID: "cliproxy", modelID: "claude-sonnet-5" })
  assert.deepEqual(await params("p1", "claude/claude-sonnet-5"), { effort: "high" })
  assert.deepEqual(await pinned("p2", "route: gpt-6-luna_low"), { providerID: "cliproxy", modelID: "gpt-6-luna" })
  assert.deepEqual(await params("p2", "codex/gpt-6-luna"), { reasoningEffort: "low" })
  assert.deepEqual(await pinned("p3", "route: claude-haiku-4-5-20251001:low"), { providerID: "cliproxy", modelID: "claude-haiku-4-5-20251001" }, "any catalog Haiku can be pinned")
  assert.deepEqual(await params("p3", "claude/claude-haiku-4-5-20251001"), {}, "Haiku takes no effort parameter")
  assert.equal(calls, n, "pinned routes make no Jev call")
  assert.deepEqual(await child("p1", opus, "continue"), { providerID: "cliproxy", modelID: "claude-sonnet-5" }, "a pin holds on resume")
  for (const [sid, line] of [["x1", "route: claude-fable-5-1:low"], ["x2", "route: claude-sonnet-5:max"], ["x3", "route: gpt-9:low"], ["x4", "please use route: claude-sonnet-5:high"]]) {
    await pinned(sid, line)
    assert.ok(calls > n, `${line} is not a valid pin, so Jev routes`)
    n = calls
  }
  const noHaiku = await plugin({ client, directory: "/test", project: { id: "test" } })
  await noHaiku.config({ provider: { cliproxy: { options: {}, models: Object.fromEntries(Object.entries(catalog()).filter(([k]) => !k.includes("haiku"))) } } })
  const nhOut = { message: { id: "m", model: { ...opus } }, parts: [{ type: "text", text: "route: claude-haiku-4-5-20251001:low" }] }
  await noHaiku["chat.message"]({ sessionID: "nh", agent: "general" }, nhOut)
  assert.notEqual(nhOut.message.model.modelID, "claude-haiku-4-5-20251001", "a pin to a model missing from the catalog is ignored")
  // Families resolve to their highest version in the live catalog, for routes, pins and Auto.
  const newer = await plugin({ client, directory: "/test", project: { id: "test" } })
  const grown = { ...catalog(),
    "claude-opus-5": { id: "claude/claude-opus-5" }, "claude-opus-4-8": { id: "claude/claude-opus-4-8" },
    "claude-sonnet-5-5": { id: "claude/claude-sonnet-5-5" }, "claude-haiku-5": { id: "claude/claude-haiku-5" }, "claude-haiku-10": { id: "claude/claude-haiku-10" },
    "gpt-5.6-sol": { id: "codex/gpt-5.6-sol" }, "gpt-6.1-sol": { id: "codex/gpt-6.1-sol" }, "gpt-7-luna": { id: "codex/gpt-7-luna" }, "gpt-6.1-astra": { id: "codex/gpt-6.1-astra" },
    "gpt-9-sol": { id: "openrouter/gpt-9-sol" }, "claude-opus-4-6-1m": { id: "claude/claude-opus-4-6-1m" }, "claude-opus-6-1m": { id: "claude/claude-opus-6-1m" } }
  const grownCfg = { provider: { cliproxy: { options: {}, models: grown } } }
  await newer.config(grownCfg)
  assert.equal(grownCfg.provider["jev-auto"].models.auto.id, "codex/gpt-6.1-sol", "Auto falls back to the newest Sol")
  const route = async (sid, answers, text = "do the task") => {
    globalThis.fetch = pick(...answers)
    const out = { message: { id: "m", model: { ...opus } }, parts: [{ type: "text", text }] }
    await newer["chat.message"]({ sessionID: sid, agent: "general" }, out)
    return out.message.model.modelID
  }
  assert.equal(await route("v1", [1, 0.9, 1, 0.9]), "claude-opus-5-5", "discussion uses newest Opus")
  assert.equal(await route("v2", [2, 0.9, 2, 0.9]), "claude-opus-5-5", "Opus 5.5 beats Opus 5 and 4.8; 4-6-1m is not a version")
  assert.equal(await route("v3", [1, 0.9, 2, 0.1]), "gpt-6.1-sol", "an openrouter/ route never counts, and 6.1 beats 6 and 5.6")
  assert.equal(await route("v4", [0, 0.9, 0, 0.1]), "gpt-7-luna")
  assert.equal(await route("v5", [1, 0.9, 1, 0.1]), "gpt-6.1-sol")
  assert.equal(await route("v6", [0, 0.9, 0, 0], "route: claude-opus-5:high"), "claude-opus-5-5", "a pin to an old version gets the newest")
  assert.equal(await route("v7", [0, 0.9, 0, 0], "route: opus_high"), "claude-opus-5-5", "a pin can name the family")
  assert.equal(await route("v8", [0, 0.9, 0, 0], "route: haiku_low"), "claude-haiku-10", "versions compare as numbers: 10 beats 5 beats 4.5 (dated)")
  assert.equal(await route("v9", [0, 0.9, 0, 0], "route: fable_high") !== "claude-fable-5-1", true, "Fable is never pinnable")
  parentID = undefined
  const autoOut = fresh()
  globalThis.fetch = response("trivial", 0.95)
  await newer["chat.message"]({ sessionID: "v-auto" }, autoOut)
  assert.equal(autoOut.message.model.modelID, "gpt-7-luna", "Auto's cheap path uses the newest Luna")
  assert.deepEqual(await params("test", "codex/gpt-6-astra"), {}, "top-level turns keep their effort")
  const sub = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(r => r.mode === "subagent")
  assert.deepEqual(sub.map(r => r.status), ["classified", "classified", "fallback", "fallback", "classified", "classified", "classified", "classified", "fallback", "fallback", "fallback",
    "pinned", "pinned", "pinned", "classified", "classified", "classified", "classified", "classified",
    "classified", "classified", "classified", "classified", "classified", "pinned", "pinned", "pinned", "classified"])
  assert.equal(sub.find(r => r.sessionId === "ns").reason, "model_not_in_catalog")
  assert.ok(!JSON.stringify(sub).includes("parseConfig"), "subagent prompts are not logged")

  parentID = "parent"
  for (const [sid, text, tier, dialogue, want] of [
    ["triage", "Triage the reported issue against the supplied logs", 1, 0.1, "gpt-6-sol"],
    ["validate", "Validate the change using the checks and report failures", 1, 0.1, "gpt-6-sol"],
    ["review", "Review this ordinary diff for bugs", 1, 0.1, "gpt-6-sol"],
    ["deep", "Deep dive into a subtle concurrency bug across services", 2, 0.1, "claude-opus-5-5"],
    ["decide", "Discuss architectural tradeoffs with the user and decide", 1, 0.9, "claude-opus-5-5"],
  ]) {
    globalThis.fetch = pick(tier, 0.9, 2, dialogue)
    assert.equal((await child(sid, opus, text, "general")).modelID, want)
  }
  parentID = undefined

  const noOpus = await plugin({ client, directory: "/test", project: { id: "test" } })
  await noOpus.config({ provider: { cliproxy: { options: {}, models: Object.fromEntries(Object.entries(catalog()).filter(([k]) => !k.includes("opus"))) } } })
  globalThis.fetch = response("deep", 0.9)
  const missingOpus = fresh()
  await noOpus["chat.message"]({ sessionID: "no-opus" }, missingOpus)
  assert.equal(missingOpus.message.model.modelID, "gpt-6-sol", "missing Opus keeps the valid Sol default")

  // Skills: one Jev call per user message, one line in the system prompt when it fits.
  const skills = "<available_skills>\n  <skill>\n    <name>tdd</name>\n    <description>Test-driven development. Use when building features test-first.</description>\n    <location>/x</location>\n  </skill>\n  <skill>\n    <name>grill-me</name>\n    <description>A relentless interview to sharpen a plan.</description>\n    <location>/y</location>\n  </skill>\n</available_skills>"
  const userTurn = async (sessionID, text, model = opus) => {
    const out = { message: { id: "u", model: { ...model }, system: "s" }, parts: [{ type: "text", text }] }
    const copy = structuredClone(out)
    await hooks["chat.message"]({ sessionID }, out)
    assert.deepEqual(out, copy, "skill picking never edits the message")
  }
  const sys = async (sessionID, system = [skills], api = "claude/claude-opus-5-5") => {
    const out = { system: [...system] }
    await hooks["experimental.chat.system.transform"]({ sessionID, model: { providerID: "cliproxy", api: { id: api } } }, out)
    return out.system.slice(system.length)
  }
  const skillAnswer = (needs, choice, confidence) => answer({ needs_skill: { noul: needs }, skill: { choice, confidence } })
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.8)
  n = calls
  await userTurn("s1", "Add a retry to the uploader, test first")
  assert.equal(calls, n, "no Jev call in chat.message")
  assert.deepEqual(await sys("s1", ["title prompt"]), ["JEV CHECKS RULE"], "calls without the skill list do not pick")
  assert.equal(calls, n)
  let lines = await sys("s1")
  assert.equal(calls, n + 1)
  assert.deepEqual(Object.keys(body.questions.skill.criteria).sort(), ["grill-me", "none", "tdd"])
  assert.match(body.state.request, /retry to the uploader/)
  assert.equal(lines[0], "JEV CHECKS RULE")
  assert.match(lines[1], /"tdd" skill/)
  assert.deepEqual(await sys("s1"), lines, "later steps of the turn repeat the pick without a call")
  assert.equal(calls, n + 1)
  const fileOnly = { message: { id: "f", model: { ...opus } }, parts: [{ type: "file" }] }
  await hooks["chat.message"]({ sessionID: "s1" }, fileOnly)
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], "a message without text clears the old pick")
  globalThis.fetch = skillAnswer(0.9, "none", 0.9)
  await userTurn("s1", "What time is it in Athens?")
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], "a new message clears the old pick")
  globalThis.fetch = skillAnswer(0.3, "tdd", 0.7)
  await userTurn("s1", "thanks")
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], "fairly sure but no skill needed: none")
  globalThis.fetch = skillAnswer(0.2, "tdd", 0.9)
  await userTurn("s1", "tests please")
  assert.match((await sys("s1"))[1], /"tdd"/, "sure of the skill: named even when needs_skill is low")
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.2)
  await userTurn("s1", "maybe tests")
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"])
  for (const bad of ["not-a-skill", "constructor"]) {
    globalThis.fetch = skillAnswer(0.9, bad, 0.9)
    await userTurn("s1", "x")
    assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], `rejects ${bad}`)
  }
  // A newer message while Jev is still answering: the old pick must not land on it.
  let release
  globalThis.fetch = (url, init) => new Promise(r => { release = () => r(Response.json({ id: "r", answers: { needs_skill: { noul: 0.9 }, skill: { choice: "tdd", confidence: 0.9 } }, usage: {} })) })
  await userTurn("s1", "old request")
  const slow = sys("s1")
  await new Promise(r => setTimeout(r, 10))
  await hooks["chat.message"]({ sessionID: "s1" }, { message: { id: "f2", model: { ...opus } }, parts: [{ type: "file" }] })
  release()
  await slow
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], "a stale pick is dropped")
  // Session lookup down: no skill pick (it may be a subagent), Auto stays on Sol.
  getFails = true
  n = calls
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.9)
  await userTurn("lf", "test first")
  assert.deepEqual(await sys("lf"), ["JEV CHECKS RULE"])
  const lfAuto = fresh()
  await hooks["chat.message"]({ sessionID: "lf-auto" }, lfAuto)
  assert.equal(lfAuto.message.model.modelID, "gpt-6-sol")
  assert.equal(calls, n, "no Jev call when the session lookup fails")
  getFails = false
  globalThis.fetch = async () => { throw new Error("down") }
  await userTurn("s1", "y")
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"])
  n = calls
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.9)
  await userTurn("g1", "test first please", { providerID: "cliproxy", modelID: "or-glm-5.3-flash" })
  assert.deepEqual(await sys("g1", [skills], "openrouter/or-glm-5.3-flash"), [])
  parentID = "parent"
  await child("k1", opus, "test first please")
  parentID = undefined
  await sys("k1")
  assert.equal(calls, n + 1, "no skill pick for OpenRouter models; the child call is the subagent route only")
  const sk = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(r => r.mode === "skill")
  assert.deepEqual(sk.map(r => r.skill ?? null), ["tdd", null, null, "tdd", null, null, null, "tdd", null])
  assert.ok(!JSON.stringify(sk).includes("uploader"), "user text is not logged")
  console.log("PASS: Auto catalog, high-confidence routing, fallback, manual selections, context gates, safe logs, OpenAI/Anthropic-only Jev")
} finally {
  globalThis.fetch = saved.fetch
  for (const [key, value] of [["XDG_STATE_HOME", saved.state], ["JEV_OPENROUTER_KEY_FILE", saved.key], ["XDG_CONFIG_HOME", saved.config]]) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  await rm(dir, { recursive: true, force: true })
}

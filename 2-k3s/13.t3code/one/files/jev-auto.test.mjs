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

// A fake OpenCode 2 plugin context: sessions, catalog, skills and the registered hooks.
let getFails = false, switchFails = false, history = [], skills = []
const sessions = new Map()
const setup = async (catalog) => {
  const hooks = {}, switched = []
  let registered
  const list = Object.entries(catalog).map(([id, m]) => ({ id, modelID: m.id, providerID: "cliproxy", variants: [] }))
  const ctx = {
    location: { directory: "/test", project: { id: "test" } },
    model: { list: async () => ({ data: [...list, ...(registered?.models ?? [])] }) },
    skill: { list: async () => ({ data: skills }) },
    provider: { transform: async (cb) => cb({
      get: (id) => id === "cliproxy" ? { provider: { id, settings: { baseURL: "http://proxy/v1", apiKey: "k" } }, models: new Map(list.map(m => [m.id, m])) } : undefined,
      add: (x) => { registered = x },
    }) },
    session: {
      hook: async (name, cb) => { hooks[name] = cb },
      get: async ({ sessionID }) => { if (getFails) throw new Error("down"); return { id: sessionID, ...sessions.get(sessionID) } },
      context: async () => history,
      switchModel: async ({ sessionID, model }) => {
        if (switchFails) throw new Error("switch refused")
        switched.push(sessionID)
        sessions.set(sessionID, { ...sessions.get(sessionID), model })
      },
    },
    tool: { hook: async (name, cb) => { hooks[`tool.${name}`] = cb } },
  }
  await plugin.setup(ctx)
  return { hooks, registered, switched }
}
const auto = { providerID: "jev-auto", id: "auto" }
const prompt = (hooks, sessionID, text, extra = {}) => hooks.prompt({ sessionID, messageID: `m-${sessionID}`, prompt: { text, ...extra }, delivery: "immediate" })
const context = async (hooks, sessionID, model) => {
  const event = { sessionID, agent: "build", model, system: [], messages: [], options: {}, tools: {} }
  await hooks.context(event)
  return event.system.map(p => p.text)
}
// The model the next primary request of a session goes out with.
const routed = async (hooks, sessionID, providerID = "jev-auto") => {
  const event = { sessionID, agent: "build", model: { providerID, id: "auto" }, kind: "primary",
    request: new Request("http://proxy/v1/chat/completions", { method: "POST", body: JSON.stringify({ model: "codex/gpt-6-sol", messages: [] }) }) }
  await hooks["http.request"](event)
  return JSON.parse(await event.request.text()).model
}

try {
  let calls = 0
  const catalog = () => ({ "gpt-6-luna": { id: "codex/gpt-6-luna" }, "gpt-6-astra": { id: "codex/gpt-6-astra" },
    "or-glm-5.3-flash": { id: "openrouter/or-glm-5.3-flash" }, "claude-opus-5-5": { id: "claude/claude-opus-5-5" },
    "gpt-6-sol": { id: "codex/gpt-6-sol" }, "claude-sonnet-5": { id: "claude/claude-sonnet-5" }, "claude-fable-5-1": { id: "claude/claude-fable-5-1" },
    "claude-haiku-4-5-20251001": { id: "claude/claude-haiku-4-5-20251001" } })
  const { hooks, registered } = await setup(catalog())
  assert.equal(registered.models[0].id, "auto")
  assert.equal(registered.models[0].providerID, "jev-auto")
  assert.equal(registered.models[0].modelID, "codex/gpt-6-sol", "Auto's catalog entry is the newest Sol")
  assert.equal(registered.models[0].package, "aisdk:@ai-sdk/openai-compatible")
  assert.equal(registered.info.settings.baseURL, "http://proxy/v1", "Auto talks to CLIProxy directly; no local proxy")
  const response = (choice, confidence) => async () => {
    calls++
    return Response.json({ id: "request", answers: { route: { choice, confidence } }, usage: { cost: 0.00002 } })
  }
  const autoTurn = async (sid, text = "Fix the typo: deploymnet", extra) => {
    sessions.set(sid, { model: { ...auto } })
    await prompt(hooks, sid, text, extra)
    assert.deepEqual(sessions.get(sid).model, auto, "the session, and T3's selector, stay on Auto")
    return routed(hooks, sid)
  }
  globalThis.fetch = response("trivial", 0.95)
  assert.equal(await autoTurn("test"), "codex/gpt-6-luna")
  for (const [route, confidence] of [["trivial", 0.89], ["strong", 1], ["invalid", 1], ["trivial", 2]]) {
    globalThis.fetch = response(route, confidence)
    assert.equal(await autoTurn("test"), "codex/gpt-6-sol")
  }
  for (const route of ["deep", "dialogue"]) {
    globalThis.fetch = response(route, 0.9)
    assert.equal(await autoTurn("test"), "claude/claude-opus-5-5", `${route} uses Opus over Chat Completions`)
    globalThis.fetch = response(route, 0.3)
    assert.equal(await autoTurn("test"), "codex/gpt-6-sol", "uncertain classification keeps Sol")
  }
  // A new turn never inherits the previous turn's route.
  globalThis.fetch = response("deep", 0.9)
  assert.equal(await autoTurn("repeat"), "claude/claude-opus-5-5")
  history = [{ id: "old", type: "user" }]
  assert.equal(await autoTurn("repeat"), "codex/gpt-6-sol", "a follow-up turn goes back to Sol")
  globalThis.fetch = response("trivial", 1)
  const beforeFollowup = calls
  assert.equal(await autoTurn("test"), "codex/gpt-6-sol")
  assert.equal(calls, beforeFollowup, "followups must not invoke Jev")
  history = [{ id: "m-test", type: "user" }, { id: "x", type: "model_selected" }]
  assert.equal(await autoTurn("test"), "codex/gpt-6-luna", "the turn's own message and selection events are not history")
  history = []
  const beforeFiles = calls
  assert.equal(await autoTurn("test", "look", { files: [{ uri: "file:///x" }] }), "codex/gpt-6-sol")
  assert.equal(calls, beforeFiles, "files must not invoke Jev")
  sessions.set("manual", { model: { providerID: "cliproxy", id: "gpt-6-astra" } })
  await prompt(hooks, "manual", "Fix the typo")
  assert.deepEqual(sessions.get("manual").model, { providerID: "cliproxy", id: "gpt-6-astra" }, "manual selections unchanged")
  const untouched = { sessionID: "manual", model: { providerID: "cliproxy", id: "gpt-6-astra" }, kind: "primary",
    request: new Request("http://proxy/v1/chat/completions", { method: "POST", body: JSON.stringify({ model: "codex/gpt-6-astra" }) }) }
  const original = untouched.request
  await hooks["http.request"](untouched)
  assert.equal(untouched.request, original, "a request without tools on a manual model is not rebuilt")
  globalThis.fetch = async () => { throw new Error("secret error") }
  assert.equal(await autoTurn("test"), "codex/gpt-6-sol")
  const log = await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")
  assert.ok(!/test-key|deploymnet|secret error/.test(log))
  assert.equal(JSON.parse(log.split("\n")[0]).actualModel, "cliproxy/gpt-6-luna")
  assert.equal(JSON.parse(log.split("\n")[0]).selectedModel, "jev-auto/auto")

  // Auto is never registered unless both targets are OpenAI or Anthropic routes.
  const offRoute = await setup({ ...catalog(), "gpt-6-sol": { id: "openrouter/gpt-6-sol" } })
  assert.equal(offRoute.registered, undefined, "Auto must not target a non-OpenAI/Anthropic route")

  // Jev MCP tools run only in sessions whose current step is an OpenAI or Anthropic model.
  const blocked = (sessionID, tool = "jev_jev_screen") => Promise.resolve().then(() => hooks["tool.execute.before"]({ tool, sessionID, id: "c", input: {} }))
    .then(() => false, e => { assert.match(e.message, /only on OpenAI and Anthropic/); return true })
  const jevCalls = calls
  await context(hooks, "auto-fail", auto)
  assert.equal(await blocked("auto-fail"), false, "an Auto step keeps Jev")
  await context(hooks, "glm", { providerID: "cliproxy", id: "or-glm-5.3-flash" })
  assert.equal(await blocked("glm"), true, "OpenRouter model loses Jev")
  assert.equal(await blocked("glm", "shell"), false, "other tools are untouched")
  await context(hooks, "claude", { providerID: "cliproxy", id: "claude-opus-5-5" })
  assert.equal(await blocked("claude"), false, "Anthropic via CLIProxyAPI keeps Jev")
  await context(hooks, "claude", { providerID: "cliproxy", id: "or-glm-5.3-flash" })
  assert.equal(await blocked("claude"), true, "switching a thread to OpenRouter removes Jev")
  await context(hooks, "direct", { providerID: "anthropic", id: "claude-sonnet-5" })
  assert.equal(await blocked("direct"), false, "direct Anthropic provider keeps Jev")
  await context(hooks, "direct-openai", { providerID: "openai", id: "gpt-5" })
  assert.equal(await blocked("direct-openai"), false, "direct OpenAI provider keeps Jev")
  await context(hooks, "unknown-model", { providerID: "cliproxy", id: "not-in-catalog" })
  assert.equal(await blocked("unknown-model"), true, "unknown catalog entry fails closed")
  assert.equal(await blocked("never-seen"), true, "unseen session fails closed")
  assert.equal(calls, jevCalls, "steps without a pending skill pick never call Jev")

  // The jev-checks rule reaches OpenAI and Anthropic models only.
  assert.deepEqual(await context(hooks, "x", { providerID: "cliproxy", id: "or-glm-5.3-flash" }), [])
  assert.deepEqual(await context(hooks, "x", { providerID: "cliproxy", id: "gpt-6-luna" }), ["JEV CHECKS RULE"])
  assert.deepEqual(await context(hooks, "x", { providerID: "cliproxy", id: "claude-opus-5-5" }), ["JEV CHECKS RULE"])
  assert.deepEqual(await context(hooks, "x", { providerID: "openai", id: "gpt-5" }), ["JEV CHECKS RULE"])
  assert.deepEqual(await context(hooks, "test", auto), ["JEV CHECKS RULE", "Begin your reply with: [Jev Auto: gpt-6-sol]."], "Auto steps name their route")

  // Subagents: Jev scores tier, effort and prose once per child session; its model when unsure.
  // The route is switched on the child with the effort as the model variant.
  let body
  const answer = (answers) => async (url, init) => { calls++; body = JSON.parse(init.body); return Response.json({ id: "req", answers, usage: { cost: 0.00003 } }) }
  const pick = (tier, tierConfidence, effort, dialogue) => answer({ tier: { score: tier, confidence: tierConfidence }, effort: { score: effort, confidence: 0.5 }, dialogue: { noul: dialogue } })
  const child = async (sessionID, model, text = "Find where parseConfig is defined and list its callers", agent = "explore", target = hooks) => {
    if (!sessions.has(sessionID)) sessions.set(sessionID, { model: { ...model }, parentID: "parent", agent })
    else sessions.set(sessionID, { ...sessions.get(sessionID), model: { ...model } })
    await prompt(target, sessionID, `You are a subagent spawned by another session.\n${text}`)
    return sessions.get(sessionID).model
  }
  const opus = { providerID: "cliproxy", id: "claude-opus-5-5" }
  const luna = (variant) => ({ providerID: "cliproxy", id: "gpt-6-luna", ...(variant ? { variant } : {}) })
  globalThis.fetch = pick(0.1, 0.9, 0.2, 0.1)
  let n = calls
  assert.deepEqual(await child("c1", opus), luna("low"))
  assert.equal(calls, n + 1)
  assert.deepEqual(Object.keys(body.questions).sort(), ["dialogue", "effort", "tier"])
  assert.equal(body.state.agent, "explore")
  assert.match(body.state.task, /parseConfig/)
  assert.deepEqual(await child("c1", luna("low"), "continue"), luna("low"), "a resumed subagent keeps its route")
  assert.equal(calls, n + 1, "one Jev call per subagent session")
  assert.deepEqual(await child("c1", { providerID: "cliproxy", id: "or-glm-5.3-flash" }, "continue"), luna("low"), "the route holds when something moved the child")
  getFails = true
  assert.deepEqual(await child("c1", opus, "continue"), luna("low"), "a known route needs no lookup")
  getFails = false
  assert.equal(calls, n + 1)
  globalThis.fetch = pick(1.2, 0.8, 1.8, 0.9)
  assert.deepEqual(await child("c2", opus), { ...opus, variant: "high" })
  globalThis.fetch = pick(0.1, 0.3, 0, 0)
  assert.deepEqual(await child("c3", opus), opus, "unsure: its own model")
  globalThis.fetch = pick("x", 0.9, 1, 0.5)
  assert.deepEqual(await child("c4", opus), opus, "malformed answers are rejected")
  for (const [answers, want] of [[[2, 0.9, 2, 0.8], "claude-opus-5-5:high"], [[1.9, 0.9, 1, 0.2], "claude-opus-5-5:medium"], [[0.9, 0.9, 0.4, 0.1], "gpt-6-sol:low"], [[0.2, 0.9, 1.5, 0.1], "gpt-6-luna:high"]]) {
    globalThis.fetch = pick(...answers)
    const m = await child("map-" + want, opus)
    assert.equal(`${m.id}:${m.variant}`, want)
  }
  globalThis.fetch = async () => { throw new Error("down") }
  assert.deepEqual(await child("c5", opus), opus, "Jev down: its own model")
  n = calls
  globalThis.fetch = pick(0, 0.9, 0, 0)
  assert.deepEqual(await child("c6", { providerID: "cliproxy", id: "or-glm-5.3-flash" }), { providerID: "cliproxy", id: "or-glm-5.3-flash" })
  assert.equal(calls, n, "no Jev for a non-OpenAI/Anthropic parent")
  globalThis.fetch = pick(0, 0.2, 0, 0)
  assert.deepEqual(await child("c7", auto), { providerID: "cliproxy", id: "gpt-6-sol" }, "unsure under Auto: Sol")
  // A catalog without Sol keeps the child's model for ordinary work.
  const noSol = await setup(Object.fromEntries(Object.entries(catalog()).filter(([k]) => k !== "gpt-6-sol")))
  globalThis.fetch = pick(1, 0.9, 2, 0.1)
  assert.deepEqual(await child("ns", opus, "design it", "general", noSol.hooks), opus, "model missing from the catalog: its own model")
  // A `route: <model>:<effort>` line pins the subagent without a Jev call (review gate).
  n = calls
  globalThis.fetch = pick(2, 0.9, 2, 0.9)
  const pinned = (sid, line) => child(sid, opus, `Review this diff for bugs.\n${line}\nReport file:line.`, "general")
  assert.deepEqual(await pinned("p1", "route: claude-sonnet-5:high"), { providerID: "cliproxy", id: "claude-sonnet-5", variant: "high" })
  assert.deepEqual(await pinned("p2", "route: gpt-6-luna_low"), luna("low"))
  assert.deepEqual(await pinned("p3", "route: claude-haiku-4-5-20251001:low"), { providerID: "cliproxy", id: "claude-haiku-4-5-20251001" }, "any catalog Haiku can be pinned, without an effort variant")
  assert.equal(calls, n, "pinned routes make no Jev call")
  assert.deepEqual(await child("p1", opus, "continue"), { providerID: "cliproxy", id: "claude-sonnet-5", variant: "high" }, "a pin holds on resume")
  for (const [sid, line] of [["x1", "route: claude-fable-5-1:low"], ["x2", "route: claude-sonnet-5:max"], ["x3", "route: gpt-9:low"], ["x4", "please use route: claude-sonnet-5:high"]]) {
    await pinned(sid, line)
    assert.ok(calls > n, `${line} is not a valid pin, so Jev routes`)
    n = calls
  }
  const noHaiku = await setup(Object.fromEntries(Object.entries(catalog()).filter(([k]) => !k.includes("haiku"))))
  assert.notEqual((await child("nh", opus, "route: claude-haiku-4-5-20251001:low", "general", noHaiku.hooks)).id, "claude-haiku-4-5-20251001", "a pin to a model missing from the catalog is ignored")
  // Families resolve to their highest version in the live catalog, for routes, pins and Auto.
  const grown = { ...catalog(),
    "claude-opus-5": { id: "claude/claude-opus-5" }, "claude-opus-4-8": { id: "claude/claude-opus-4-8" },
    "claude-sonnet-5-5": { id: "claude/claude-sonnet-5-5" }, "claude-haiku-5": { id: "claude/claude-haiku-5" }, "claude-haiku-10": { id: "claude/claude-haiku-10" },
    "gpt-5.6-sol": { id: "codex/gpt-5.6-sol" }, "gpt-6.1-sol": { id: "codex/gpt-6.1-sol" }, "gpt-7-luna": { id: "codex/gpt-7-luna" }, "gpt-6.1-astra": { id: "codex/gpt-6.1-astra" },
    "gpt-9-sol": { id: "openrouter/gpt-9-sol" }, "claude-opus-4-6-1m": { id: "claude/claude-opus-4-6-1m" }, "claude-opus-6-1m": { id: "claude/claude-opus-6-1m" } }
  const newer = await setup(grown)
  assert.equal(newer.registered.models[0].modelID, "codex/gpt-6.1-sol", "Auto falls back to the newest Sol")
  const route = async (sid, answers, text = "do the task") => {
    globalThis.fetch = pick(...answers)
    return (await child(sid, opus, text, "general", newer.hooks)).id
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
  globalThis.fetch = response("trivial", 0.95)
  sessions.set("v-auto", { model: { ...auto } })
  await prompt(newer.hooks, "v-auto", "Fix the typo: deploymnet")
  assert.equal(await routed(newer.hooks, "v-auto"), "codex/gpt-7-luna", "Auto's cheap path uses the newest Luna")
  const sub = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(r => r.mode === "subagent")
  assert.deepEqual(sub.map(r => r.status), ["classified", "classified", "fallback", "fallback", "classified", "classified", "classified", "classified", "fallback", "fallback", "fallback",
    "pinned", "pinned", "pinned", "classified", "classified", "classified", "classified", "classified",
    "classified", "classified", "classified", "classified", "classified", "pinned", "pinned", "pinned", "classified"])
  assert.equal(sub.find(r => r.sessionId === "ns").reason, "model_not_in_catalog")
  assert.ok(!JSON.stringify(sub).includes("parseConfig"), "subagent prompts are not logged")

  // A pin whose switch fails is logged with the model the child really has, and its steps are routed as usual.
  const sonnet = { providerID: "cliproxy", id: "claude-sonnet-5" }
  switchFails = true
  assert.deepEqual(await child("sf", sonnet, "route: opus_high", "general"), sonnet)
  switchFails = false
  const failed = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(r => r.sessionId === "sf")
  assert.deepEqual([failed.at(-1).status, failed.at(-1).actualModel], ["switch_failed", "cliproxy/claude-sonnet-5"])
  const stepCall = async (sessionID, model) => {
    const event = { sessionID, model: { providerID: "cliproxy", id: model.split("/")[1] }, kind: "primary",
      request: new Request("http://proxy/v1/chat/completions", { method: "POST", body: JSON.stringify({ model, messages: [{ role: "user", content: "fix it" }], tools: [{ type: "function", function: { name: "shell" } }] }) }) }
    await hooks["http.request"](event)
    return JSON.parse(await event.request.text()).model
  }
  globalThis.fetch = pick(2, 0.9, 2, 0)
  n = calls
  assert.equal(await stepCall("sf", "claude/claude-sonnet-5"), "claude/claude-opus-5-5", "the failed pin does not exempt the child's steps")
  assert.equal(calls, n + 1)
  assert.deepEqual(await child("p1", opus, "continue"), { providerID: "cliproxy", id: "claude-sonnet-5", variant: "high" })
  n = calls
  assert.equal(await stepCall("p1", "claude/claude-sonnet-5"), "claude/claude-sonnet-5", "a switched pin keeps its model")
  assert.equal(calls, n, "and makes no Jev call")
  sessions.set("p1", { ...sessions.get("p1"), model: { providerID: "cliproxy", id: "claude-sonnet-5", variant: "low" } })
  await prompt(hooks, "p1", "continue")
  assert.deepEqual(sessions.get("p1").model, { providerID: "cliproxy", id: "claude-sonnet-5", variant: "high" }, "same model, other effort: the pin restores its effort")
  switchFails = true
  assert.deepEqual(await child("p1", opus, "continue"), opus)
  switchFails = false
  const retry = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(r => r.sessionId === "p1").at(-1)
  assert.deepEqual([retry.status, retry.actualModel, retry.route], ["switch_failed", "cliproxy/claude-opus-5-5", "claude-sonnet-5:high"], "a failed pin retry is logged with the real model")

  for (const [sid, text, tier, dialogue, want] of [
    ["triage", "Triage the reported issue against the supplied logs", 1, 0.1, "gpt-6-sol"],
    ["validate", "Validate the change using the checks and report failures", 1, 0.1, "gpt-6-sol"],
    ["review", "Review this ordinary diff for bugs", 1, 0.1, "gpt-6-sol"],
    ["deep", "Deep dive into a subtle concurrency bug across services", 2, 0.1, "claude-opus-5-5"],
    ["decide", "Discuss architectural tradeoffs with the user and decide", 1, 0.9, "claude-opus-5-5"],
  ]) {
    globalThis.fetch = pick(tier, 0.9, 2, dialogue)
    assert.equal((await child(sid, opus, text, "general")).id, want)
  }

  const noOpus = await setup(Object.fromEntries(Object.entries(catalog()).filter(([k]) => !k.includes("opus"))))
  globalThis.fetch = response("deep", 0.9)
  sessions.set("no-opus", { model: { ...auto } })
  await prompt(noOpus.hooks, "no-opus", "Fix the typo: deploymnet")
  assert.equal(await routed(noOpus.hooks, "no-opus"), "codex/gpt-6-sol", "missing Opus keeps the valid Sol default")

  // Skills: one Jev call per user message, one line in the system prompt when it fits.
  skills = [{ id: "tdd", name: "tdd", description: "Test-driven development. Use when building features test-first." },
    { id: "grill-me", name: "grill-me", description: "A relentless interview to sharpen a plan." }]
  const userTurn = async (sessionID, text, model = opus, extra) => {
    sessions.set(sessionID, { model: { ...model } })
    await prompt(hooks, sessionID, text, extra)
    assert.deepEqual(sessions.get(sessionID).model, model, "skill picking never edits the session")
  }
  const sys = async (sessionID, model = opus) => (await context(hooks, sessionID, model))
  const skillAnswer = (needs, choice, confidence) => answer({ needs_skill: { noul: needs }, skill: { choice, confidence } })
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.8)
  n = calls
  await userTurn("s1", "Add a retry to the uploader, test first")
  assert.equal(calls, n, "no Jev call in the prompt hook")
  const saveSkills = skills
  skills = []
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], "an empty skill catalog does not pick")
  assert.equal(calls, n)
  skills = saveSkills
  await userTurn("s1", "Add a retry to the uploader, test first")
  let lines = await sys("s1")
  assert.equal(calls, n + 1)
  assert.deepEqual(Object.keys(body.questions.skill.criteria).sort(), ["grill-me", "none", "tdd"])
  assert.match(body.state.request, /retry to the uploader/)
  assert.equal(lines[0], "JEV CHECKS RULE")
  assert.match(lines[1], /"tdd" skill.*skill tool \(id "tdd"\)/)
  assert.deepEqual(await sys("s1"), lines, "later steps of the turn repeat the pick without a call")
  assert.equal(calls, n + 1)
  await userTurn("s1", "", opus, { files: [{ uri: "file:///x" }] })
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
  globalThis.fetch = () => new Promise(r => { release = () => r(Response.json({ id: "r", answers: { needs_skill: { noul: 0.9 }, skill: { choice: "tdd", confidence: 0.9 } }, usage: {} })) })
  await userTurn("s1", "old request")
  const slow = sys("s1")
  for (const deadline = Date.now() + 2000; !release && Date.now() < deadline;) await new Promise(r => setTimeout(r, 1))
  assert.ok(release, "the skill pick never reached Jev within 2 s")
  await userTurn("s1", "", opus, { files: [{ uri: "file:///x" }] })
  release()
  await slow
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"], "a stale pick is dropped")
  // Session lookup down: no skill pick (it may be a subagent), Auto stays on Sol.
  getFails = true
  n = calls
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.9)
  await prompt(hooks, "lf", "test first")
  assert.deepEqual(await sys("lf"), ["JEV CHECKS RULE"])
  sessions.set("lf-auto", { model: { ...auto } })
  await prompt(hooks, "lf-auto", "Fix the typo")
  assert.equal(await routed(hooks, "lf-auto"), "codex/gpt-6-sol")
  assert.equal(calls, n, "no Jev call when the session lookup fails")
  getFails = false
  globalThis.fetch = async () => { throw new Error("down") }
  await userTurn("s1", "y")
  assert.deepEqual(await sys("s1"), ["JEV CHECKS RULE"])
  n = calls
  globalThis.fetch = skillAnswer(0.9, "tdd", 0.9)
  const glm = { providerID: "cliproxy", id: "or-glm-5.3-flash" }
  await userTurn("g1", "test first please", glm)
  assert.deepEqual(await sys("g1", glm), [])
  await child("k1", opus, "test first please")
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

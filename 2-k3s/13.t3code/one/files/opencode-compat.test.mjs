import assert from "node:assert/strict"
import plugin from "./opencode-compat.js"

// A fake OpenCode 2 tool editor holding the native skill tool, and plugin storage.
const storage = new Map()
const skillCalls = []
const tools = new Map([["skill", { name: "skill", input: { type: "object", properties: { id: { type: "string" } }, required: ["id"] },
  execute: async (input, context) => { skillCalls.push({ input, context }); return { content: `loaded ${input.id}` } } }]])
let transform
await plugin.setup({
  tool: { transform: async (cb) => { transform = cb } },
  storage: { get: async (k) => storage.get(k), set: async (k, v) => { storage.set(k, v) } },
})
transform({ add: (tool) => tools.set(tool.name, tool), update: (id, fn) => fn(tools.get(id)) })

// todowrite: a direct tool, the whole list each time, kept per session.
const write = tools.get("todowrite"), read = tools.get("todoread")
assert.equal(write.options.codemode, false, "offered to the model directly, not through Code Mode")
assert.equal(read.options.codemode, false)
assert.deepEqual(write.input.required, ["todos"])
const todos = [{ content: "port the plugin", status: "in_progress", priority: "high" }, { content: "test it", status: "pending", priority: "medium" }]
const result = await write.execute({ todos }, { sessionID: "s1" })
assert.deepEqual(result.output, { todos })
assert.deepEqual(JSON.parse(result.content), todos, "the model sees the list as text")
assert.equal(result.metadata.open, 2)
assert.deepEqual(storage.get("todos/s1"), todos)
await write.execute({ todos: [{ ...todos[0], status: "completed" }] }, { sessionID: "s1" })
assert.equal((await read.execute({}, { sessionID: "s1" })).output.todos[0].status, "completed", "the latest list replaces the old one")
assert.deepEqual((await read.execute({}, { sessionID: "s2" })).output, { todos: [] }, "sessions do not share lists")

// skill: `name` (OpenCode 1) and `id` (OpenCode 2) both reach the native tool as `id`.
const skill = tools.get("skill")
assert.ok(skill.input.properties.name && skill.input.properties.id)
assert.equal(skill.input.required, undefined, "either argument is enough")
assert.equal((await skill.execute({ name: "tdd" }, { sessionID: "s1" })).content, "loaded tdd")
assert.equal((await skill.execute({ id: "grill-me" }, { sessionID: "s1" })).content, "loaded grill-me")
assert.deepEqual(skillCalls.map(c => c.input), [{ id: "tdd" }, { id: "grill-me" }])
assert.equal(skillCalls[0].context.sessionID, "s1", "the tool context passes through")
console.log("opencode-compat: all checks passed")

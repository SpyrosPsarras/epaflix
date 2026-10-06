const TODO_ITEM = {
  type: "object",
  properties: {
    content: { type: "string", description: "Brief description of the task" },
    status: { type: "string", description: "Current status of the task: pending, in_progress, completed, cancelled" },
    priority: { type: "string", description: "Priority level of the task: high, medium, low" },
  },
  required: ["content", "status", "priority"],
}
const TODOS = { type: "object", properties: { todos: { type: "array", description: "The updated todo list", items: TODO_ITEM } }, required: ["todos"] }
const key = (sessionID) => `todos/${sessionID}`

export default {
  id: "opencode-compat",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "todowrite",
        description: "Create and update the structured task list for the current session. Use it for work with three or more steps; keep exactly one task in_progress, mark each task completed as soon as it is done, and send the whole list every time.",
        input: TODOS,
        output: TODOS,
        // Plugin tools default to Code Mode; these stay in the model's own tool list, as in OpenCode 1.
        options: { codemode: false },
        execute: async ({ todos }, { sessionID }) => {
          await ctx.storage.set(key(sessionID), todos)
          return { output: { todos }, content: JSON.stringify(todos, null, 2), metadata: { open: todos.filter(t => !["completed", "cancelled"].includes(t.status)).length } }
        },
      })
      editor.add({
        name: "todoread",
        description: "Read the current session's task list.",
        input: { type: "object", properties: {} },
        output: TODOS,
        options: { codemode: false },
        execute: async (_, { sessionID }) => {
          const todos = (await ctx.storage.get(key(sessionID))) ?? []
          return { output: { todos }, content: JSON.stringify(todos, null, 2) }
        },
      })
      editor.update("skill", (tool) => {
        const execute = tool.execute
        tool.input = { type: "object", properties: { id: { type: "string", description: "The skill id" }, name: { type: "string", description: "Alias of id, for OpenCode 1 callers" } } }
        tool.execute = (input, context) => execute({ id: input.id ?? input.name }, context)
      })
    })
  },
}

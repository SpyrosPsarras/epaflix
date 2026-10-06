import { spawn } from "node:child_process"
import { realpathSync } from "node:fs"
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { basename, dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

export const stop = async (child, grace = 5000) => {
  const exited = child.exitCode !== null || child.signalCode !== null ? Promise.resolve() : new Promise(r => child.once("exit", r))
  const group = (signal) => { try { process.kill(-child.pid, signal) } catch (e) { if (e.code !== "ESRCH") throw e } }
  group("SIGTERM")
  await new Promise(r => { const timer = setTimeout(r, grace); exited.then(() => { clearTimeout(timer); r() }) })
  group("SIGKILL")
  await exited
}

export const serve = async (binary, { cwd, env, tries = 240 }) => {
  // The binary is <modules>/@opencode/cli-<platform>/bin/opencode; the released client sits beside it.
  const { OpenCode } = await import(pathToFileURL(join(dirname(dirname(dirname(binary))), "client/dist/promise/index.js")).href)
  const password = crypto.randomUUID()
  const port = 20000 + Math.floor(Math.random() * 20000)
  const child = spawn(binary, ["serve", "--hostname", "127.0.0.1", "--port", String(port)], { cwd, detached: true, stdio: ["ignore", "pipe", "pipe"],
    env: { ...env, OPENCODE_PASSWORD: password } })
  let logs = ""
  child.stdout.on("data", d => logs += d)
  child.stderr.on("data", d => logs += d)
  const client = OpenCode.make({ baseUrl: `http://127.0.0.1:${port}`, headers: { Authorization: `Basic ${Buffer.from(`opencode:${password}`).toString("base64")}` } })
  for (let i = 0; ; i++) {
    try { await client.server.info({ signal: AbortSignal.timeout(5000) }); break } catch {
      if (i > tries || child.exitCode !== null) {
        await stop(child)
        throw new Error(`opencode did not start: ${logs.slice(-2000)}`)
      }
    }
    await sleep(500)
  }
  return { child, client }
}

const check = async (binary, configDir, required) => {
  const root = await mkdtemp(join(tmpdir(), "opencode-plugin-check-"))
  let child
  try {
    const config = join(root, "config", "opencode"), project = join(root, "project")
    // A copy of the config without MCP servers, and private data and state dirs:
    // the check never opens the real database or connects to MCP servers.
    await cp(configDir, config, { recursive: true, filter: (src) => basename(src) !== "node_modules" })
    const json = JSON.parse(await readFile(join(config, "opencode.json"), "utf8"))
    delete json.mcp
    await writeFile(join(config, "opencode.json"), JSON.stringify(json))
    await mkdir(project)
    const server = await serve(binary, { cwd: project,
      env: { ...process.env, XDG_CONFIG_HOME: join(root, "config"), XDG_DATA_HOME: join(root, "data"), XDG_STATE_HOME: join(root, "state") } })
    child = server.child
    // Plugins load in the background after a location's first use, packages only
    // after their install; a failed one is listed with its error.
    let plugins = []
    for (const deadline = Date.now() + 180000; Date.now() < deadline; await sleep(500)) {
      plugins = (await server.client.plugin.list({ location: { directory: project } }, { signal: AbortSignal.timeout(10000) })).data
      if (required.every(id => plugins.some(p => p.id === id)) || plugins.some(p => p.state.status !== "active")) break
    }
    const problems = [
      ...required.filter(id => !plugins.some(p => p.id === id)).map(id => `${id}: not loaded`),
      ...plugins.filter(p => p.state.status !== "active").map(p => `${p.id ?? JSON.stringify(p.source)}: ${p.state.error}`),
    ]
    if (problems.length) {
      console.error(`opencode-plugin-check: ${problems.join("; ")}`)
      process.exitCode = 1
    } else console.log(`opencode-plugin-check: ${required.join(", ")} active`)
  } finally {
    if (child) await stop(child)
    await rm(root, { recursive: true, force: true })
  }
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [binary, configDir, ...required] = process.argv.slice(2).map((a, i) => i < 2 ? resolve(a) : a)
  if (!binary || !configDir || !required.length) throw new Error("usage: opencode-plugin-check.mjs <opencode binary> <config dir> <plugin id>...")
  await check(binary, configDir, required)
}

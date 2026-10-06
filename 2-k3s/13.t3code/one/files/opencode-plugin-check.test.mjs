import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { readFileSync } from "node:fs"
import { once } from "node:events"
import { stop } from "./opencode-plugin-check.mjs"

const stubborn = `process.on("SIGTERM", () => {}); setInterval(() => {}, 1000);`
const leader = spawn(process.execPath, ["-e", `${stubborn}
  const worker = require("node:child_process").spawn(process.execPath, ["-e", ${JSON.stringify(stubborn + "console.log('up')")}], { stdio: ["ignore", "pipe", "ignore"] })
  worker.stdout.once("data", () => console.log(worker.pid))`], { detached: true, stdio: ["ignore", "pipe", "ignore"] })
process.on("exit", () => { try { process.kill(-leader.pid, "SIGKILL") } catch {} })
const worker = Number(String((await once(leader.stdout, "data"))[0]).trim())
const alive = (pid) => { try { return readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1][0] !== "Z" } catch { return false } }
assert.ok(alive(leader.pid) && alive(worker), "both processes run before stop")

const start = Date.now()
await stop(leader, 300)
for (let i = 0; i < 50 && alive(worker); i++) await new Promise(r => setTimeout(r, 20))
assert.equal(leader.signalCode, "SIGKILL", "the SIGTERM-ignoring server is killed after the grace period")
assert.ok(!alive(worker), "its process group is killed too")
assert.ok(Date.now() - start < 3000, "stop returns soon after the grace period")
await stop(leader, 300)
console.log("PASS: stop kills a server and its process group that ignore SIGTERM")

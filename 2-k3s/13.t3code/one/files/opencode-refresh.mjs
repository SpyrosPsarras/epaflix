// T3 gives OpenCode 5 s to list its models at a status check and checks again
// only when asked. Right after a restart the pod is busy and OpenCode can miss
// that window, so T3 showed its models as unavailable until someone pressed
// refresh. This presses it: server.refreshProviders for the opencode instance,
// every 30 s until OpenCode lists models, for about 10 minutes.
import { execFileSync } from "node:child_process"

const base = "http://127.0.0.1:3773"
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

const refresh = async () => {
  const token = execFileSync("t3", ["auth", "session", "issue", "--base-dir", process.env.T3_HOME, "--ttl", "5m", "--token-only"],
    { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 30000 }).trim()
  const issued = await fetch(`${base}/api/auth/websocket-ticket`, {
    method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: "{}",
    signal: AbortSignal.timeout(10000),
  })
  if (!issued.ok) throw new Error(`websocket ticket HTTP ${issued.status}`)
  const ws = new WebSocket(`${base.replace(/^http/, "ws")}/ws?orchestrationProtocol=2&wsTicket=${encodeURIComponent((await issued.json()).ticket)}`)
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("websocket not open in 10 s")), 10000)
      ws.onopen = () => { clearTimeout(timer); resolve() }
      ws.onerror = () => { clearTimeout(timer); reject(new Error("websocket refused")) }
    })
    const id = crypto.randomUUID()
    ws.send(JSON.stringify({ _tag: "Request", id, tag: "server.refreshProviders", payload: { instanceId: "opencode" }, headers: [] }))
    const exit = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("no answer in 60 s")), 60000)
      ws.onclose = () => { clearTimeout(timer); reject(new Error("websocket closed")) }
      ws.onmessage = message => {
        let data
        try { data = JSON.parse(message.data) } catch { return }
        if (data.requestId !== id || (data._tag !== "Exit" && data._tag !== "Defect")) return
        clearTimeout(timer)
        data._tag === "Exit" && data.exit._tag === "Success" ? resolve(data.exit.value) : reject(new Error(JSON.stringify(data).slice(0, 300)))
      }
    })
    return exit.providers.find(provider => (provider.instanceId ?? provider.id) === "opencode")
  } finally {
    ws.close()
  }
}

for (let attempt = 1; attempt <= 20; attempt++) {
  await sleep(30000)
  try {
    const opencode = await refresh()
    if (opencode?.models?.length) {
      console.log(`t3env: OpenCode status refreshed: ${opencode.message ?? opencode.probe?.message ?? `${opencode.models.length} models`} (try ${attempt})`)
      process.exit(0)
    }
    console.error(`t3env: OpenCode status try ${attempt}: ${opencode?.message ?? opencode?.probe?.message ?? "no opencode instance"}`)
  } catch (error) {
    console.error(`t3env: OpenCode status try ${attempt} failed: ${error.message}`)
  }
}
console.error("t3env: OpenCode still lists no models after 20 refreshes; press refresh in Settings > Providers")
process.exit(1)

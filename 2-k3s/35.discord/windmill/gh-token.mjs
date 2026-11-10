import { sign } from "node:crypto";
import { readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");

export function appJwt(appId, privateKeyPem, nowSeconds) {
  const unsigned = `${encode({ alg: "RS256", typ: "JWT" })}.${encode({ iat: nowSeconds - 60, exp: nowSeconds + 540, iss: appId })}`;
  return `${unsigned}.${sign("sha256", Buffer.from(unsigned), privateKeyPem).toString("base64url")}`;
}

export async function refresh(env) {
  const jwt = appJwt(env.GITHUB_APP_ID, readFileSync(env.GITHUB_APP_KEY_FILE, "utf8"), Math.floor(Date.now() / 1000));
  const response = await fetch(`https://api.github.com/app/installations/${env.GITHUB_INSTALLATION_ID}/access_tokens`, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${jwt}`,
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ repositories: ["epaflix"], permissions: { contents: "write", pull_requests: "write", checks: "read", statuses: "read", actions: "read" } }),
  });
  if (!response.ok) throw new Error(`GitHub returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
  const { token } = await response.json();
  if (!token) throw new Error("GitHub returned no token");
  const target = env.GH_TOKEN_FILE ?? "/run/gh/token";
  rmSync(`${target}.tmp`, { force: true });
  writeFileSync(`${target}.tmp`, token, { mode: 0o440 });
  renameSync(`${target}.tmp`, target);
}

if (import.meta.main) {
  for (;;) {
    try {
      await refresh(process.env);
      console.log("gh-token: refreshed");
      await sleep(30 * 60_000);
    } catch (error) {
      console.error(`gh-token: ${error.message} ${error.cause?.code ?? ""}`);
      await sleep(60_000);
    }
  }
}

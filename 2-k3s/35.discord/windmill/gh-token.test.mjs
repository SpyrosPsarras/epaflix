import assert from "node:assert/strict";
import { generateKeyPairSync, verify } from "node:crypto";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { appJwt, refresh } from "./gh-token.mjs";

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const pem = privateKey.export({ type: "pkcs1", format: "pem" });
const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});

function decode(part) {
  return JSON.parse(Buffer.from(part, "base64url").toString());
}

function setup(response) {
  const dir = mkdtempSync(join(tmpdir(), "gh-token-"));
  writeFileSync(join(dir, "key.pem"), pem);
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return response;
  };
  const env = {
    GITHUB_APP_ID: "12345",
    GITHUB_INSTALLATION_ID: "67890",
    GITHUB_APP_KEY_FILE: join(dir, "key.pem"),
    GH_TOKEN_FILE: join(dir, "token"),
  };
  return { env, calls };
}

test("appJwt signs an RS256 JWT for the App", () => {
  const jwt = appJwt("12345", pem, 1_700_000_000);
  const [header, payload, signature] = jwt.split(".");
  assert.deepEqual(decode(header), { alg: "RS256", typ: "JWT" });
  assert.deepEqual(decode(payload), { iat: 1_699_999_940, exp: 1_700_000_540, iss: "12345" });
  assert.ok(verify("sha256", Buffer.from(`${header}.${payload}`), publicKey, Buffer.from(signature, "base64url")));
});

test("refresh writes the token and asks only for epaflix", async () => {
  const { env, calls } = setup(new Response(JSON.stringify({ token: "t" }), { status: 201 }));
  await refresh(env);
  assert.equal(readFileSync(env.GH_TOKEN_FILE, "utf8"), "t");
  assert.equal(statSync(env.GH_TOKEN_FILE).mode & 0o777, 0o440);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.github.com/app/installations/67890/access_tokens");
  assert.equal(calls[0].init.method, "POST");
  assert.match(calls[0].init.headers.Authorization, /^Bearer [\w-]+\.[\w-]+\.[\w-]+$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    repositories: ["epaflix"],
    permissions: { contents: "write", pull_requests: "write", checks: "read", statuses: "read", actions: "read" },
  });
});

test("refresh keeps the old token when GitHub refuses", async () => {
  const { env } = setup(new Response(JSON.stringify({ message: "Bad credentials" }), { status: 401 }));
  writeFileSync(env.GH_TOKEN_FILE, "old");
  await assert.rejects(refresh(env), /401/);
  assert.equal(readFileSync(env.GH_TOKEN_FILE, "utf8"), "old");
});

test("refresh replaces a token file left read-only by an earlier run", async () => {
  const { env } = setup(new Response(JSON.stringify({ token: "new" }), { status: 201 }));
  writeFileSync(env.GH_TOKEN_FILE, "old", { mode: 0o440 });
  writeFileSync(`${env.GH_TOKEN_FILE}.tmp`, "stale", { mode: 0o440 });
  await refresh(env);
  assert.equal(readFileSync(env.GH_TOKEN_FILE, "utf8"), "new");
});

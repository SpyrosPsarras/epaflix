import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { gunzipSync, gzipSync } from "node:zlib";

import { committedAt, commitOf, due } from "./deploy.mjs";

const git = (...args) => execFileSync("git", args, { cwd: import.meta.dirname, maxBuffer: 1 << 26 });

test("reads the commit from a git archive tarball", () => {
  const tarball = git("archive", "--format=tar.gz", "HEAD", "--", "README.md");
  assert.equal(commitOf(tarball), git("rev-parse", "HEAD").toString().trim());
});

test("rejects a tarball without a commit id", () => {
  const tarball = git("archive", "--format=tar.gz", "HEAD^{tree}", "--", "README.md");
  assert.throws(() => commitOf(tarball), /no commit id/);
});

test("reads the commit time from a git archive tarball", () => {
  const tarball = git("archive", "--format=tar.gz", "HEAD", "--", "README.md");
  assert.equal(committedAt(tarball), Number(git("log", "-1", "--format=%ct", "HEAD").toString().trim()));
});

test("rejects a tarball without a positive commit time", () => {
  const tar = gunzipSync(git("archive", "--format=tar.gz", "HEAD", "--", "README.md"));
  const size = parseInt(tar.subarray(124, 136).toString(), 8);
  const entry = 512 + Math.ceil(size / 512) * 512;
  for (const value of ["00000000000", "invalid"]) {
    tar.fill(0, entry + 136, entry + 148);
    tar.write(value, entry + 136);
    assert.throws(() => committedAt(gzipSync(tar)), /no commit time/);
  }
});

test("deploys commits younger than 15 minutes", () => {
  const now = new Date("2026-11-09T12:03:00Z");
  assert.equal(due(now.getTime() / 1000 - 14 * 60 - 59, now), true);
  assert.equal(due(now.getTime() / 1000 - 15 * 60, now), false);
  assert.equal(due(now.getTime() / 1000 - 15 * 60 - 1, now), false);
});

test("deploys older commits every 10 UTC minutes", () => {
  const now = new Date("2026-11-09T12:20:00Z");
  assert.equal(due(now.getTime() / 1000 - 3600, now), true);
  assert.equal(due(now.getTime() / 1000 - 3600, new Date("2026-11-09T14:20:00+02:00")), true);
  assert.equal(due(now.getTime() / 1000 - 3600, new Date("2026-11-09T12:21:00Z")), false);
});

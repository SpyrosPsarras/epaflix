import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";

import { commitOf } from "./deploy.mjs";

const git = (...args) => execFileSync("git", args, { cwd: import.meta.dirname, maxBuffer: 1 << 26 });

test("reads the commit from a git archive tarball", () => {
  const tarball = git("archive", "--format=tar.gz", "HEAD", "--", "README.md");
  assert.equal(commitOf(tarball), git("rev-parse", "HEAD").toString().trim());
});

test("rejects a tarball without a commit id", () => {
  const tarball = git("archive", "--format=tar.gz", "HEAD^{tree}", "--", "README.md");
  assert.throws(() => commitOf(tarball), /no commit id/);
});

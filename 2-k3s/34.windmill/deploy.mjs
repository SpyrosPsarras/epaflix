import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";

const SOURCE = "https://codeload.github.com/SpyrosPsarras/epaflix/tar.gz/refs/heads/main";
const TOP = "epaflix-main";
const WORKSPACE = "2-k3s/34.windmill/workspace";

// git archive stores the commit id as the "comment" of a leading pax global header.
export function commitOf(tarball) {
  const tar = gunzipSync(tarball);
  const size = parseInt(tar.subarray(124, 136).toString(), 8);
  const header = tar[156] === 0x67 ? tar.subarray(512, 512 + size).toString() : "";
  const commit = header.match(/^\d+ comment=([0-9a-f]{40})$/m)?.[1];
  if (!commit) throw new Error("tarball has no commit id");
  return commit;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.status !== 0) throw new Error(result.error?.message ?? `${command} exited with ${result.status ?? result.signal}`);
}

if (import.meta.main) {
  let commit = null;
  try {
    const response = await fetch(SOURCE);
    if (!response.ok) throw new Error(`download failed with HTTP ${response.status}`);
    const tarball = Buffer.from(await response.arrayBuffer());
    commit = commitOf(tarball);
    const work = mkdtempSync(join(tmpdir(), "deploy-"));
    const archive = join(work, "source.tar.gz");
    const source = join(work, "source");
    const cli = join(work, "cli");
    writeFileSync(archive, tarball);
    mkdirSync(source);
    run("tar", ["-xzf", archive, "-C", source, "--strip-components=1", `${TOP}/${WORKSPACE}`]);
    mkdirSync(cli);
    for (const file of ["package.json", "package-lock.json"]) copyFileSync(join(import.meta.dirname, file), join(cli, file));
    run("npm", ["ci", "--ignore-scripts", "--no-audit", "--no-fund"], { cwd: cli });
    run(join(cli, "node_modules/.bin/wmill"), ["sync", "push", "--yes", "--auto-metadata", "--keep-deleted"], {
      cwd: join(source, WORKSPACE),
    });
    console.log(JSON.stringify({ commit, outcome: "deployed" }));
  } catch (error) {
    console.log(JSON.stringify({ commit, outcome: "failed", error: error.message }));
    process.exitCode = 1;
  }
}

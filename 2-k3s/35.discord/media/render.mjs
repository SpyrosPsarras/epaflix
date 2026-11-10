import { copyFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";

const [configDir, stateDir] = process.argv.slice(2);

function fail(message) {
  console.error(`render: ${message}`);
  process.exit(1);
}

function discordId(name) {
  const value = process.env[name] ?? "";
  if (!/^\d+$/.test(value)) fail(`${name} must be a numeric Discord ID`);
  return value;
}

let config = readFileSync(`${configDir}/openclaw.json`, "utf8");
for (const name of ["DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID"]) {
  config = config.replaceAll(`__${name}__`, discordId(name));
}
const leftover = config.match(/__[A-Z0-9_]+__/);
if (leftover) fail(`unrendered placeholder ${leftover[0]}`);
for (const [, name] of config.matchAll(/\$\{([A-Z0-9_]+)\}/g)) {
  if (!process.env[name]) fail(`${name} is empty`);
}
JSON.parse(config);

mkdirSync(`${stateDir}/workspace`, { recursive: true });
writeFileSync(`${stateDir}/openclaw.json.tmp`, config, { mode: 0o600 });
renameSync(`${stateDir}/openclaw.json.tmp`, `${stateDir}/openclaw.json`);
copyFileSync(`${configDir}/AGENTS.md`, `${stateDir}/workspace/AGENTS.md`);

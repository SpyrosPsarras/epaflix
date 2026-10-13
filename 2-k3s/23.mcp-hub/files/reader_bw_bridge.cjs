#!/usr/bin/env node
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");

const CAPABILITY = Object.freeze({
    bridge: "restricted-sync-v2",
    client_version: "2026.8.0",
    bundle_sha256: "688a23275876477fa5eddd42a1a61a25f7168b3cd164bd634235531904cc87f6",
});
const STOCK_CALL = "yield this.syncService.fullSync(normalizedOptions.force, true);";
const READER_CALL = "yield this.syncService.fullSync(true, {allowThrowOnError: true, skipTokenRefresh: true});";
const STOCK_MIGRATION_CHECK = "const needsMigration = yield this.needsMigrations(userId);";
const READER_MIGRATION_CHECK = `let needsMigration;
            try {
                needsMigration = yield this.needsMigrations(userId);
            } catch {
                throw new Error("restricted reader account maintenance check failed");
            }
            if (needsMigration !== "noMigrationNeeded") {
                throw new Error("restricted reader account requires owner maintenance");
            }`;

function adaptBundle(packageJson, bundle) {
    if (packageJson.name !== "@bitwarden/cli" || packageJson.version !== CAPABILITY.client_version) {
        throw new Error("reader client version mismatch");
    }
    const source = bundle.toString("utf8");
    if (source.split(STOCK_CALL).length !== 2) {
        throw new Error("reader client sync call mismatch");
    }
    if (source.split(STOCK_MIGRATION_CHECK).length !== 2) {
        throw new Error("reader client migration check mismatch");
    }
    if (crypto.createHash("sha256").update(bundle).digest("hex") !== CAPABILITY.bundle_sha256) {
        throw new Error("reader client bundle mismatch");
    }
    return source.replace(STOCK_CALL, READER_CALL).replace(STOCK_MIGRATION_CHECK, READER_MIGRATION_CHECK);
}

function clientArgs(args) {
    const command = JSON.stringify(args);
    if (command === '["reader-sync"]') {
        return ["sync", "--force"];
    }
    if ([ '["--reader-capabilities"]', '["login","--apikey"]',
          '["unlock","--passwordenv","BW_PASSWORD","--raw"]', '["status"]',
          '["list","organizations"]', '["list","collections"]', '["list","items"]' ].includes(command) ||
        args.length === 3 && args[0] === "config" && args[1] === "server" && args[2].startsWith("https://") ||
        args.length === 6 && args[0] === "get" && args[1] === "attachment" &&
        /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(args[2]) && args[3] === "--itemid" &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(args[4]) && args[5] === "--raw") {
        return args;
    }
    throw new Error("unsupported reader command");
}

function main() {
    const args = clientArgs(process.argv.slice(2));
    const packageRoot = path.join(__dirname, "node_modules", "@bitwarden", "cli");
    const filename = path.join(packageRoot, "build", "bw.js");
    const source = adaptBundle(JSON.parse(fs.readFileSync(path.join(packageRoot, "package.json"))),
                              fs.readFileSync(filename));
    if (args[0] === "--reader-capabilities") {
        process.stdout.write(JSON.stringify(CAPABILITY) + "\n");
        return;
    }
    process.argv = [process.argv[0], filename, ...args];
    const client = new Module(filename, module);
    client.filename = filename;
    client.paths = Module._nodeModulePaths(path.dirname(filename));
    client._compile(source, filename);
}

module.exports = {CAPABILITY, STOCK_CALL, READER_CALL, STOCK_MIGRATION_CHECK, READER_MIGRATION_CHECK, adaptBundle, clientArgs};
if (require.main === module) {
    try {
        main();
    } catch {
        process.stderr.write("restricted reader bridge unavailable\n");
        process.exitCode = 1;
    }
}

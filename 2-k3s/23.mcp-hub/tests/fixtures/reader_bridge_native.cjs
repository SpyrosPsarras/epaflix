"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const bridge = require("../../files/reader_bw_bridge.cjs");

const packageRoot = "/tools/node_modules/@bitwarden/cli";
const filename = path.join(packageRoot, "build/bw.js");
const original = fs.readFileSync(filename);
const packageJson = JSON.parse(fs.readFileSync(path.join(packageRoot, "package.json")));
const adapted = bridge.adaptBundle(packageJson, original);
const USER = "00000000-0000-4000-8000-000000000001";
const ORG = "00000000-0000-4000-8000-000000000002";
const COLLECTION = "00000000-0000-4000-8000-000000000003";
const ITEM = "00000000-0000-4000-8000-000000000005";
const digest = data => crypto.createHash("sha256").update(data).digest("hex");

function compiled(source) {
    const entry = "\nmain();\n";
    assert.equal(source.split(entry).length, 2);
    const exports = "\nmodule.exports = {SyncCommand, DefaultSyncService, NodeApiService, TokenService, BaseProgram, external_rxjs_namespaceObject, AuthenticationStatus, UnlockCommand, LoginCommand, DefaultEncryptedMigrator, MinimumKdfMigration, PBKDF2KdfConfig, FeatureFlag, src_ClientType};\n";
    const client = new Module(filename, module);
    client.filename = filename;
    client.paths = Module._nodeModulePaths(path.dirname(filename));
    client._compile(source.replace(entry, exports), filename);
    return client.exports;
}

function fixture(native) {
    const {of, BehaviorSubject} = native.external_rxjs_namespaceObject;
    const noop = async () => {};
    const stores = {collections: {}, ciphers: {}, organizations: {}};
    const calls = {identity: 0, sync: 0, crypto: 0, logout: [], requests: [], handlers: []};
    const controls = {identityLimit: 10, identityStatus: 200, syncStatus: 200, cryptoFailure: false, revoked: false, generation: 0};
    const jwt = seconds => "eyJhbGciOiJub25lIn0." + Buffer.from(JSON.stringify({sub: USER, exp: Math.floor(Date.now() / 1000) + seconds})).toString("base64url") + ".synthetic";
    let accessToken = jwt(3600);
    let stamp = null;
    const token = Object.assign(Object.create(native.TokenService.prototype), {
        getAccessToken: async id => { assert.equal(id, USER); return accessToken; },
        getRefreshToken: async () => null,
        getClientId: async () => "user." + USER,
        getClientSecret: async () => "synthetic-api-secret",
        setAccessToken: async value => { accessToken = value; return value; },
        getSecurityStamp: async () => stamp,
        setSecurityStamp: async value => { stamp = value; },
    });
    const account = {activeAccount$: of({id: USER}), setAccountEmailVerified: noop,
                     setAccountCreationDate: noop, setAccountVerifyNewDeviceLogin: noop};
    const platform = {getDevice: () => 8, getDeviceString: () => "synthetic-offline-device",
                      getClientType: () => "cli", supportsAutofill: () => false, isDev: () => false,
                      getApplicationVersionNumber: async () => "2026.8.0", packageType: async () => null};
    const environment = {getIdentityUrl: () => "https://identity.example.test",
                         getApiUrl: () => "https://api.example.test", hasBaseUrl: () => true};
    const log = {error: noop, warning: noop, debug: noop};
    const api = new native.NodeApiService(token, platform,
        {environment$: of(environment), getEnvironment$: () => of(environment)},
        {getAppId: async () => "synthetic-device"}, noop, log,
        async reason => calls.logout.push(reason),
        {getVaultTimeoutActionByUserId$: () => of("lock"), getVaultTimeoutByUserId$: () => of(15)}, account);
    api.nativeFetch = async request => {
        calls.requests.push({url: request.url, authorization: request.headers.get("authorization")});
        if (request.url.endsWith("/connect/token")) {
            calls.identity++;
            const body = new URLSearchParams(await request.text());
            assert.equal(body.get("grant_type"), "client_credentials");
            assert.equal(body.get("client_id"), "user." + USER);
            assert.equal(body.get("client_secret"), "synthetic-api-secret");
            assert.equal(body.get("deviceIdentifier"), "synthetic-device");
            const status = calls.identity > controls.identityLimit ? 429 : controls.identityStatus;
            return new Response(JSON.stringify(status === 200 ? {access_token: jwt(3600), token_type: "Bearer", expires_in: 3600, Kdf: 0, KdfIterations: 600000} : {error: "invalid_grant"}),
                                {status, headers: {"content-type": "application/json"}});
        }
        assert.equal(request.url, "https://api.example.test/sync?excludeDomains=true");
        assert.equal(request.headers.get("authorization"), "Bearer " + accessToken);
        assert.equal(await token.tokenNeedsRefresh(USER), false);
        calls.sync++;
        const payload = controls.syncStatus === 200 ? {
            Profile: {Id: USER, SecurityStamp: "synthetic-stamp", CreationDate: "2020-01-01T00:00:00Z",
                      Organizations: [{Id: ORG, Type: 2, Status: 2, Enabled: true}], Providers: [], ProviderOrganizations: []},
            Collections: controls.revoked ? [] : [{Id: COLLECTION, OrganizationId: ORG, Name: "synthetic-collection", ReadOnly: true, Manage: false}],
            Ciphers: controls.revoked ? [] : [{Id: ITEM, OrganizationId: ORG, CollectionIds: [COLLECTION], Type: 1,
                       Name: "synthetic-generation-" + controls.generation, RevisionDate: "2020-01-01T00:00:00Z"}],
            Folders: [], Sends: [], Policies: [],
        } : {Message: "synthetic-sync-failure"};
        return new Response(JSON.stringify(payload), {status: controls.syncStatus, headers: {"content-type": "application/json"}});
    };
    const lastSync = new BehaviorSubject(null);
    const replace = key => ({replace: async value => { stores[key] = value; }});
    const sync = Object.assign(Object.create(native.DefaultSyncService.prototype), {
        accountService: account, authService: {authStatusFor$: () => of(native.AuthenticationStatus.Unlocked)},
        apiService: api, tokenService: token, logService: log, syncInProgress: false,
        inFlightApiCalls: {refreshToken: null, sync: null},
        messageSender: {send: (name, data) => { if (name === "syncCompleted") calls.completed = data; }},
        stateProvider: {getUser: () => ({state$: lastSync, update: async fn => lastSync.next(fn())})},
        keyService: {setProviderKeys: noop, setOrgKeys: noop}, avatarService: {setSyncAvatarColor: noop},
        configService: {getFeatureFlag: async () => false},
        billingAccountProfileStateService: {setHasPremium: noop},
        keyConnectorService: {setUsesKeyConnector: noop, convertAccountRequired$: of(false)},
        userDecryptionOptionsService: {userDecryptionOptionsById$: () => of({hasMasterPassword: true})},
        providerService: {save: noop}, organizationService: replace("organizations"),
        folderService: {replace: noop}, collectionService: replace("collections"),
        cipherService: {...replace("ciphers"), clear: async () => { stores.ciphers = {}; }},
        sendService: {replace: noop}, domainSettingsService: {setEquivalentDomains: noop},
        policyService: {replace: noop}, newPolicyService: {replace: noop},
        sdkService: {userClient$: () => of({take: () => ({
            value: {crypto_sync_handler: () => ({on_sync: async () => {
                calls.crypto++;
                if (controls.cryptoFailure) throw new Error("synthetic-crypto-rejection");
            }})},
            [Symbol.dispose]: () => {},
        })})},
    });
    const handlers = ["runCryptoSyncHandler", "syncProfile", "syncFolders", "syncCollections", "syncCiphers", "syncSends", "syncSettings", "syncPolicies", "syncNewPolicies", "setLastSync"];
    for (const name of handlers) {
        const actual = sync[name];
        sync[name] = function (...args) { calls.handlers.push(name); return actual.apply(this, args); };
    }
    return {calls, controls, stores, lastSync, sync, token, handlers,
            expire: () => { accessToken = jwt(-1); }, command: new native.SyncCommand(sync)};
}

function failure(native, reply) {
    assert.equal(reply.success, false);
    assert.equal(reply.data, undefined);
    const quiet = process.env.BW_QUIET;
    process.env.BW_QUIET = "true";
    new native.BaseProgram({}).processResponse(reply);
    assert.equal(process.exitCode, 1);
    process.exitCode = 0;
    if (quiet === undefined) delete process.env.BW_QUIET;
    else process.env.BW_QUIET = quiet;
}

function compatibility() {
    assert.equal(digest(original), bridge.CAPABILITY.bundle_sha256);
    assert.equal(digest(Buffer.from(adapted.replace(bridge.READER_CALL, bridge.STOCK_CALL).replace(bridge.READER_MIGRATION_CHECK, bridge.STOCK_MIGRATION_CHECK))), digest(original));
    assert.throws(() => bridge.adaptBundle({...packageJson, name: "other"}, original), /version mismatch/);
    assert.throws(() => bridge.adaptBundle({...packageJson, version: "0.0.0"}, original), /version mismatch/);
    assert.throws(() => bridge.adaptBundle(packageJson, Buffer.concat([original, Buffer.from("\n")])), /bundle mismatch/);
    assert.throws(() => bridge.adaptBundle(packageJson, Buffer.from(original.toString().replace(bridge.STOCK_CALL, "void 0;"))), /sync call mismatch/);
    assert.throws(() => bridge.adaptBundle(packageJson, Buffer.from(original.toString() + bridge.STOCK_CALL)), /sync call mismatch/);
    assert.throws(() => bridge.adaptBundle(packageJson, Buffer.from(original.toString().replace(bridge.STOCK_MIGRATION_CHECK, "void 0;"))), /migration check mismatch/);
    assert.throws(() => bridge.adaptBundle(packageJson, Buffer.from(original.toString() + bridge.STOCK_MIGRATION_CHECK)), /migration check mismatch/);
    assert.deepEqual(bridge.clientArgs(["reader-sync"]), ["sync", "--force"]);
    for (const args of [["sync", "--force"], ["serve"], ["create", "item"], ["reader-sync", "--last"], ["login", "--apikey", "--raw"]]) {
        assert.throws(() => bridge.clientArgs(args), /unsupported/);
    }
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "reader-native-"));
    try {
        const executable = path.join(root, "reader-bw.cjs");
        fs.copyFileSync(require.resolve("../../files/reader_bw_bridge.cjs"), executable);
        const pkg = path.join(root, "node_modules/@bitwarden/cli");
        fs.mkdirSync(path.join(pkg, "build"), {recursive: true});
        const env = {PATH: "/usr/local/bin:/usr/bin:/bin", HOME: root, BITWARDENCLI_APPDATA_DIR: root, BW_NOINTERACTION: "true"};
        const run = args => spawnSync(process.execPath, [executable, ...args], {env, encoding: "utf8", timeout: 10000});
        const cases = [
            [packageJson, original, true],
            [{...packageJson, version: "0.0.0"}, original, false],
            [packageJson, Buffer.concat([original, Buffer.from("\n")]), false],
            [packageJson, Buffer.from(original.toString().replace(bridge.STOCK_CALL, "void 0;")), false],
            [packageJson, Buffer.from(original.toString().replace(bridge.STOCK_MIGRATION_CHECK, "void 0;")), false],
            [packageJson, Buffer.from(original.toString() + bridge.STOCK_MIGRATION_CHECK), false],
            [packageJson, Buffer.from(original.toString().replace(bridge.STOCK_MIGRATION_CHECK,
                bridge.STOCK_MIGRATION_CHECK + "\nvoid 0;")), false],
        ];
        for (const [metadata, source, valid] of cases) {
            fs.writeFileSync(path.join(pkg, "package.json"), JSON.stringify(metadata));
            fs.writeFileSync(path.join(pkg, "build/bw.js"), source);
            const result = run(["--reader-capabilities"]);
            assert.equal(result.status, valid ? 0 : 1, result.stderr);
            if (valid) assert.deepEqual(JSON.parse(result.stdout), bridge.CAPABILITY);
            else {
                const login = run(["login", "--apikey"]);
                assert.equal(login.status, 1);
                assert.equal(login.stdout, "");
                assert.equal(login.stderr, "restricted reader bridge unavailable\n");
            }
            assert.equal(digest(fs.readFileSync(path.join(pkg, "build/bw.js"))), digest(source));
        }
        const stock = spawnSync(process.execPath, [filename, "--reader-capabilities"], {env, encoding: "utf8", timeout: 15000});
        assert.equal(stock.status, 1, stock.stderr);
        assert.match(stock.stderr, /unknown option/);
        assert.equal(digest(fs.readFileSync(filename)), digest(original));
    } finally {
        fs.rmSync(root, {recursive: true, force: true});
    }
    console.log("N1 compatibility: wrong name/version/hash, missing/duplicate/incompatible sync/migration seams and stock command rejected before login; original CLI unchanged");
}

async function maintenanceFixture(native, enabled, belowMinimum, options = {}) {
    const {of} = native.external_rxjs_namespaceObject;
    const noop = () => {};
    const calls = {login: 0, unlock: 0, mutations: 0, sync: 0, logout: 0};
    const sentinel = new Error("synthetic SDK mutation stopped");
    const detectionFailure = new Error("synthetic-private-detection-error");
    const detect = name => {if (options.failure === name) throw detectionFailure;};
    const log = {info: noop, error: noop, mark: noop, measure: noop};
    let lowKdf = belowMinimum;
    const kdf = {getKdfConfig: async () => {
        detect("kdf");
        return new native.PBKDF2KdfConfig(lowKdf ? 100000 : native.PBKDF2KdfConfig.ITERATIONS.min);
    }};
    const sync = {fullSync: async force => {
        calls.sync++;
        if (!force) detect("sync");
        if (options.syncedCurrent) lowKdf = false;
    }};
    const master = {userHasMasterPassword: async () => {detect("master"); return true;}};
    const sdk = {userClient$: () => of({take: () => ({value: {
        user_crypto_management: () => ({change_kdf: async () => {
            calls.mutations++;
            throw sentinel;
        }}),
    }, [Symbol.dispose]: noop})})};
    const migrator = new native.DefaultEncryptedMigrator(kdf, log,
        {getFeatureFlag: async flag => {
            assert.equal(flag, native.FeatureFlag.ForceUpdateKDFSettings);
            detect("flag");
            return enabled;
        }}, master, sync, {}, {}, {},
        {getClientType: () => native.src_ClientType.Cli}, {}, {}, sdk);
    assert.equal(migrator.migrations.length, 1);
    assert.ok(migrator.migrations[0].migration instanceof native.MinimumKdfMigration);
    const account = {activeAccount$: of({id: USER})};
    const logout = async () => {calls.logout++;};
    const keyConnector = {
        convertAccountRequired$: of(Boolean(options.convert)),
        requiresDomainConfirmation$: () => of(options.confirm ? {keyConnectorUrl: "https://connector.example.test"} : null),
        getUsesKeyConnector: async () => false,
        migrateUser: async () => {calls.mutations++; throw sentinel;},
        convertNewSsoUserToKeyConnector: async () => {calls.mutations++; throw sentinel;},
    };
    const i18n = {t: value => value};
    let command;
    if (options.login) {
        command = new native.LoginCommand({logIn: async credentials => {
            assert.equal(credentials.clientId, "user." + USER);
            assert.equal(credentials.clientSecret, "synthetic-api-secret");
            calls.login++;
            return {userId: USER};
        }}, {authStatusFor$: () => of(native.AuthenticationStatus.Locked)}, {}, {}, {}, {}, {},
        account, {}, sync, keyConnector, logout, {}, i18n, master, {}, migrator);
        command.validatedParams = async () => {};
    } else {
        command = new native.UnlockCommand(account, log, keyConnector, {}, {}, logout, i18n, migrator,
            {unlockWithMasterPassword: async () => {calls.unlock++;}});
        command.setNewSessionKey = async () => {};
        command.successResponse = async () => ({success: true});
    }
    let reply, error;
    try {
        reply = options.login ? await command.run(undefined, undefined, {apikey: true}) :
            await command.run(undefined, {passwordenv: "BW_PASSWORD"});
    } catch (caught) {
        error = caught;
    }
    return {calls, reply, error, sentinel, detectionFailure};
}

async function maintenance(stock, patched) {
    process.env.BW_PASSWORD = "synthetic-master-password";
    process.env.BW_NOINTERACTION = "true";
    process.env.BW_CLIENTID = "user." + USER;
    process.env.BW_CLIENTSECRET = "synthetic-api-secret";
    for (const [enabled, belowMinimum] of [[false, true], [true, false], [true, true]]) {
        const old = await maintenanceFixture(stock, enabled, belowMinimum);
        const fresh = await maintenanceFixture(patched, enabled, belowMinimum);
        const required = enabled && belowMinimum;
        console.log(JSON.stringify({enabled, belowMinimum, stock: old.calls, reader: fresh.calls}));
        assert.equal(old.calls.mutations, required ? 1 : 0);
        assert.equal(old.error, required ? old.sentinel : undefined);
        assert.equal(fresh.calls.mutations, 0, "reader unlock must not mutate account KDF");
        if (required) {
            assert.ok(fresh.error, "required maintenance must refuse, not silently skip");
            assert.notEqual(fresh.error, fresh.sentinel);
        } else {
            assert.equal(fresh.error, undefined);
            assert.equal(fresh.reply.success, true);
            assert.equal(fresh.calls.unlock, 1);
        }
    }
    console.log("N7 maintenance: stock attempts change_kdf; reader refuses; flag-off/current-KDF unlock normally");
    for (const login of [false, true]) {
        for (const name of ["master", "kdf", "flag", "sync"]) {
            const fresh = await maintenanceFixture(patched, true, true, {login, failure: name});
            assert.equal(fresh.calls.mutations, 0);
            if (login) {
                assert.equal(fresh.error, undefined);
                assert.equal(fresh.reply.message, "Error: restricted reader account maintenance check failed");
                failure(patched, fresh.reply);
            } else {
                assert.equal(fresh.error.message, "restricted reader account maintenance check failed");
                assert.notEqual(fresh.error, fresh.detectionFailure);
            }
        }
        const refreshed = await maintenanceFixture(patched, true, true, {login, syncedCurrent: true});
        assert.equal(refreshed.error, undefined);
        assert.equal(refreshed.reply.success, true);
        assert.equal(refreshed.calls.mutations, 0);
    }
    console.log("N8 detection: master/KDF/flag/sync errors refuse sanitized in login and unlock; externally updated KDF accepts");
    for (const [enabled, belowMinimum] of [[false, true], [true, false], [true, true]]) {
        const old = await maintenanceFixture(stock, enabled, belowMinimum, {login: true});
        const fresh = await maintenanceFixture(patched, enabled, belowMinimum, {login: true});
        assert.equal(old.error, undefined);
        assert.equal(old.reply.success, true, "stock API-key login skips password-required migration without password");
        assert.equal(old.calls.mutations, 0);
        assert.equal(fresh.calls.login, 1);
        assert.equal(fresh.calls.unlock, 0);
        assert.equal(fresh.calls.mutations, 0);
        assert.equal(fresh.error, undefined);
        if (enabled && belowMinimum) {
            assert.equal(fresh.reply.message, "Error: restricted reader account requires owner maintenance");
            failure(patched, fresh.reply);
        } else {
            assert.equal(fresh.reply.success, true);
        }
    }
    for (const native of [stock, patched]) {
        const convert = await maintenanceFixture(native, true, true, {convert: true});
        const confirm = await maintenanceFixture(native, true, true, {login: true, confirm: true});
        for (const f of [convert, confirm]) {
            assert.equal(f.error, undefined);
            failure(native, f.reply);
            assert.equal(f.calls.mutations, 0);
            assert.equal(f.calls.logout, 1);
        }
    }
    console.log("N9 lifecycle: actual API-key LoginCommand refuses maintenance before unlock; BW_NOINTERACTION rejects Key Connector conversion/confirmation without their mutation calls");
}

async function main() {
    await maintenance(compiled(original.toString()), compiled(adapted));
    compatibility();
    const stock = compiled(original.toString());
    const patched = compiled(adapted);
    global.fetch = () => { throw new Error("network is forbidden in native proof"); };
    const old = fixture(stock);
    for (let i = 0; i < 10; i++) {
        const reply = await old.command.run({force: true});
        assert.equal(reply.success, true, reply.message);
    }
    assert.equal(old.calls.identity, 10);
    assert.equal(old.calls.sync, 10);
    const unhandled = [];
    const capture = error => unhandled.push(error);
    process.on("unhandledRejection", capture);
    failure(stock, await old.command.run({force: true}));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(old.calls.identity, 11);
    assert.equal(old.calls.sync, 10);
    assert.equal(unhandled.length, 1);
    assert.equal(unhandled[0].statusCode, 429);
    console.log("N2 stock: 11 forced reads make 11 identity requests; default budget fails read 11 before data fetch");

    const fresh = fixture(patched);
    for (let i = 0; i < 11; i++) {
        fresh.controls.generation = i;
        assert.equal((await fresh.command.run({force: false})).success, true);
        assert.equal(fresh.stores.ciphers[ITEM].name, "synthetic-generation-" + i);
    }
    assert.equal(fresh.calls.identity, 0);
    assert.equal(fresh.calls.sync, 11);
    assert.equal(fresh.calls.crypto, 11);
    assert.equal(fresh.calls.handlers.length, fresh.handlers.length * 11);
    for (let i = 0; i < 11; i++) assert.deepEqual(fresh.calls.handlers.slice(i * fresh.handlers.length, (i + 1) * fresh.handlers.length), fresh.handlers);
    assert.ok(fresh.lastSync.value instanceof Date);
    assert.equal(fresh.sync.inFlightApiCalls.sync, null);
    assert.equal(fresh.sync.inFlightApiCalls.refreshToken, null);
    console.log("N3 bridge: 11 reads force 11 fresh fetches with 0 identity renewals; real handlers and lastSync run");

    fresh.expire();
    assert.equal(await fresh.token.tokenNeedsRefresh(USER), true);
    assert.equal((await fresh.command.run({})).success, true);
    assert.equal(fresh.calls.identity, 1);
    assert.equal(fresh.calls.sync, 12);
    assert.equal(await fresh.token.tokenNeedsRefresh(USER), false);
    console.log("N4 expiry: real JWT expiry check renews via real API-key identity request, then fetches fresh data");

    for (const status of [401, 403, 500]) {
        const f = fixture(patched);
        assert.equal((await f.command.run({})).success, true);
        const stamp = f.lastSync.value;
        f.controls.syncStatus = status;
        const reply = await f.command.run({});
        failure(patched, reply);
        assert.equal(f.lastSync.value, stamp);
        assert.equal(f.calls.crypto, 1);
        assert.equal(f.calls.sync, status === 401 ? 3 : 2);
        assert.deepEqual(f.calls.logout, status === 500 ? [] : ["invalidAccessToken"]);
    }
    const renewal = fixture(patched);
    assert.equal((await renewal.command.run({})).success, true);
    const before = renewal.lastSync.value;
    renewal.expire();
    renewal.controls.identityStatus = 400;
    const rejected = await renewal.command.run({});
    failure(patched, rejected);
    assert.equal(renewal.calls.identity, 1);
    assert.equal(renewal.calls.sync, 1);
    assert.equal(renewal.lastSync.value, before);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(unhandled.length, 2);
    assert.equal(unhandled[1].statusCode, 400);
    process.removeListener("unhandledRejection", capture);
    console.log("N5 errors: renewal failure and 401/403/500 fail without success payload or new lastSync; real BaseProgram sets exit 1; upstream 401 retry preserved");

    fresh.controls.revoked = true;
    assert.equal((await fresh.command.run({})).success, true);
    assert.deepEqual(fresh.stores.collections, {});
    assert.deepEqual(fresh.stores.ciphers, {});
    const cryptoFail = fixture(patched);
    cryptoFail.controls.cryptoFailure = true;
    assert.equal((await cryptoFail.command.run({})).success, false);
    assert.deepEqual(cryptoFail.calls.handlers, ["runCryptoSyncHandler"]);
    assert.equal(cryptoFail.lastSync.value, null);
    console.log("N6 grants/crypto: real handlers replace revoked collection/cipher state; crypto rejection stops subsequent handlers");
    assert.equal(digest(fs.readFileSync(filename)), digest(original));
    console.log("Native proof passed. Synthetic backend, token storage, SDK crypto collaborator and stores only. No real account/login/network. Login/unlock commands use synthetic auth and SDK/storage collaborators.");
}

main().catch(error => { console.error(error); process.exitCode = 1; });

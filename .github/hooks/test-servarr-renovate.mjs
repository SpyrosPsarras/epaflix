// Run with RENOVATE_ROOT pointing to the installed renovate package, version 43.33.2.
// Uses Renovate's actual extraction, versioning and rule application, without network access.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

assert(process.env.RENOVATE_ROOT, 'Set RENOVATE_ROOT to the installed renovate package');
const load = (path) => import(pathToFileURL(resolve(process.env.RENOVATE_ROOT, `dist/${path}.js`)));
const { extractPackageFile } = await load('modules/manager/kustomize/extract');
const { applyPackageRules } = await load('util/package-rules/index');
const { get } = await load('modules/versioning/index');
const { getUpdateType } = await load('workers/repository/process/lookup/update-type');
const packageFile = '2-k3s/08.servarr/kustomization.yaml';
const { packageRules } = JSON.parse(readFileSync('.github/renovate.json', 'utf8'));
const { deps } = extractPackageFile(readFileSync(packageFile, 'utf8'), packageFile, {});
const moving = new Set([
  'ghcr.io/thephaseless/byparr',
  'ghcr.io/spyrospsarras/airvpn-bluetit',
  'ghcr.io/spyrospsarras/vpn-picker',
]);
const manual = new Set([
  'ghcr.io/fredrikburmester/streamystats-aio',
]);
// Self-built overlay images (TEMP OVERRIDE entries): excluded from Renovate in
// .github/renovate.json, so extraction must see exactly these names disabled.
const disabled = new Set([
  'ghcr.io/spyrospsarras/jellysweep',
  'ghcr.io/spyrospsarras/lingarr',
]);
for (const dep of deps) {
  assert(!dep.skipReason, `${dep.depName}: ${dep.skipReason}`);
  assert.match(dep.currentDigest, /^sha256:[a-f0-9]{64}$/);
  const base = { ...dep, packageFile, manager: 'kustomize', packageRules, automerge: false };
  const configured = await applyPackageRules(base, 'test');
  if (configured.enabled === false) {
    assert(disabled.has(dep.depName), `${dep.depName}: unexpectedly disabled`);
    continue;
  }
  const versioning = get(configured.versioning ?? 'docker');
  if (!moving.has(dep.depName) && !manual.has(dep.depName)) {
    assert(versioning.isValid(dep.currentValue), `${dep.depName}: invalid release tag`);
  }
  for (const updateType of ['digest', 'patch', 'minor', 'major']) {
    const result = await applyPackageRules({ ...base, updateType }, 'test');
    const expected = !manual.has(dep.depName) && ['digest', 'patch'].includes(updateType);
    assert.equal(result.automerge, expected, `${dep.depName}: ${updateType}`);
  }
  console.log(`PASS ${dep.depName}: extraction and digest/patch/minor/major policy`);
}

for (const [name, current, next, expected] of [
  ['lscr.io/linuxserver/sonarr', '4.0.19', '5.0.0', 'major'],
  ['lscr.io/linuxserver/sonarr', '4.0.19', '4.1.0', 'minor'],
  ['lscr.io/linuxserver/sonarr', '4.0.19', '4.0.20', 'patch'],
  ['ghcr.io/hotio/unpackerr', 'release-0.15.2', 'release-0.15.3', 'patch'],
  ['ghcr.io/hotio/unpackerr', 'release-0.15.2', 'release-1.0.0', 'major'],
  ['qbittorrentofficial/qbittorrent-nox', '5.2.3-1', '5.2.3-2', 'patch'],
  ['qbittorrentofficial/qbittorrent-nox', '5.2.3-1', '6.0.0-2', 'major'],
]) {
  const config = await applyPackageRules({ packageName: name, packageFile, packageRules }, 'test');
  const api = get(config.versioning ?? 'docker');
  assert(api.isCompatible(next, current), `${name}: ${next} is filtered out`);
  assert(api.isGreaterThan(next, current));
  assert.equal(getUpdateType(config, api, current, next), expected);
  console.log(`PASS ${name}: ${current} -> ${next} = ${expected}`);
}
assert(!get('docker').isCompatible('5.0.0.1-ls400', '4.0.19.2979-ls324'));
console.log('PASS LinuxServer build suffix regression: use clean version tags');

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Role, Status, identities, setupConfigActive, atMostOne, agentEmail, agentGrant, validateAgent, validateMembers, validateIsolation, runCommand } = require('../setup.cjs');

test('ambiguous matches stop instead of creating another account or org', () => {
  const identity = identities.organizations[0];
  assert.equal(atMostOne([], () => true), undefined);
  assert.equal(atMostOne([identity], org => org.name === identity.name), identity);
  assert.throws(() => atMostOne([identity, identity], org => org.name === identity.name));
});

test('setup requires closed registration, no email verification and the temporary config', () => {
  const settings = [
    { name: 'ORG_CREATION_USERS', valueFrom: { secretKeyRef: { name: 'vaultwarden-org-setup', key: 'BW_EMAIL' } } },
    { name: '_ENABLE_SMTP', value: 'false' },
    { name: 'SIGNUPS_ALLOWED', value: 'false' },
    { name: 'SIGNUPS_VERIFY', value: 'false' },
  ];
  assert.equal(setupConfigActive(settings), true);
  for (const setting of settings) {
    assert.equal(setupConfigActive(settings.filter(entry => entry !== setting)), false);
    assert.equal(setupConfigActive(settings.map(entry => entry === setting ? { name: entry.name, value: 'true' } : entry)), false);
  }
});

test('agent addresses use only the shared identity pattern', () => {
  for (const { name } of identities.organizations) {
    assert.equal(agentEmail(name), identities.agentEmail.replace('{name}', name));
  }
  assert.throws(() => agentEmail('unknown-organization'));
});

test('agent grant is limited to an explicit read-write collection', () => {
  const address = agentEmail(identities.organizations[0].name);
  assert.deepEqual(agentGrant(address, 'collection-id'), {
    emails: [address], type: Role.user, accessAll: false,
    collections: [{ id: 'collection-id', readOnly: false, hidePasswords: false, manage: false }],
    groups: [], permissions: {},
  });
});

test('existing elevated or read-only agent membership fails closed', () => {
  const member = { type: Role.user, accessAll: false, status: Status.confirmed, collections: [{ id: 'collection-id', readOnly: false, hidePasswords: false, manage: false }] };
  assert.doesNotThrow(() => validateAgent(member, 'collection-id'));
  for (const changed of [{ type: Role.owner }, { accessAll: true }, { collections: [] }, { collections: [{ ...member.collections[0], readOnly: true }] }, { collections: [{ ...member.collections[0], manage: true }] }]) {
    assert.throws(() => validateAgent({ ...member, ...changed }, 'collection-id'));
  }
});

test('setup deletion waits for exactly the intended confirmed owner and agent', () => {
  const owner = 'owner@example.invalid';
  const agent = agentEmail(identities.organizations[0].name);
  const setup = identities.setupEmail;
  const members = [{ email: owner, type: Role.owner, status: Status.confirmed }, { email: agent, type: Role.user, status: Status.confirmed }, { email: setup, type: Role.owner, status: Status.confirmed }];
  assert.doesNotThrow(() => validateMembers(members, owner, agent, setup));
  assert.throws(() => validateMembers(members.map(member => member.type === Role.owner ? { ...member, status: Status.accepted } : member), owner, agent, setup));
  assert.throws(() => validateMembers([...members, { email: 'other@example.invalid', type: Role.owner, status: Status.confirmed }], owner, agent, setup));
});

test('isolation rejects extra organizations and missing own canary', () => {
  assert.doesNotThrow(() => validateIsolation([{ id: 'own' }], [{ id: 'canary', organizationId: 'own' }], 'own', 'canary', 'foreign'));
  assert.throws(() => validateIsolation([{ id: 'own' }, { id: 'other' }], [{ id: 'canary' }], 'own', 'canary', 'foreign'));
  assert.throws(() => validateIsolation([{ id: 'own' }], [], 'own', 'canary', 'foreign'));
  assert.throws(() => validateIsolation([{ id: 'own' }], [{ id: 'canary', organizationId: 'own' }, { id: 'foreign', organizationId: 'other' }], 'own', 'canary', 'foreign'));
});

test('failed child processes never expose their output or arguments', () => {
  const privateValue = 'synthetic-sensitive-value';
  assert.throws(() => runCommand(process.execPath, ['-e', `process.stderr.write('${privateValue}'); process.exit(1)`]), error => !String(error).includes(privateValue));
});

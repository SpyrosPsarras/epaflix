const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const identities = require('./identities.json');
const Checkpoint = Object.freeze(identities.checkpoints);

const Role = Object.freeze({ owner: 0, admin: 1, user: 2, manager: 3 });
const Status = Object.freeze({ invited: 0, accepted: 1, confirmed: 2 });
const ItemType = Object.freeze({ secureNote: 2 });
const SecureNoteType = Object.freeze({ generic: 0 });
class SetupError extends Error {}
class WaitingForOwner extends SetupError {}

function atMostOne(records, matches) {
  const found = records.filter(matches);
  if (found.length > 1) throw new SetupError('Ambiguous setup state');
  return found[0];
}

function setupConfigActive(environment) {
  const value = name => environment.find(entry => entry.name === name)?.value;
  const creators = environment.find(entry => entry.name === 'ORG_CREATION_USERS')?.valueFrom?.secretKeyRef;
  const setupOnlyCreator = creators?.name === 'vaultwarden-org-setup' && creators?.key === 'BW_EMAIL';
  const smtpDisabled = value('_ENABLE_SMTP') === 'false';
  const registrationClosed = value('SIGNUPS_ALLOWED') === 'false';
  const verificationDisabled = value('SIGNUPS_VERIFY') === 'false';
  return setupOnlyCreator && smtpDisabled && registrationClosed && verificationDisabled;
}

function agentEmail(name) {
  if (!identities.organizations.some(organization => organization.name === name)) throw new SetupError('Invalid setup identity');
  return identities.agentEmail.replace('{name}', name);
}

function agentGrant(email, collectionId) {
  return { emails: [email], type: Role.user, accessAll: false, collections: [{ id: collectionId, readOnly: false, hidePasswords: false, manage: false }], groups: [], permissions: {} };
}

function validateAgent(member, collectionId) {
  const collections = member.collections || [];
  const grant = atMostOne(collections, collection => collection.id === collectionId);
  const ordinaryMember = member.type === Role.user && member.status === Status.confirmed;
  const scopedCollection = collections.length === 1 && Boolean(grant);
  const readWrite = grant?.readOnly === false && grant?.hidePasswords === false && grant?.manage === false;
  const noExtraGrants = member.accessAll === false && !(member.groups || []).length && !Object.values(member.permissions || {}).some(Boolean);
  if (!ordinaryMember || !scopedCollection || !readWrite || !noExtraGrants) throw new SetupError('Unexpected agent permissions');
}

function validateMembers(members, ownerEmail, agentAddress, setupEmail) {
  const expectedCount = members.some(member => member.email === setupEmail) ? 3 : 2;
  const unexpectedMember = members.some(member => ![ownerEmail, agentAddress, setupEmail].includes(member.email));
  if (members.length !== expectedCount || unexpectedMember) throw new SetupError('Unexpected organization members');
  for (const [email, role] of [[ownerEmail, Role.owner], [agentAddress, Role.user]]) {
    const member = atMostOne(members, candidate => candidate.email === email);
    if (!member || member.type !== role || member.status !== Status.confirmed) throw new SetupError('Owner or agent not confirmed');
  }
}

function validateIsolation(organizations, items, organizationId, ownCanaryId, foreignCanaryId) {
  const ownOrganizationOnly = organizations.length === 1 && organizations[0].id === organizationId;
  const ownCanaryVisible = items.some(item => item.id === ownCanaryId && item.organizationId === organizationId);
  const foreignItemVisible = items.some(item => item.id === foreignCanaryId || item.organizationId !== organizationId);
  if (!ownOrganizationOnly || !ownCanaryVisible || foreignItemVisible) throw new SetupError('Agent isolation check failed');
}

function runCommand(binary, args, options = {}) {
  try {
    return execFileSync(binary, args, { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], timeout: 120000, maxBuffer: 8 * 1024 * 1024, ...options }).trim();
  } catch {
    throw new SetupError('Child process failed: ' + path.basename(binary) + ' ' + args[0]);
  }
}

class Cluster {
  constructor(namespace) { this.namespace = namespace; }
  get(kind, name, namespace = this.namespace) {
    return JSON.parse(runCommand('kubectl', ['-n', namespace, 'get', kind, name, '-o', 'json']));
  }
  secret(name, namespace = this.namespace) {
    return Object.fromEntries(Object.entries(this.get('secret', name, namespace).data || {}).map(([key, encoded]) => [key, Buffer.from(encoded, 'base64').toString()]));
  }
  save(name, fields, namespace = this.namespace) {
    const directory = fs.mkdtempSync('/work/vault-patch-');
    try {
      const file = path.join(directory, 'patch.json');
      fs.writeFileSync(file, JSON.stringify({ stringData: fields }), { mode: 0o600 });
      runCommand('kubectl', ['-n', namespace, 'patch', 'secret', name, '--type=merge', '--patch-file=' + file, '-o', 'name']);
    } finally { fs.rmSync(directory, { recursive: true, force: true }); }
  }
}

class Vault {
  constructor(server, scratchDirectory) {
    this.server = new URL(server).origin;
    if (!server.startsWith('https://')) throw new SetupError('HTTPS required');
    this.scratchDirectory = scratchDirectory;
  }
  async start(adminToken) {
    const { chromium, request } = require('playwright');
    this.browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    this.admin = await request.newContext();
    const response = await this.admin.post(this.server + '/admin', { form: { token: adminToken } });
    if (response.status() === 429) throw new SetupError('Admin login rate limited. Wait five minutes before rerunning.');
    if (response.status() !== 200) throw new SetupError('Admin login failed');
  }
  async adminUser(email) {
    const response = await this.admin.get(this.server + '/admin/users/by-mail/' + encodeURIComponent(email));
    if (response.status() === 404) return undefined;
    if (response.status() !== 200) throw new SetupError('Admin lookup failed');
    return response.json();
  }
  async deleteUser(userId) {
    const response = await this.admin.post(this.server + `/admin/users/${userId}/delete`, { data: {} });
    if (!response.ok()) throw new SetupError('Setup account deletion failed');
  }
  async register(credentials) {
    const user = await this.adminUser(credentials.BW_EMAIL);
    if (user?.key) return;
    if (!user) {
      const response = await this.admin.post(this.server + '/admin/invite', { data: { email: credentials.BW_EMAIL } });
      if (response.status() !== 200) throw new SetupError('Admin invite failed');
    }
    const context = await this.context();
    try {
      const page = await context.newPage();
      await page.goto(this.server + '/#/register');
      await page.locator('#register-start_form_input_email').fill(credentials.BW_EMAIL);
      await page.getByRole('button', { name: 'Continue', exact: true }).click();
      await page.locator('#input-password-form_new-password').fill(credentials.BW_PASSWORD);
      await page.locator('#input-password-form_new-password-confirm').fill(credentials.BW_PASSWORD);
      await page.locator('#input-password-form_check-for-breaches').uncheck();
      const registration = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/accounts/register/finish'));
      await page.getByRole('button', { name: 'Create account', exact: true }).click();
      if ((await registration).status() !== 200) throw new SetupError('Registration failed');
    } finally { await context.close(); }
  }
  async context() {
    const context = await this.browser.newContext();
    context.setDefaultTimeout(60000);
    await context.route('**/*', route => new URL(route.request().url()).origin === this.server ? route.continue() : route.abort());
    return context;
  }
  async login(credentials, label) {
    const context = await this.context();
    const page = await context.newPage();
    await page.goto(this.server + '/#/login');
    await page.locator('input[type=email]:visible').fill(credentials.BW_EMAIL);
    await page.getByRole('button', { name: 'Continue', exact: true }).click();
    await page.locator('input[type=password]:visible').fill(credentials.BW_PASSWORD);
    const loginResponse = page.waitForResponse(response => response.url().endsWith('/identity/connect/token') && response.status() === 200);
    await page.getByRole('button', { name: 'Log in', exact: true }).click();
    const accessToken = (await (await loginResponse).json()).access_token;
    const environment = { ...process.env, BITWARDENCLI_APPDATA_DIR: path.join(this.scratchDirectory, label), BW_PASSWORD: credentials.BW_PASSWORD, BW_NOINTERACTION: 'true' };
    const bw = (args, input) => runCommand('bw', args, { env: environment, input });
    bw(['config', 'server', this.server]);
    environment.BW_SESSION = bw(['login', credentials.BW_EMAIL, '--passwordenv', 'BW_PASSWORD', '--raw']);
    return { page, context, bw, json: args => JSON.parse(bw(args)), accessToken };
  }
  async api(session, method, route, payload, allowMissing = false) {
    const response = await session.context.request.fetch(this.server + '/api' + route, { method, headers: { Authorization: 'Bearer ' + session.accessToken }, data: payload });
    if (allowMissing && response.status() === 404) return;
    if (!response.ok()) throw new SetupError('Vault API request failed');
    const body = await response.text();
    return body ? JSON.parse(body) : undefined;
  }
  async createOrg(session, name, email) {
    const page = session.page;
    await page.goto(this.server + '/#/vault');
    for (let attempt = 0; attempt < 120; attempt++) {
      if (await page.getByText('New organization', { exact: true }).isVisible()) break;
      for (const [role, label] of [['button', 'Add it later'], ['link', 'Skip to web app'], ['button', 'Skip']]) {
        const target = page.getByRole(role, { name: label, exact: true });
        if (await target.isVisible()) await target.click();
      }
      await page.waitForTimeout(500);
    }
    await page.getByText('New organization', { exact: true }).click();
    await page.getByLabel('Organization name').fill(name);
    await page.locator('input[type=email]:visible').fill(email);
    const creation = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/api/organizations'));
    await page.getByRole('button', { name: 'Submit', exact: true }).click();
    if ((await creation).status() !== 200) throw new SetupError('Organization creation failed');
    session.bw(['sync']);
  }
  async close() {
    await this.admin?.dispose();
    await this.browser?.close();
  }
}

async function ensureOrg(context, identity, create) {
  const { cluster, setupSession, setupAccount, vault } = context;
  const { name, namespace } = identity;
  const credentials = cluster.secret('vaultwarden-agent', namespace);
  const email = agentEmail(name);
  const conflictingAddress = credentials.BW_EMAIL && credentials.BW_EMAIL !== email;
  if (!credentials.BW_PASSWORD || conflictingAddress) throw new SetupError('Unexpected agent Secret');
  credentials.BW_EMAIL = email;
  let organization = atMostOne(setupSession.json(['list', 'organizations']), org => org.name === name);
  if (!organization && create) {
    await vault.createOrg(setupSession, name, setupAccount.BW_EMAIL);
    organization = atMostOne(setupSession.json(['list', 'organizations']), org => org.name === name);
  }
  const conflictingOrganization = credentials.BW_ORGANIZATION_ID && credentials.BW_ORGANIZATION_ID !== organization?.id;
  if (!organization || conflictingOrganization) throw new SetupError('Organization mismatch');
  const collections = setupSession.json(['list', 'org-collections', '--organizationid', organization.id]);
  let collection = atMostOne(collections, candidate => candidate.name === name);
  if (!collection && create && collections.length === 1 && collections[0].name === 'Default collection') {
    collection = { ...collections[0], name };
    setupSession.bw(['edit', 'org-collection', collection.id, '--organizationid', organization.id], Buffer.from(JSON.stringify(collection)).toString('base64'));
  }
  const conflictingCollection = credentials.BW_COLLECTION_ID && credentials.BW_COLLECTION_ID !== collection?.id;
  if (!collection || collections.length !== 1 || conflictingCollection) throw new SetupError('Collection mismatch');
  if (create) cluster.save('vaultwarden-agent', { BW_EMAIL: email, BW_SERVER: vault.server, BW_ORGANIZATION_ID: organization.id, BW_COLLECTION_ID: collection.id }, namespace);
  return { name, credentials, organization, collection, members: () => setupSession.json(['list', 'org-members', '--organizationid', organization.id]) };
}

async function ensureMember(context, organization, email, role, grant) {
  const { vault, setupSession } = context;
  let member = atMostOne(organization.members(), candidate => candidate.email === email);
  if (!member) {
    await vault.api(setupSession, 'POST', `/organizations/${organization.organization.id}/users/invite`, grant);
    member = atMostOne(organization.members(), candidate => candidate.email === email);
  }
  if (!member || member.type !== role) throw new SetupError('Unexpected membership role');
  if (member.status === Status.accepted) {
    try { setupSession.bw(['confirm', 'org-member', member.id, '--organizationid', organization.organization.id]); }
    catch (error) {
      if (role === Role.owner) throw new WaitingForOwner('Owner confirmation is blocked. Setup account retained.');
      throw error;
    }
    member = atMostOne(organization.members(), candidate => candidate.email === email);
  }
  if (member.status !== Status.confirmed) {
    if (role === Role.owner) throw new WaitingForOwner('Owner has not accepted the invitation. Setup account retained.');
    throw new SetupError('Agent not confirmed');
  }
  return member;
}

async function setupOrganizations(context) {
  for (const identity of identities.organizations) {
    console.log(identity.name + ': ensuring organization and memberships');
    const organization = await ensureOrg(context, identity, true);
    await context.vault.register(organization.credentials);
    await ensureMember(context, organization, organization.credentials.BW_EMAIL, Role.user, agentGrant(organization.credentials.BW_EMAIL, organization.collection.id));
    await ensureMember(context, organization, context.setupAccount.OWNER_EMAIL, Role.owner, { emails: [context.setupAccount.OWNER_EMAIL], type: Role.owner, collections: [], groups: [] });
  }
}

async function finalizeSetup(context) {
  const { cluster, setupAccount, setupSession, vault } = context;
  const agents = {};
  for (const identity of identities.organizations) {
    const { name } = identity;
    const organization = await ensureOrg(context, identity, false);
    validateMembers(organization.members(), setupAccount.OWNER_EMAIL, organization.credentials.BW_EMAIL, setupAccount.BW_EMAIL);
    const member = atMostOne(organization.members(), candidate => candidate.email === organization.credentials.BW_EMAIL);
    validateAgent(await vault.api(setupSession, 'GET', `/organizations/${organization.organization.id}/users/${member.id}?includeCollections=true&includeGroups=true`), organization.collection.id);
    const session = await vault.login(organization.credentials, name);
    agents[name] = { ...organization, session };
    const canaryKey = 'canary_' + name;
    agents[name].canaryKey = canaryKey;
    let canaryId = setupAccount[canaryKey];
    if (!canaryId) {
      const canary = { type: ItemType.secureNote, name: 'vault-org-setup-synthetic-canary', organizationId: organization.organization.id, collectionIds: [organization.collection.id], secureNote: { type: SecureNoteType.generic }, notes: 'Synthetic setup isolation check' };
      canaryId = JSON.parse(session.bw(['create', 'item'], Buffer.from(JSON.stringify(canary)).toString('base64'))).id;
      cluster.save('vaultwarden-org-setup', { [canaryKey]: canaryId });
    }
    agents[name].canaryId = canaryId;
  }
  for (const { name } of identities.organizations) {
    const agent = agents[name];
    const other = agents[identities.organizations.find(candidate => candidate.name !== name).name];
    agent.session.bw(['sync']);
    const canary = agent.session.json(['get', 'item', agent.canaryId]);
    validateIsolation(agent.session.json(['list', 'organizations']), [canary], agent.organization.id, agent.canaryId, other.canaryId);
    let denied = false;
    try { agent.session.bw(['get', 'item', other.canaryId]); } catch { denied = true; }
    if (!denied) throw new SetupError('Foreign canary was visible');
    console.log(name + ': isolation check passed');
  }
  for (const { name } of identities.organizations) {
    await vault.api(agents[name].session, 'DELETE', `/ciphers/${agents[name].canaryId}`, undefined, true);
    cluster.save('vaultwarden-org-setup', { [agents[name].canaryKey]: '' });
  }
  const setupUser = await vault.adminUser(setupAccount.BW_EMAIL);
  cluster.save('vaultwarden-org-setup', { stage: Checkpoint.deleting });
  await vault.deleteUser(setupUser.id);
  if (await vault.adminUser(setupAccount.BW_EMAIL)) throw new SetupError('Setup account still exists');
  cluster.save('vaultwarden-org-setup', { stage: Checkpoint.finalized });
  console.log('Both owners confirmed. Setup account deleted. Revert the setup component.');
}

async function run(cluster, scratchDirectory) {
  const setupAccount = cluster.secret('vaultwarden-org-setup');
  if (setupAccount.stage === Checkpoint.finalized) {
    console.log('Organization setup already complete. No changes.');
    return;
  }
  const deployment = cluster.get('deployment', 'vaultwarden');
  const serverEnv = deployment.spec.template.spec.containers.find(container => container.name === 'vaultwarden').env;
  const setting = name => serverEnv.find(entry => entry.name === name);
  if (!setupConfigActive(serverEnv) || setupAccount.BW_EMAIL !== identities.setupEmail) throw new SetupError('Setup component not active');
  const vault = new Vault(setting('DOMAIN').value, scratchDirectory);
  try {
    await vault.start(setupAccount.ADMIN_TOKEN);
    if (setupAccount.stage === Checkpoint.deleting && !(await vault.adminUser(setupAccount.BW_EMAIL))) {
      cluster.save('vaultwarden-org-setup', { stage: Checkpoint.finalized });
      console.log('Setup account already deleted. Revert the setup component.');
      return;
    }
    await vault.register(setupAccount);
    const setupSession = await vault.login(setupAccount, 'setup');
    const context = { cluster, setupAccount, setupSession, vault };
    await setupOrganizations(context);
    await finalizeSetup(context);
  } catch (error) {
    if (!(error instanceof WaitingForOwner)) throw error;
    cluster.save('vaultwarden-org-setup', { stage: Checkpoint.waitingForOwner });
    console.log('Waiting for owner. ' + error.message);
  } finally { await vault.close(); }
}

module.exports = { Role, Status, identities, setupConfigActive, atMostOne, agentEmail, agentGrant, validateAgent, validateMembers, validateIsolation, runCommand, Vault };

if (require.main === module) {
  process.umask(0o077);
  const scratchDirectory = fs.mkdtempSync('/work/vault-org-');
  run(new Cluster(identities.vaultNamespace), scratchDirectory)
    .catch(error => { console.error('Vault organization setup failed. ' + (error instanceof SetupError ? error.message : 'Browser or runtime operation failed at ' + (String(error.stack).match(/setup\.cjs:\d+:\d+/)?.[0] || 'unknown step'))); process.exitCode = 1; })
    .finally(() => fs.rmSync(scratchDirectory, { recursive: true, force: true }));
}

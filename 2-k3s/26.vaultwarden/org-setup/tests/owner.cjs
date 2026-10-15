const fs = require('node:fs');
const { Vault, Role, Status, identities } = require('../setup.cjs');
const EmergencyAccessType = Object.freeze({ view: 0 });

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const scratchDirectory = fs.mkdtempSync('/work/synthetic-logins-');
  const vault = new Vault(input.server, scratchDirectory);
  try {
    if (input.action === 'fresh-agents') {
      const { chromium } = require('playwright');
      vault.browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
      for (const { credentials, name } of input.agents) {
        const session = await vault.login(credentials, name);
        const profile = await vault.api(session, 'GET', '/accounts/profile');
        if (profile.emailVerified !== false) throw new Error('Expected an unverified agent with mail enabled');
        const organizations = session.json(['list', 'organizations']);
        const isolated = organizations.length === 1 && organizations[0].id === credentials.BW_ORGANIZATION_ID;
        if (!isolated) throw new Error('Fresh agent login was not isolated');
        const ordinaryMember = profile.organizations.length === 1 && profile.organizations[0].type === Role.user;
        if (!ordinaryMember) throw new Error('Expected an ordinary agent member');
        const response = await session.context.request.post(vault.server + '/api/emergency-access/invite', {
          headers: { Authorization: 'Bearer ' + session.accessToken },
          data: { email: identities.agentEmail.replace('{name}', 'emergency-target'), type: EmergencyAccessType.view, waitTimeDays: 1 },
        });
        const refused = response.status() === 400 && (await response.json()).message === 'Emergency access is not enabled.';
        if (!refused) throw new Error('Ordinary agent emergency-access invitation was not refused');
      }
    } else {
      await vault.start(input.admin);
      if (input.action === 'register') {
        await vault.register(input.credentials);
      } else if (input.action === 'waiting') {
        if (!(await vault.adminUser(identities.setupEmail))) throw new Error('Setup account deleted while waiting');
      } else if (input.action === 'verify') {
        const owner = await vault.login(input.credentials, 'owner');
        const organizations = owner.json(['list', 'organizations']);
        const expectedCount = organizations.length === identities.organizations.length;
        const expectedIds = input.agents.every(agent => organizations.some(org => org.id === agent.credentials.BW_ORGANIZATION_ID));
        const setupDeleted = !(await vault.adminUser(identities.setupEmail));
        if (!expectedCount || !expectedIds || !setupDeleted) throw new Error('Unexpected final ownership');
        for (const organization of organizations) {
          const members = owner.json(['list', 'org-members', '--organizationid', organization.id]);
          const ownerMember = members.find(member => member.email === input.credentials.BW_EMAIL);
          const confirmedOwner = ownerMember?.type === Role.owner && ownerMember.status === Status.confirmed;
          if (members.length !== 2 || !confirmedOwner) throw new Error('Unexpected final members');
        }
      } else throw new Error('Unknown synthetic account action');
    }
    console.log('Synthetic account check passed');
  } finally {
    await vault.close();
    fs.rmSync(scratchDirectory, { recursive: true, force: true });
  }
}
main().catch(error => { console.error('Synthetic check failed at ' + (String(error.stack).match(/(?:setup|owner)\.cjs:\d+:\d+/)?.[0] || 'unknown step')); process.exitCode = 1; });

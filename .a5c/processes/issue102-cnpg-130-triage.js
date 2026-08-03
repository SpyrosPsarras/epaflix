/**
 * @process specializations/devops-sre-platform/issue102-cnpg-130-triage
 * @description Triage GitHub issue #102 ("Bump CNPG operator past 1.28.x to 1.30") on
 *   SpyrosPsarras/epaflix and decide whether it can be fixed + closed. The 1.30 operator
 *   bump named by #102 is, after #93 closed (CNPG operator brought under ArgoCD, PR #123),
 *   re-scoped + re-tracked as the GitOps-native bump in #128, which itself is sequenced
 *   AFTER the cnpg-operator selfHeal flip (#127 — needs a ~48h soak from the 2026-05-31
 *   adoption). So #102 is the pre-GitOps umbrella and is SUPERSEDED, not actionable as a
 *   bump today. This process: (1) verify the gate/supersession state read-only (issues +
 *   repo manifest), (2) an owner DECISION gate, (3) execute the chosen disposition
 *   (recommended: close #102 as superseded by #128, cross-linking #127/#10/#93), (4) verify.
 *   The actual 1.30 bump is NOT performed here — it is a separate deploy on the DB backing
 *   Authentik + every *arr, gated on #127's soak, and lives in #128.
 * @inputs { repoRoot, repo, issue, supersededBy, prereqFlip, pluginIssue, adoptionIssue, operatorManifest }
 * @outputs { success, decision, closed, issueUrl, summary }
 *
 * @agent general-purpose (gh/git read + gh issue comment/close executor)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// PHASE 1 — assess #102 supersession + bump-gate state (NO mutation).
const assessTask = defineTask('assess', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Assess issue #102 — superseded? bump actionable now?',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer triaging a CloudNativePG operator-bump tracking issue on the Epaflix k3s cluster',
      task:
        'Determine whether issue #' + args.issue + ' can be fixed + closed now. Establish (a) whether ' +
        'the 1.30 bump it tracks has been re-scoped/re-tracked elsewhere (#' + args.supersededBy + '), and ' +
        '(b) whether the bump is actionable today or still gated. DO NOT change anything (read-only).',
      context: { ...args },
      instructions: [
        'Run gh/git locally from repoRoot=' + args.repoRoot + ', repo=' + args.repo + '. NO cluster mutation; NO bump.',
        'Read #' + args.issue + ' and its comments (`gh issue view ' + args.issue + ' --repo ' + args.repo + ' --comments`). Confirm the named gates: #' + args.pluginIssue + ' (Barman plugin migration) and #' + args.adoptionIssue + ' (CNPG operator under ArgoCD) — confirm both CLOSED via `gh issue view <n> --repo ' + args.repo + ' --json state`.',
        'Read #' + args.supersededBy + ' (the GitOps-native 1.30 bump) and #' + args.prereqFlip + ' (cnpg-operator selfHeal flip). Confirm #' + args.supersededBy + ' is the live tracker for the actual bump and that it is sequenced AFTER #' + args.prereqFlip + '.',
        'Confirm repo state: the operator is still pinned at v1.28.0 in ' + args.operatorManifest + ' (`git --no-pager grep -n "cloudnative-pg:" -- ' + args.operatorManifest + '`). Confirm the cnpg-operator ArgoCD App is still MANUAL sync (syncPolicy: {}) in 2-k3s/11.argocd/apps/app-cnpg-operator.yaml — i.e. #' + args.prereqFlip + ' (selfHeal flip) has NOT landed yet.',
        'Assess actionability: the bump is sequenced after the selfHeal flip (#' + args.prereqFlip + '), which needs a ~48h soak from the 2026-05-31 ArgoCD adoption (PR #123). If that flip has not merged, the bump is NOT ready today. Bumping the operator now (same day adoption landed, on the DB backing Authentik SSO + every *arr) would be a high-risk unsequenced deploy.',
        'Recommendation: classify #' + args.issue + ' as one of: "close-superseded" (close as superseded by #' + args.supersededBy + ', the actual bump lives there; gate #' + args.prereqFlip + ' tracks the prerequisite) | "keep-open" (still uniquely useful) | "bump-now" (override the gate and do the 1.30 bump — strongly discouraged in this triage). Give a one-line rationale.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueState', 'pluginIssueClosed', 'adoptionIssueClosed', 'supersededByState', 'prereqFlipState', 'prereqFlipLanded', 'operatorVersionInRepo', 'appSyncIsManual', 'bumpActionableNow', 'recommendation', 'rationale', 'summary'],
      properties: {
        issueState: { type: 'string' },
        pluginIssueClosed: { type: 'boolean' },
        adoptionIssueClosed: { type: 'boolean' },
        supersededByState: { type: 'string' },
        prereqFlipState: { type: 'string' },
        prereqFlipLanded: { type: 'boolean' },
        operatorVersionInRepo: { type: 'string' },
        appSyncIsManual: { type: 'boolean' },
        bumpActionableNow: { type: 'boolean' },
        recommendation: { type: 'string' },
        rationale: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 2a — close #102 as superseded (comment + close).
const closeTask = defineTask('close-superseded', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #' + args.issue + ' as superseded by #' + args.supersededBy,
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer closing an OWNER-APPROVED superseded tracking issue on SpyrosPsarras/epaflix',
      task:
        'The owner approved closing #' + args.issue + ' as superseded. Post a closing comment that records WHY ' +
        '(re-scoped to GitOps-native #' + args.supersededBy + ' after #' + args.adoptionIssue + ' closed; the actual ' +
        'bump is gated on the #' + args.prereqFlip + ' selfHeal flip + soak), cross-link #' + args.supersededBy +
        '/#' + args.prereqFlip + '/#' + args.pluginIssue + '/#' + args.adoptionIssue + ', then close the issue.',
      context: { ...args },
      instructions: [
        'Run gh locally from repoRoot=' + args.repoRoot + ', repo=' + args.repo + '.',
        'Post the closing comment via `gh issue comment ' + args.issue + ' --repo ' + args.repo + ' --body "<comment>"`. The comment MUST state: gates #' + args.pluginIssue + ' + #' + args.adoptionIssue + ' both closed; the 1.30 bump was re-scoped to a reviewable GitOps manifest swap and is now tracked in #' + args.supersededBy + '; #' + args.supersededBy + ' is sequenced AFTER the cnpg-operator selfHeal flip #' + args.prereqFlip + ' (which needs a ~48h soak from the 2026-05-31 PR #123 adoption); operator still v1.28.0 in repo and the App is still manual-sync, so the bump is correctly NOT done yet. Closing #' + args.issue + ' as SUPERSEDED to avoid a duplicate tracker; the live work is #' + args.supersededBy + '.',
        'Then close: `gh issue close ' + args.issue + ' --repo ' + args.repo + ' --reason "not planned" --comment ""` (the standalone comment above carries the rationale; do NOT pass a second body). If --reason is unsupported, fall back to `gh issue close ' + args.issue + ' --repo ' + args.repo + '`.',
        'Capture the issue URL and confirm state == CLOSED via `gh issue view ' + args.issue + ' --repo ' + args.repo + ' --json state,url`.',
        'Do NOT touch any manifest, do NOT perform the operator bump, do NOT open any PR. Follow-ups already exist (#' + args.supersededBy + ', #' + args.prereqFlip + ') so no new issue is needed.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['closed', 'issueUrl', 'commentPosted'],
      properties: { closed: { type: 'boolean' }, issueUrl: { type: 'string' }, commentPosted: { type: 'boolean' } },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: 'SpyrosPsarras/epaflix',
    issue: 102,
    supersededBy: 128,   // GitOps-native 1.30 bump tracker
    prereqFlip: 127,     // cnpg-operator selfHeal flip (must precede the bump)
    pluginIssue: 10,     // Barman Cloud Plugin migration (closed)
    adoptionIssue: 93,   // CNPG operator under ArgoCD (closed)
    operatorManifest: '2-k3s/06.postgres/operator-kustomization/cnpg-operator.yaml',
    ...inputs,
  };

  ctx.log('info', 'issue #102 CNPG 1.30 bump triage — assess → decide → (close-superseded|keep|bump) → verify');

  // PHASE 1 — assess (read-only).
  const a = await ctx.task(assessTask, { ...cfg });
  ctx.log('info', `Assess: #${cfg.issue}=${a.issueState}; super(#${cfg.supersededBy})=${a.supersededByState}; flipLanded=${a.prereqFlipLanded}; bumpActionable=${a.bumpActionableNow}; rec=${a.recommendation}`);

  // GATE — owner decision (closing an issue is outward-facing; the "can't bump now" call is a judgment).
  const gate = await ctx.breakpoint({
    question:
      'Issue #' + cfg.issue + ' (Bump CNPG operator 1.28→1.30) — what should I do?\n\n' +
      'State of #' + cfg.issue + ': ' + a.issueState + '\n' +
      'Named gates: #' + cfg.pluginIssue + ' closed=' + a.pluginIssueClosed + ', #' + cfg.adoptionIssue + ' closed=' + a.adoptionIssueClosed + '\n' +
      'Re-scoped tracker #' + cfg.supersededBy + ': ' + a.supersededByState + ' (the actual GitOps bump lives here)\n' +
      'Prerequisite selfHeal flip #' + cfg.prereqFlip + ': ' + a.prereqFlipState + ' — landed=' + a.prereqFlipLanded + ' (needs ~48h soak from 2026-05-31 PR #123)\n' +
      'Repo operator version: ' + a.operatorVersionInRepo + '; cnpg-operator App manual-sync=' + a.appSyncIsManual + '\n' +
      'Bump actionable TODAY: ' + a.bumpActionableNow + '\n\n' +
      'Recommendation: ' + a.recommendation + ' — ' + a.rationale + '\n\n' +
      'Summary: ' + a.summary + '\n\n' +
      'The actual 1.30 bump is a high-risk deploy (DB backs Authentik + every *arr), sequenced after #' + cfg.prereqFlip + '. This triage will NOT perform the bump. Choose:',
    options: [
      'Close #' + cfg.issue + ' as superseded by #' + cfg.supersededBy,
      'Keep #' + cfg.issue + ' open (do not close)',
      'Stop — I will handle it manually',
    ],
    expert: 'owner',
    tags: ['github', 'decision-gate', 'outward-facing'],
  });

  const resp = (gate.response || '').toLowerCase();

  // No explicit decision / dismissed → safe default: do nothing (leave open).
  if (!gate.approved && !resp) {
    ctx.log('warn', 'No decision provided — leaving #' + cfg.issue + ' open, no mutation.');
    return { success: true, decision: 'keep-open', closed: false, summary: 'No decision; #' + cfg.issue + ' left open. ' + a.summary };
  }

  // KEEP OPEN.
  if (resp.includes('keep') || resp.includes('stop') || resp.includes('manual')) {
    ctx.log('info', 'Decision: leave #' + cfg.issue + ' open — no action.');
    return { success: true, decision: 'keep-open', closed: false, summary: '#' + cfg.issue + ' left open per owner. Live bump tracker remains #' + cfg.supersededBy + '. ' + a.summary };
  }

  // CLOSE AS SUPERSEDED (default for "close").
  const c = await ctx.task(closeTask, { ...cfg });
  ctx.log('info', `Closed #${cfg.issue}: closed=${c.closed}; comment=${c.commentPosted}; url=${c.issueUrl}`);

  if (!c.closed) {
    const recover = await ctx.breakpoint({
      question:
        'Close of #' + cfg.issue + ' did not confirm.\nclosed=' + c.closed + '\ncommentPosted=' + c.commentPosted + '\nurl=' + c.issueUrl + '\n\nHow to proceed?',
      options: ['Retry close', 'Accept state', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('retry')) {
      const c2 = await ctx.task(closeTask, { ...cfg, attempt: 2 });
      return { success: c2.closed, decision: 'close-superseded', closed: c2.closed, issueUrl: c2.issueUrl, summary: 'Retried close; closed=' + c2.closed + '. ' + a.summary };
    }
    if (!recover.approved || r.includes('stop')) {
      return { success: false, decision: 'close-superseded', closed: false, issueUrl: c.issueUrl, reason: 'close-unconfirmed' };
    }
  }

  return {
    success: true,
    decision: 'close-superseded',
    closed: c.closed,
    issueUrl: c.issueUrl,
    summary: 'Closed #' + cfg.issue + ' as superseded by #' + cfg.supersededBy + '. Actual 1.30 bump tracked in #' + cfg.supersededBy + ', gated on #' + cfg.prereqFlip + ' selfHeal flip + soak. ' + a.summary,
  };
}

/**
 * @process specializations/devops-sre-platform/searxng-followups
 * @description Two follow-ups from the merged SearXNG work (PRs #373/#374), each "create the
 *   gh issue, then solve it":
 *   (1) Flip the `searxng` ArgoCD Application from prune:false to prune:true. Pre-flip safety
 *       verify centred on the SOPS-sourced `searxng-secret` (tracked + git-sourced + Synced, same
 *       ksops pattern as filebrowser-oidc) so prune cannot delete it. Deploy breakpoint (shows the
 *       PR body before it is posted), branch+commit, push+PR+merge (app-of-apps selfHeal reconciles
 *       the merged Application spec), post-merge verify, closeout.
 *   (2) Add a `fetch_url` tool to the pi extension at ~/.pi/agent/extensions/searxng-web-search/
 *       (fetch a URL, strip to readable text, truncate). TDD with `node --test`. Not in git, so no
 *       PR/merge — implement, verify the extension still loads, close the issue.
 * @inputs { repoRoot, kctx, repo, appName, appManifest, appPath, ns, secret, branch, extDir,
 *           pruneIssueTitle, pruneIssueBody, fetchIssueTitle, fetchIssueBody }
 * @outputs { success, pruneIssue, fetchIssue, merged, prUrl, livePrune, fetchUrlAdded }
 * @agent general-purpose
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const io = (taskCtx) => ({
  inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
  outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
});

// --- Create both GitHub issues (bodies pre-approved by the owner last turn) ---
const createIssuesTask = defineTask('create-issues', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Create the two follow-up GitHub issues',
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform engineer filing tracked follow-ups in SpyrosPsarras/epaflix',
      task: 'Create two GitHub issues with the exact titles/bodies provided in context, then return their numbers and URLs.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Create issue 1: `gh issue create --repo ' + args.repo + ' --title "<pruneIssueTitle>" --body "<pruneIssueBody>"` using the exact pruneIssueTitle/pruneIssueBody from context (write the body to a temp file and use --body-file to preserve markdown).',
        'Create issue 2 the same way from fetchIssueTitle/fetchIssueBody.',
        'Do NOT invent or alter the bodies. Return ONLY structured JSON with the two issue numbers and URLs.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['pruneIssueNumber', 'pruneIssueUrl', 'fetchIssueNumber', 'fetchIssueUrl'],
      properties: {
        pruneIssueNumber: { type: 'number' },
        pruneIssueUrl: { type: 'string' },
        fetchIssueNumber: { type: 'number' },
        fetchIssueUrl: { type: 'string' },
      },
    },
  },
  io: io(taskCtx),
}));

// --- FU1 Phase 1: pre-flip safety verify (no mutation) ---
const preflightVerifyTask = defineTask('preflight-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify prune:true is safe for the searxng app (SOPS secret git-sourced, no tracked orphans)',
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE on the Epaflix k3s cluster',
      task: 'Prove flipping the `' + args.appName + '` ArgoCD Application to prune:true is SAFE — no resource ArgoCD tracks for this app is missing from git (prune would delete it), with attention to the SOPS Secret `' + args.secret + '`. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl is available directly as `' + args.kctx + '` (epaflix context). Run git locally from repoRoot=' + args.repoRoot + '.',
        '`' + args.kctx + ' -n argocd get application ' + args.appName + ' -o json` — record sync.status (expect Synced), health.status (expect Healthy), spec.syncPolicy.automated (expect selfHeal:true, prune:false).',
        'Build the tracked resource list from .status.resources[]; every entry should be Synced.',
        'Local render is NOT possible (no age key / ksops exec on this host) — rely on ArgoCD diff: a fully-Synced app means desired(git)==live, so nothing would be pruned.',
        'PRUNE-SAFETY: identify any tracked resource that exists live but is no longer in git (would-be-pruned). For a Synced app this must be empty.',
        'SECRET CHECK: confirm Secret/' + args.secret + ' in ns ' + args.ns + ' is tracked (in .status.resources[]) AND Synced (rendered from git via ' + args.appPath + '/ksops-generator.yaml + searxng-secret.enc.yaml). Confirm those files exist in git and kustomization.yaml lists the generator.',
        'Set safeToFlip=true ONLY if app Synced+Healthy AND would-be-pruned is empty AND ' + args.secret + ' is tracked+git-sourced+Synced. Report any untracked orphans (prune ignores them).',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['safeToFlip', 'appSync', 'appHealth', 'currentPrune', 'trackedCount', 'secretTracked', 'secretGitSourced', 'wouldBePruned', 'untrackedOrphans', 'summary'],
      properties: {
        safeToFlip: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        currentPrune: { type: 'boolean' },
        trackedCount: { type: 'number' },
        secretTracked: { type: 'boolean' },
        secretGitSourced: { type: 'boolean' },
        wouldBePruned: { type: 'array', items: { type: 'string' } },
        untrackedOrphans: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: io(taskCtx),
}));

// --- FU1 Phase 2: author the flip on a branch + local commit (no push) ---
const prepareChangeTask = defineTask('prepare-change', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Flip prune:true in app-searxng.yaml + local commit on a branch',
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer editing ArgoCD Application manifests in the Epaflix repo',
      task: 'Flip syncPolicy.automated.prune false->true in ' + args.appManifest + ', update the header comment, create a branch and ONE local commit. Do NOT push, do NOT open a PR.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. CLAUDE.md rules apply; this is a one-line policy change.',
        'git fetch origin; create branch ' + args.branch + ' off origin/main.',
        'Edit ' + args.appManifest + ': change `prune: false` to `prune: true` under spec.syncPolicy.automated. Update the `# Sync:` header comment to say prune is enabled now (Spyros opted to flip immediately rather than wait for the soak), referencing issue #' + args.pruneIssueNumber + '. Keep selfHeal:true and ignoreDifferences untouched.',
        'Stage ONLY ' + args.appManifest + '. ONE commit referencing #' + args.pruneIssueNumber + '. End the message with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'Compose the PR title and body. The body MUST include a Test Plan checklist for the post-merge verify (app-of-apps reconciles prune:true live, app Synced+Healthy, searxng-secret survives, no unexpected pruning) and a "Closes #' + args.pruneIssueNumber + '" line.',
        'Return ONLY structured JSON: branch, commitSha, the exact prune diff (before/after lines), prTitle, prBody.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'diff', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
      },
    },
  },
  io: io(taskCtx),
}));

// --- FU1 Phase 3: push + PR + merge (the deploy) ---
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, merge per policy (triggers app-of-apps reconcile)',
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task: 'Push the branch, open a PR with the approved title/body, wait for CI `validate` to pass, then merge per Epaflix policy (merge commit). Merging is the deploy — app-of-apps selfHeal reconciles the new spec.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Push branch ' + args.branch + ' to origin.',
        'Open a PR to main with approvedPrTitle/approvedPrBody (in context). The branch was cut from origin/main so it is up to date (no rebase).',
        'Wait for the `validate` check to pass: `gh pr checks <n> --repo ' + args.repo + ' --watch --interval 5`.',
        'Merge with `gh pr merge <n> --repo ' + args.repo + ' --merge` (merge commit; admin bypass authorized). Confirm state MERGED before returning.',
        'Return ONLY structured JSON: prUrl, prNumber, merged, mergeSha.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'prNumber', 'merged', 'mergeSha'],
      properties: {
        prUrl: { type: 'string' },
        prNumber: { type: 'number' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
      },
    },
  },
  io: io(taskCtx),
}));

// --- FU1 Phase 4: post-merge verify ---
const postMergeVerifyTask = defineTask('post-merge-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify app-of-apps reconciled prune:true live + secret survives + nothing pruned',
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying a post-merge GitOps reconcile',
      task: 'Confirm the merged prune:true reached the live `' + args.appName + '` Application via app-of-apps, the app is still Synced+Healthy, the SOPS secret survived, and nothing was unexpectedly pruned.',
      context: { ...args },
      instructions: [
        'kubectl is `' + args.kctx + '`. Allow app-of-apps to reconcile; you may trigger a refresh by annotating `' + args.kctx + ' -n argocd annotate application app-of-apps argocd.argoproj.io/refresh=hard --overwrite` and poll. Do NOT force-sync with prune flags manually.',
        'Confirm live spec: `' + args.kctx + ' -n argocd get application ' + args.appName + ' -o jsonpath="{.spec.syncPolicy.automated.prune}"` returns true.',
        'Confirm sync.status==Synced, health.status==Healthy, and the searxng Deployment is Available / pod Ready (`' + args.kctx + ' -n ' + args.ns + ' get deploy,pods`).',
        'SECRET SURVIVAL: confirm Secret/' + args.secret + ' in ' + args.ns + ' still exists (`' + args.kctx + ' -n ' + args.ns + ' get secret ' + args.secret + '`).',
        'Confirm tracked resource count unchanged vs trackedCount in context (nothing pruned).',
        'Set verified=true ONLY if livePrune==true AND Synced+Healthy AND secret survives AND nothing tracked was pruned. Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'livePrune', 'appSync', 'appHealth', 'secretSurvives', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        livePrune: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        secretSurvives: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: io(taskCtx),
}));

// --- FU1 Phase 5: closeout (close prune issue, tick PR test plan) ---
const closeoutPruneTask = defineTask('closeout-prune', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close the prune-flip issue with outcome + tick the PR test plan',
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issue/PR after a verified GitOps change',
      task: 'Comment the verified outcome on issue #' + args.pruneIssueNumber + ' and CLOSE it (if the PR "Closes" line did not already), and tick the PR test-plan checkboxes by EDITING the PR body (never a new comment).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo ' + args.repo + '.',
        'If issue #' + args.pruneIssueNumber + ' is still open, comment the verified result (prune:true live via app-of-apps, Synced+Healthy, secret survived, nothing pruned) and close it.',
        'Edit PR #' + args.prNumber + ' body (gh pr edit --body) to tick the Test Plan items that passed, recording evidence inline. Do NOT add a separate comment.',
        'Return ONLY structured JSON: issueState, prUpdated.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueState', 'prUpdated'],
      properties: { issueState: { type: 'string' }, prUpdated: { type: 'boolean' } },
    },
  },
  io: io(taskCtx),
}));

// --- FU2: implement fetch_url tool in the pi extension (TDD), then close the issue ---
const implementFetchUrlTask = defineTask('implement-fetch-url', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Add fetch_url tool to the pi searxng extension (TDD) and close its issue',
  agent: {
    name: AGENT,
    prompt: {
      role: 'TypeScript engineer extending a pi coding-agent extension',
      task: 'Add a `fetch_url` tool to the pi extension at ' + args.extDir + ' that fetches a URL and returns readable text (HTML stripped, truncated). Follow TDD with `node --test`. This directory is NOT a git repo — do not git anything.',
      context: { ...args },
      instructions: [
        'Read the existing files first: ' + args.extDir + '/index.ts (registers web_search, imports from ./format.mjs, sets dns ipv4first) and ' + args.extDir + '/format.mjs (pure helpers).',
        'TDD: add a new pure module ' + args.extDir + '/fetch.mjs exporting `htmlToText(html)` (strip <script>/<style>, strip tags, decode basic entities, collapse whitespace) and `truncate(text, max)` (default max ~4000 chars, append a "[truncated]" marker when cut). FIRST write failing tests in ' + args.extDir + '/test/fetch.test.mjs (node --test): htmlToText removes tags/script, truncate caps length and marks truncation. Run `node --test ' + args.extDir + '/test/fetch.test.mjs` and confirm it FAILS (module missing), then implement fetch.mjs and confirm it PASSES.',
        'In index.ts register a second tool `fetch_url` via pi.registerTool: params { url: string, maxChars?: number }. execute: fetch(url) with the abort signal and a 20s guard, read text(), pass through htmlToText then truncate, return { content: [{type:"text", text}], details: {} }. On non-200 or fetch error return a clear error text (no silent failure), mirroring web_search. Add a promptSnippet and a promptGuideline naming fetch_url explicitly ("Use fetch_url to read the full text of a page returned by web_search").',
        'Keep imports consistent with index.ts style (import from "./fetch.mjs"). Do not break web_search.',
        'Verify the extension still loads: `timeout 60 pi -e ' + args.extDir + '/index.ts -p "list your available tools" 2>&1 | grep -i fetch_url` should show fetch_url with no TypeScript/import error.',
        'Then close the GitHub issue: comment on issue #' + args.fetchIssueNumber + ' in ' + args.repo + ' that fetch_url was implemented locally (path, tests passing, pi loads it) and CLOSE it. (The extension is not in git, so no PR.)',
        'Return ONLY structured JSON: filesCreated, filesModified, testCommand, testsPassed, piLoadsFetchUrl, issueState.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['filesCreated', 'filesModified', 'testsPassed', 'piLoadsFetchUrl', 'issueState'],
      properties: {
        filesCreated: { type: 'array', items: { type: 'string' } },
        filesModified: { type: 'array', items: { type: 'string' } },
        testCommand: { type: 'string' },
        testsPassed: { type: 'boolean' },
        piLoadsFetchUrl: { type: 'boolean' },
        issueState: { type: 'string' },
      },
    },
  },
  io: io(taskCtx),
}));

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-proxmox/epaflix',
    kctx: 'kubectl --context epaflix',
    repo: 'SpyrosPsarras/epaflix',
    appName: 'searxng',
    appManifest: '2-k3s/11.argocd/apps/app-searxng.yaml',
    appPath: '2-k3s/14.searxng',
    ns: 'searxng',
    secret: 'searxng-secret',
    branch: 'searxng-prune-flip',
    extDir: '/home/spy/.pi/agent/extensions/searxng-web-search',
    ...inputs,
  };

  ctx.log('info', 'searxng follow-ups: create 2 issues -> FU1 prune flip (verify -> deploy gate -> merge -> verify -> closeout) -> FU2 fetch_url tool (TDD)');

  // Create both tracked issues (bodies pre-approved by owner).
  const issues = await ctx.task(createIssuesTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo,
    pruneIssueTitle: cfg.pruneIssueTitle, pruneIssueBody: cfg.pruneIssueBody,
    fetchIssueTitle: cfg.fetchIssueTitle, fetchIssueBody: cfg.fetchIssueBody,
  });
  ctx.log('info', `Issues: prune #${issues.pruneIssueNumber}, fetch #${issues.fetchIssueNumber}`);

  // ===== FOLLOW-UP 1: prune flip =====
  const verify = await ctx.task(preflightVerifyTask, {
    repoRoot: cfg.repoRoot, kctx: cfg.kctx, appName: cfg.appName, appPath: cfg.appPath, ns: cfg.ns, secret: cfg.secret,
  });
  ctx.log('info', `Pre-flip: safe=${verify.safeToFlip}; ${verify.appSync}/${verify.appHealth}; secretTracked=${verify.secretTracked}; wouldBePruned=${(verify.wouldBePruned || []).length}`);

  // Author the change locally so the deploy gate can show the real PR body before posting.
  const change = await ctx.task(prepareChangeTask, {
    repoRoot: cfg.repoRoot, appManifest: cfg.appManifest, branch: cfg.branch, pruneIssueNumber: issues.pruneIssueNumber,
  });

  // GATE (deploy) — show diff + PR body, approve push+merge.
  const gate = await ctx.breakpoint({
    question:
      'Approve flipping `searxng` ArgoCD app to prune:true and MERGING it (#' + issues.pruneIssueNumber + ')?\n\n' +
      'Pre-flip safety: ' + (verify.safeToFlip ? 'SAFE' : 'NOT SAFE') + ' — ' + verify.appSync + '/' + verify.appHealth + ', secret ' + cfg.secret + ' tracked=' + verify.secretTracked + '/git-sourced=' + verify.secretGitSourced + ', would-be-pruned=' + JSON.stringify(verify.wouldBePruned) + '.\n\n' +
      'Diff:\n' + change.diff + '\n\n' +
      'PR body to be posted:\n' + change.prBody + '\n\n' +
      'Merging triggers app-of-apps selfHeal to apply prune:true live. Proceed?',
    options: ['Approve flip + merge', 'Abort follow-up 1'],
    expert: 'owner',
    tags: ['deploy', 'approval-gate'],
  });

  let pub = { merged: false, prUrl: '', prNumber: 0 };
  let post = { verified: false, livePrune: false };
  if (gate.approved) {
    pub = await ctx.task(publishMergeTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, branch: change.branch,
      approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
    });
    ctx.log('info', `Merged: ${pub.merged}; PR ${pub.prUrl}`);
    post = await ctx.task(postMergeVerifyTask, {
      kctx: cfg.kctx, appName: cfg.appName, ns: cfg.ns, secret: cfg.secret, trackedCount: verify.trackedCount,
    });
    if (pub.merged && post.verified) {
      await ctx.task(closeoutPruneTask, {
        repoRoot: cfg.repoRoot, repo: cfg.repo, pruneIssueNumber: issues.pruneIssueNumber, prNumber: pub.prNumber,
      });
    } else {
      ctx.log('warn', `Post-merge verify not clean: livePrune=${post.livePrune} verified=${post.verified}`);
    }
  } else {
    ctx.log('warn', 'Prune flip aborted by owner; leaving issue open and proceeding to follow-up 2.');
  }

  // ===== FOLLOW-UP 2: fetch_url tool (local, no deploy) =====
  const fetchUrl = await ctx.task(implementFetchUrlTask, {
    extDir: cfg.extDir, repo: cfg.repo, fetchIssueNumber: issues.fetchIssueNumber,
  });
  ctx.log('info', `fetch_url: testsPassed=${fetchUrl.testsPassed} piLoads=${fetchUrl.piLoadsFetchUrl} issue=${fetchUrl.issueState}`);

  return {
    success: true,
    pruneIssue: issues.pruneIssueNumber,
    fetchIssue: issues.fetchIssueNumber,
    pruneFlipApproved: gate.approved,
    merged: pub.merged,
    prUrl: pub.prUrl,
    livePrune: post.livePrune,
    fetchUrlAdded: fetchUrl.testsPassed && fetchUrl.piLoadsFetchUrl,
  };
}

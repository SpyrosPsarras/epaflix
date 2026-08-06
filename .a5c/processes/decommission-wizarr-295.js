#!/usr/bin/env node
/**
 * @process project/decommission-wizarr-295
 * @description Decommission wizarr completely after the owner decision on #295:
 * remove it from GitOps, merge through the semi-linear policy, then delete the
 * live Kubernetes objects, its PVC and the orphaned Authentik forward_domain
 * provider, verifying each step against live state.
 *
 * Composition references:
 * - specializations/devops-sre-platform/iac-implementation.js
 * - project/deliver-open-issues-oldest-first.js
 * - methodologies/superpowers/verification-before-completion.js
 *
 * @skill gitops specializations/devops-sre-platform/skills/gitops/SKILL.md
 * @agent platform-engineer specializations/devops-sre-platform/agents/platform-engineer/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    issue = 295,
    branch = 'issue-295-decommission-wizarr',
    ownerDecision = 'Decommission wizarr completely. It was never used and is not needed.',
  } = inputs || {};

  const discovery = await ctx.task(discoverWizarrTask, { repo, repoRoot, issue });

  const prepared = await ctx.task(prepareRemovalTask, {
    repo, repoRoot, issue, branch, ownerDecision,
    discovery: discovery.stdout,
  });

  const approval = await ctx.breakpoint({
    title: `Destructive + deploy gate - decommission wizarr (#${issue})`,
    question:
      'The GitOps removal is committed on a branch but nothing is published or deleted yet. ' +
      'Approve opening and merging this PR, then performing the live deletions listed below?\n\n' +
      `PROPOSED PR TITLE:\n${prepared.prTitle}\n\nPROPOSED PR BODY:\n${prepared.prBody}\n\n` +
      `PROPOSED CLOSING COMMENT FOR #${issue}:\n${prepared.issueComment}\n\n` +
      `LIVE DELETIONS AFTER MERGE:\n- ${(prepared.liveDeletions || []).join('\n- ')}\n`,
    options: ['Approve PR, merge and live deletion', 'Hold - do not publish or delete', 'Stop'],
    context: {
      runId: ctx.runId,
      issue,
      branch,
      filesChanged: prepared.filesChanged,
      liveDeletions: prepared.liveDeletions,
      irreversible: 'Deletes the wizarr PVC and the Authentik provider; the PVC contents are not recoverable afterwards.',
    },
    expert: 'owner',
    tags: ['approval-gate', 'deploy', 'destructive', 'destructive-git', 'external-text'],
  });

  if (!approval.approved) {
    return {
      success: false,
      stopped: true,
      reason: approval.response || approval.feedback || 'Not approved',
      branch,
      prepared,
    };
  }

  const executed = await ctx.task(executeDecommissionTask, {
    repo, repoRoot, issue, branch,
    prTitle: prepared.prTitle,
    prBodyPath: prepared.prBodyPath,
    issueCommentPath: prepared.issueCommentPath,
    liveDeletions: prepared.liveDeletions,
    approvalResponse: approval.response || 'Approved',
  });

  const verified = await ctx.task(verifyDecommissionTask, { repo, repoRoot, issue });

  return {
    success: executed.merged && executed.issueClosed && verified.exitCode === 0,
    issue,
    prUrl: executed.prUrl,
    mergeCommit: executed.mergeCommit,
    deleted: executed.deleted,
    followUps: executed.followUps,
    verified,
    metadata: { processId: 'project/decommission-wizarr-295', timestamp: ctx.now() },
  };
}

export const discoverWizarrTask = defineTask('discover-wizarr-footprint', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Map every wizarr footprint in git, the cluster, DNS and Authentik',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `echo '=== issue ==='`,
      `gh issue view ${args.issue} --repo ${args.repo} --json number,title,state,body --jq '"#\\(.number) \\(.state) \\(.title)"'`,
      `echo`,
      `echo '=== git files that mention wizarr ==='`,
      `git grep -rln -i wizarr -- . | grep -v '^\\.a5c/' || echo none`,
      `echo`,
      `echo '=== git: wizarr directory contents ==='`,
      `ls -1 2-k3s/08.servarr/wizarr/ 2>/dev/null || echo 'no directory'`,
      `grep -n -i wizarr 2-k3s/08.servarr/kustomization.yaml || echo 'not in kustomization'`,
      `echo`,
      `echo '=== live workload state ==='`,
      `kubectl -n servarr get deploy wizarr -o jsonpath='replicas={.spec.replicas} ready={.status.readyReplicas}{"\\n"}' 2>/dev/null || echo 'no deployment'`,
      `kubectl -n servarr get pods -l app=wizarr --no-headers 2>/dev/null | wc -l | sed 's/^/pods: /'`,
      `kubectl -n servarr get pvc wizarr-config -o jsonpath='pvc={.metadata.name} phase={.status.phase} size={.status.capacity.storage} node={.metadata.annotations.volume\\.kubernetes\\.io/selected-node}{"\\n"}' 2>/dev/null || echo 'no pvc'`,
      `kubectl -n servarr get ingressroute -o name 2>/dev/null | grep -i wizarr || echo 'no ingressroutes'`,
      `kubectl -n servarr get svc wizarr -o jsonpath='svc={.metadata.name} port={.spec.ports[0].port}{"\\n"}' 2>/dev/null || echo 'no service'`,
      `echo`,
      `echo '=== is the endpoint reachable from outside ==='`,
      `printf 'https://wizarr.epaflix.com -> HTTP %s\\n' "$(curl -ks -o /dev/null -w '%{http_code}' --max-time 10 https://wizarr.epaflix.com/ || echo unreachable)"`,
      `echo`,
      `echo '=== authentik objects mentioning wizarr (names and pks only) ==='`,
      `AK_TOKEN=$(sops -d --extract '["authentik_iac_service_account_token"]' .github/instructions/secrets.enc.yaml)`,
      `echo "token length: \${#AK_TOKEN}"`,
      `curl -fsS --max-time 30 -H "Authorization: Bearer $AK_TOKEN" 'https://auth.epaflix.com/api/v3/providers/all/?page_size=200' | jq -r '[.results[] | select((.name|ascii_downcase|test("wizarr")) or ((.component // "")|test("forward_domain")))] | .[] | "provider pk=\\(.pk) name=\\(.name) component=\\(.component // "") assigned=\\(.assigned_application_name // "none")"' || echo 'provider query failed'`,
      `curl -fsS --max-time 30 -H "Authorization: Bearer $AK_TOKEN" 'https://auth.epaflix.com/api/v3/core/applications/?page_size=200' | jq -r '[.results[] | select(.name|ascii_downcase|test("wizarr"))] | .[] | "application slug=\\(.slug) name=\\(.name) provider=\\(.provider // "none")"' || echo 'no wizarr application'`,
      `curl -fsS --max-time 30 -H "Authorization: Bearer $AK_TOKEN" 'https://auth.epaflix.com/api/v3/outposts/instances/?page_size=50' | jq -r '.results[] | "outpost \\(.name) providers=\\(.providers|tostring)"' || echo 'outpost query failed'`,
      `unset AK_TOKEN`,
      `echo`,
      `echo '=== DNS ==='`,
      `dig +short wizarr.epaflix.com @192.168.10.30 || true`,
      `ssh -o ConnectTimeout=10 root@192.168.10.11 "pct exec 1030 -- grep -rn wizarr /etc/dnsmasq.d/ 2>/dev/null" || echo 'no explicit pihole record (wildcard covers it)'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'discovery', 'wizarr', 'issue-295'],
}));

export const prepareRemovalTask = defineTask('prepare-wizarr-removal', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Commit the GitOps removal on a branch and draft the PR and closing comment',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'GitOps engineer removing a decommissioned app from a Kustomize + ArgoCD repo',
      task: 'Remove wizarr from git on a branch, commit it, and draft the PR body and issue-closing comment. Do not push, do not open a PR, do not delete anything live.',
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}. It is a dedicated git worktree; another agent uses the main checkout, so never touch that.`,
        `Read CLAUDE.md first for the merge policy, secret rules and follow-up-issue rule.`,
        'Treat the DISCOVERY block in context as source of truth for what exists.',
        `git fetch origin, then git checkout -B ${args.branch} origin/main.`,
        'Delete the 2-k3s/08.servarr/wizarr/ directory and remove its entry from 2-k3s/08.servarr/kustomization.yaml.',
        'Update the docs that describe wizarr as a live app so they do not claim something untrue: annotate rather than silently delete history where a doc is a record of past work.',
        'Do not touch unrelated apps. Do not edit any .enc.yaml or any secret.',
        'Verify the change renders: run kustomize build on 2-k3s/08.servarr if the tooling is available, otherwise parse every changed YAML with python3 and yaml.safe_load_all. Report which check you ran.',
        'Commit with a conventional-commit subject that has no issue number in it. Body explains the owner decision that wizarr was never used.',
        'Draft the PR body: what is removed, why, the live deletions that follow the merge, and a ## Test plan whose pre-merge boxes you have actually executed and ticked. Leave post-merge boxes unticked.',
        `Draft the closing comment for issue #${args.issue}: the owner decision, what git no longer contains, what was deleted live, and any remaining follow-up.`,
        'Write the PR body to /tmp/wizarr-pr-body.md and the closing comment to /tmp/wizarr-issue-comment.md, and return both paths plus their text.',
        'Plain simple English, no emoji, no invented values.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prTitle', 'prBody', 'prBodyPath', 'issueComment', 'issueCommentPath', 'filesChanged', 'liveDeletions', 'summary'],
      properties: {
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
        prBodyPath: { type: 'string' },
        issueComment: { type: 'string' },
        issueCommentPath: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        liveDeletions: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'gitops', 'wizarr', 'issue-295'],
}));

export const executeDecommissionTask = defineTask('execute-wizarr-decommission', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push, merge, delete the live footprint, and close the issue',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Epaflix release manager and Kubernetes operator',
      task: 'Publish and merge the approved removal, then delete the approved live objects and close the issue with the approved comment.',
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}.`,
        `Push ${args.branch}, then open the PR with the approved title and --body-file ${args.prBodyPath}. Do not rewrite the approved text.`,
        'Rebase onto origin/main and push with --force-with-lease, wait for the required validate check to pass, confirm the branch is up to date, then merge with gh pr merge --merge.',
        'After merge, delete the live objects listed in liveDeletions, in this order: IngressRoutes, Service, Deployment, then the PVC last so nothing is left writing to it.',
        'Then delete the Authentik provider and application named in liveDeletions. Read the token with sops -d --extract of authentik_iac_service_account_token and never print it; print only its length if you need a check.',
        'Verify after each group: the objects are gone, the servarr namespace is otherwise healthy, and no other app lost its route. Confirm at least two unrelated servarr hosts still respond as before.',
        'Execute every post-merge box of the PR test plan and record the outcome by editing the PR description. Never add a PR comment.',
        `Close issue #${args.issue} with reason completed using --body-file ${args.issueCommentPath}, appending only factual results you verified.`,
        'Open a follow-up gh issue for anything deferred, using the Finding / Current state / Desired outcome / Notes shape and cross-linking this issue.',
        'Log the significant commands and their outputs to .history/ in the main repo path, with no secret values.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeCommit', 'deleted', 'issueClosed', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        deleted: { type: 'array', items: { type: 'string' } },
        issueClosed: { type: 'boolean' },
        followUps: { type: 'array', items: { type: 'number' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'deploy', 'destructive', 'wizarr', 'issue-295'],
}));

export const verifyDecommissionTask = defineTask('verify-wizarr-decommission', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify wizarr is gone from git, the cluster and Authentik',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `git fetch origin --quiet`,
      `test "$(gh issue view ${args.issue} --repo ${args.repo} --json state --jq .state)" = CLOSED`,
      `! git ls-tree -r --name-only origin/main | grep -q '^2-k3s/08.servarr/wizarr/'`,
      `! git show origin/main:2-k3s/08.servarr/kustomization.yaml | grep -qi wizarr`,
      `! kubectl -n servarr get deploy wizarr >/dev/null 2>&1`,
      `! kubectl -n servarr get svc wizarr >/dev/null 2>&1`,
      `! kubectl -n servarr get pvc wizarr-config >/dev/null 2>&1`,
      `! kubectl -n servarr get ingressroute -o name 2>/dev/null | grep -qi wizarr`,
      `for h in sonarr radarr; do code=$(curl -ks -o /dev/null -w '%{http_code}' --max-time 15 "https://$h.epaflix.com/"); echo "$h -> $code"; test "$code" != 000; done`,
      `AK_TOKEN=$(sops -d --extract '["authentik_iac_service_account_token"]' .github/instructions/secrets.enc.yaml)`,
      `test -z "$(curl -fsS --max-time 30 -H "Authorization: Bearer $AK_TOKEN" 'https://auth.epaflix.com/api/v3/providers/all/?page_size=200' | jq -r '[.results[] | select(.name|ascii_downcase|test("wizarr"))] | .[].pk' )"`,
      `unset AK_TOKEN`,
      `echo 'wizarr fully decommissioned: git, cluster and Authentik all clean; neighbours still serving'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification', 'wizarr', 'issue-295'],
}));

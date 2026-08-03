/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Resolve Epaflix issue #164: the required, strict `validate` CI gate flakes because the
 *   "Install tools" step installs kustomize by piping the upstream install_kustomize.sh from `master`
 *   UNPINNED + UNAUTHENTICATED. That script resolves the latest release tag via an unauthenticated
 *   GitHub API call; when that call is rate-limited the tarball never downloads and the next `tar` line
 *   fails ("kustomize_v*_linux_amd64.tar.gz: Cannot open"), hard-blocking every PR + Renovate auto-merge
 *   until someone manually `gh run rerun --failed`. The helm install one line above (`get-helm-3` from
 *   `main`) carries the same curl|bash-from-a-moving-branch risk.
 *
 *   GOAL: make the tool install in `.github/workflows/ci.yml` DETERMINISTIC and flake-resistant by
 *   pinning explicit helm + kustomize versions and downloading the official release assets DIRECTLY
 *   (no latest-tag GitHub API lookup, no curl|bash of a script from a moving branch), wrapped in a
 *   bounded retry so a transient network blip self-heals instead of hard-failing. Keep the pins fresh
 *   (repo ethos = Renovate-manages-everything) by adding a Renovate custom manager so the pinned
 *   versions get bumped automatically. This is a CI-hardening change ONLY — it must NOT alter what the
 *   three downstream checks (YAML parse, Helm-pin resolve, kustomize build of sops-free dirs) actually
 *   validate; the same kustomize/helm behaviour must be reproduced with pinned binaries.
 *
 *   It is a pure GIT/CI change — NO live cluster operation, NO SSH, NO ArgoCD sync, NO secrets. The only
 *   gated action is the merge to main (the `validate` workflow is a REQUIRED, STRICT check, so a bad
 *   edit would break CI for EVERY future PR) — so there is exactly ONE owner gate, right before push+
 *   merge, showing the diff + the LOCAL verification that the pinned binaries reproduce the existing
 *   checks. The PR's OWN `validate` run (executing the new workflow) is the live proof the fix works;
 *   we wait for it green before merging.
 *
 *   Flow: plan (read-only: analyse ci.yml, pick pinned versions by probing the official release-asset
 *   URLs, decide the retry shape + Renovate custom manager, produce the exact edit plan) → implement
 *   (branch off origin/main, edit ci.yml + renovate.json [+ workflows/README.md if it documents the
 *   install], local commit, NO push) → local-verify LOOP (download the SAME pinned binaries to a temp
 *   dir and re-run the exact `kustomize build` over the sops-free dirs + a `helm pull` resolve spot-check
 *   to prove the pins reproduce the checks; refine on failure) → ONE owner gate (merge) showing diff +
 *   verification → publish+merge (rebase onto origin/main, force-with-lease, push, open PR, WAIT for the
 *   new `validate` to pass on the PR, merge --merge per Epaflix policy) → post-verify (PR merged, validate
 *   green on main) → closeout (#164 close + tick PR test plan by editing the body + open follow-ups:
 *   helm-4 major review, Renovate-pin cadence).
 *
 * @inputs { repoRoot, ciWorkflow, renovateJson, workflowsReadme, issue, repo, branch, relatedIssues }
 * @outputs { success, decision, merged, prUrl, validateGreen, issueState, followUpIssues }
 *
 * @agent general-purpose CI/GitHub-Actions + helm/kustomize + git + gh executor; classification & verification
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Phase 1 — plan (READ-ONLY): analyse ci.yml, pick pinned versions, design the
// deterministic install + retry + Renovate custom manager. No edits.
// ---------------------------------------------------------------------------
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyse ci.yml Install-tools flake; pick pinned helm+kustomize versions (probe official asset URLs); design deterministic install + retry + Renovate manager',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'CI/CD + GitHub Actions engineer hardening the Epaflix secret-free `validate` gate (issue #' + args.issue + ')',
      task:
        'Produce a concrete, verified plan to make the "Install tools" step in ' + args.ciWorkflow + ' deterministic and ' +
        'flake-resistant. Read the workflow, identify the exact failure (install_kustomize.sh resolves the latest tag via an ' +
        'unauthenticated GitHub API call that gets rate-limited), and decide PINNED versions + DIRECT official release-asset ' +
        'download (no API tag lookup, no curl|bash from a moving branch) + a bounded retry. Decide a Renovate custom manager so ' +
        'the pins stay fresh. Do NOT edit anything — output the plan + verified facts.',
      context: { ...args },
      instructions: [
        'Run read-only from repoRoot=' + args.repoRoot + '. Read ' + args.ciWorkflow + ' fully (esp. the "Install tools" step and the three checks it feeds: YAML parse, "Helm chart pins resolve" via `helm pull`, "Kustomize build (sops-free dirs)"). Also read ' + args.renovateJson + ' and, if present, ' + args.workflowsReadme + '.',
        'KUSTOMIZE pin: choose the latest stable kustomize release and VERIFY the exact direct asset URL exists by probing it (curl -fsI, no body): `https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv<VER>/kustomize_v<VER>_linux_amd64.tar.gz`. Record the resolved <VER> and the HTTP status. This URL hits the release-asset CDN, NOT the rate-limited tag API.',
        'HELM pin: the CI only needs `helm pull` to resolve chart pins. helm v4 is now the latest MAJOR — for a determinism fix prefer pinning the CURRENT latest helm 3.x patch (no `helm pull` behaviour change / major-version risk) unless you can confirm v4 keeps identical `helm pull --repo/--version` + OCI semantics. VERIFY the chosen version downloads directly from the official dist (no GitHub API): `curl -fsI https://get.helm.sh/helm-v<VER>-linux-amd64.tar.gz`. Record <VER>, the URL, and the matching checksum URL (`.sha256sum`) if you will verify it.',
        'RETRY: design a small POSIX-sh bounded retry helper (e.g. 3 attempts, sleep 5/10s backoff) wrapping each curl download so a transient blip self-heals. Keep `set -euo pipefail`. After download, extract + `sudo mv` the binary to /usr/local/bin and keep the existing `helm version` / `kustomize version` echo so the step still self-tests.',
        'RENOVATE: design a `customManagers` (regex) entry in ' + args.renovateJson + ' that tracks the two pinned versions in ci.yml (datasource `github-releases` for kustomize package `kubernetes-sigs/kustomize`, and the appropriate datasource for helm — `github-releases` package `helm/helm`). Use clear `# renovate: datasource=... depName=...` marker comments in ci.yml so the regex is robust. Confirm this composes with the existing packageRules (does not collide with the kustomize/github-actions managers).',
        'NO-API CLAIM: verify the new install path makes ZERO unauthenticated GitHub API calls (direct asset URLs only) so GITHUB_TOKEN is not even required; note this explicitly. If you instead keep any API call, require passing GITHUB_TOKEN.',
        'Produce the EXACT intended new "Install tools" step text and the EXACT renovate.json customManagers addition as plan artifacts (so implement just applies them). Keep style consistent with the existing workflow (2-space YAML, heredoc/python style untouched elsewhere).',
        'Return ONLY the structured JSON result, not a narrative.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['kustomizeVersion', 'kustomizeAssetUrl', 'kustomizeUrlOk', 'helmVersion', 'helmAssetUrl', 'helmUrlOk', 'noGithubApiCalls', 'newInstallStep', 'renovateManagerSnippet', 'filesToEdit', 'risks', 'summary'],
      properties: {
        kustomizeVersion: { type: 'string' },
        kustomizeAssetUrl: { type: 'string' },
        kustomizeUrlOk: { type: 'boolean' },
        helmVersion: { type: 'string' },
        helmAssetUrl: { type: 'string' },
        helmUrlOk: { type: 'boolean' },
        helmMajorChoiceRationale: { type: 'string' },
        noGithubApiCalls: { type: 'boolean' },
        retryDesign: { type: 'string' },
        newInstallStep: { type: 'string' },
        renovateManagerSnippet: { type: 'string' },
        filesToEdit: { type: 'array', items: { type: 'string' } },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 2 — implement: branch off origin/main, edit ci.yml + renovate.json
// (+ README if it documents the install), local commit, NO push.
// ---------------------------------------------------------------------------
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply pinned deterministic helm+kustomize install + retry to ci.yml + Renovate custom manager; branch + local commit (no push)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC/CI engineer applying the approved CI-hardening edit in the Epaflix repo (issue #' + args.issue + ')',
      task:
        'Apply the plan: edit ' + args.ciWorkflow + ' so the "Install tools" step pins + directly downloads helm and ' +
        'kustomize (no GitHub API tag lookup, no curl|bash from a moving branch) with a bounded retry, add the Renovate ' +
        'custom manager to ' + args.renovateJson + ', update ' + args.workflowsReadme + ' if it documents the install, then ' +
        'commit to a branch locally. NO push, NO PR yet.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md (no secrets; the install must remain secret-free — this is the secret-free gate). Touch ONLY ' + args.ciWorkflow + ', ' + args.renovateJson + ', and (only if it documents the tool install) ' + args.workflowsReadme + '. Do NOT touch any 2-k3s manifest or the YAML/helm/kustomize CHECK steps — only the "Install tools" step.',
        'APPLY ci.yml: replace the "Install tools" step run-script with the planned newInstallStep (context.newInstallStep) — pinned versions, direct official asset URLs, bounded retry, `set -euo pipefail`, keep the trailing `helm version` + `kustomize version` self-test. Add the `# renovate: datasource=... depName=...` marker comments so the custom manager regex matches.',
        'APPLY renovate.json: insert the planned customManagers entry (context.renovateManagerSnippet) as valid JSON (add a top-level "customManagers" array if absent). Keep the file valid JSON (it is parsed by Renovate) — validate with `python3 -c "import json;json.load(open(...))"` or `jq .`.',
        'If ' + args.workflowsReadme + ' describes the install commands, update it to match the pinned approach. Otherwise leave it.',
        'If feedback is present in context (a prior verify failure or gate rejection), incorporate it before committing.',
        'VALIDATE locally before commit: `git diff --check` (whitespace); the workflow YAML still parses (`python3 -c "import yaml,sys; list(yaml.safe_load_all(open(\'' + args.ciWorkflow + '\')))"`); renovate.json is valid JSON. If `actionlint` is available, run it on the workflow (note if not installed — do not hard-fail on a missing linter). Record what was run.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; reuse if it exists). Stage ONLY the edited files. ONE commit referencing #' + args.issue + ' (suggested subject: `ci: pin + direct-download helm/kustomize in validate gate to kill install flake (#' + args.issue + ')`). End the commit body with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'Author a PR title + body. The body MUST include a Test Plan section with checkbox items: (a) PR `validate` check passes (executes the new install step); (b) local re-run of `kustomize build` over the sops-free dirs with the pinned kustomize succeeds; (c) `helm pull` resolve spot-check with the pinned helm succeeds; (d) Renovate custom manager picks up the two pins (or a note that it will on next run). Cross-link #' + args.issue + ' and the PR (#163) that surfaced it.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'diff', 'yamlOk', 'renovateJsonOk', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
        yamlOk: { type: 'boolean' },
        renovateJsonOk: { type: 'boolean' },
        actionlintNote: { type: 'string' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 3 — local-verify: download the SAME pinned binaries to a temp dir and
// reproduce the exact checks the workflow runs (kustomize build + helm pull).
// ---------------------------------------------------------------------------
const verifyLocalTask = defineTask('verify-local', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Download the pinned helm+kustomize to a temp dir; reproduce the workflow checks (kustomize build sops-free dirs + helm pull spot-check)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'CI engineer proving the pinned binaries reproduce the existing `validate` checks (issue #' + args.issue + ')',
      task:
        'Download the EXACT pinned helm (' + args.helmVersion + ') and kustomize (' + args.kustomizeVersion + ') versions to an ' +
        'isolated temp dir using the SAME direct asset URLs the new workflow uses, then reproduce the two binary-dependent ' +
        'checks the `validate` gate runs to prove the pins behave identically: the `kustomize build` over the sops-free dirs, ' +
        'and a `helm pull` resolve spot-check. This validates the fix WITHOUT needing CI.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + ' on branch ' + args.branch + '. Do NOT install into system paths — download into a temp dir (e.g. mktemp -d) and call the binaries by absolute path so the host toolchain is untouched. Clean up the temp dir at the end.',
        'DOWNLOAD (mirror the new workflow exactly): kustomize from ' + args.kustomizeAssetUrl + ' and helm from ' + args.helmAssetUrl + ' via the same direct asset URLs (bounded retry ok). Extract; print `kustomize version` and `helm version` to confirm the pinned versions.',
        'KUSTOMIZE BUILD (the core check): run `<tmp>/kustomize build <dir> >/dev/null` for EXACTLY the sops-free dir list the workflow uses (read it from ' + args.ciWorkflow + ' "Kustomize build (sops-free dirs)" step so the list stays in sync): 2-k3s/01.kube-vip, 2-k3s/03.kube-vip-cloud-provider, 2-k3s/04.coredns, 2-k3s/06.postgres/operator-kustomization, 2-k3s/11.argocd/apps, 2-k3s/12.renovate, 2-k3s/maintenance, 2-k3s/maintenance/system-upgrade/controller. ALL must build cleanly. Record per-dir pass/fail.',
        'HELM PULL spot-check: pick 1-2 real helmCharts entries from a 2-k3s/**/kustomization.yaml and run the same `helm pull <name> --repo <repo> --version <ver> -d <tmp>` (or oci form) the workflow uses, to confirm the pinned helm resolves chart pins. Record result.',
        'Set verified=true ONLY if all sops-free dirs build AND the helm pull spot-check resolves AND the printed binary versions match the pins.',
        'If anything fails, set verified=false with the exact error + which file/dir + a concrete remediation hint for the implement phase (e.g. wrong kustomize minor changed a build behaviour). Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'kustomizeVersionConfirmed', 'helmVersionConfirmed', 'perDirBuild', 'helmPullOk', 'failures', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        kustomizeVersionConfirmed: { type: 'string' },
        helmVersionConfirmed: { type: 'string' },
        perDirBuild: { type: 'array', items: { type: 'object' } },
        helmPullOk: { type: 'boolean' },
        failures: { type: 'array', items: { type: 'string' } },
        remediation: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 4 — publish + merge per Epaflix policy. The PR's own `validate` run is
// the live proof; wait for it green before merging.
// ---------------------------------------------------------------------------
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, wait for new `validate` to pass on the PR, rebase + merge per Epaflix policy',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED CI-hardening change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open a PR, WAIT for the new `validate` check (which now executes the pinned install) to pass on the ' +
        'PR, then merge per the Epaflix policy (merge-commit + mandatory rebase / semi-linear, PR required, 0 approvals).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Rebase branch ' + args.branch + ' onto origin/main and `git push --force-with-lease` (strict up-to-date + required `validate` check block stale branches — see feedback_epaflix_merge_policy).',
        'Open a PR to main with the approved title/body (context: approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + ' and PR #163 (surfaced it).',
        'WAIT for the required `validate` check to pass on THIS PR — this is the live proof the new install step works (it executes the pinned download). Use `gh pr checks <pr> --watch` or poll `gh pr checks`. CRITICAL: if `validate` fails here, capture the failing log and return merged=false + validateGreen=false + the error — do NOT merge a broken gate. (A transient pre-existing-flake re-appearing once is fine to `gh run rerun --failed`, but a deterministic failure of the NEW step means the fix is wrong.)',
        'Once `validate` is green, merge with `gh pr merge --merge` (merge commit — never squash/rebase-merge). Capture the PR URL + merge commit SHA. Confirm MERGED before returning.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'validateGreen', 'merged', 'mergeSha'],
      properties: {
        prUrl: { type: 'string' },
        validateGreen: { type: 'boolean' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
        validateError: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 5 — post-verify: PR merged + `validate` green on main (push event).
// ---------------------------------------------------------------------------
const postVerifyTask = defineTask('post-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm PR merged + `validate` green on main after merge',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'CI engineer verifying issue #' + args.issue + ' is resolved with no regression',
      task:
        'Confirm the PR is merged and the post-merge `validate` run on main (push event) passes with the new pinned install, ' +
        'so the gate is now deterministic.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo ' + args.repo + '.',
        'Confirm PR ' + (args.prUrl || '(from context)') + ' is MERGED (`gh pr view --json state,mergedAt`).',
        'Find the `validate` workflow run triggered by the merge push to main (`gh run list --workflow ci.yml --branch main --limit 5`) and confirm its conclusion is success (the "Install tools" step pulled the pinned binaries with no API-tag lookup). If it is still running, wait briefly. Capture the run id + conclusion.',
        'Set verified=true only if merged AND the post-merge main `validate` concluded success.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'merged', 'mainValidateConclusion', 'runId', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        merged: { type: 'boolean' },
        mainValidateConclusion: { type: 'string' },
        runId: { type: 'string' },
        anomalies: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 6 — closeout: close #164, tick PR test plan (edit body), open follow-ups.
// ---------------------------------------------------------------------------
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #164 with outcome, update PR test plan (edit body), open follow-ups (helm-4 review, Renovate-pin cadence)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified CI change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by EDITING the PR body ' +
        '(never a new comment), and open the necessary follow-up issues per CLAUDE.md policy.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (Install tools now pins helm ' + args.helmVersion + ' + kustomize ' + args.kustomizeVersion + ', direct official asset download, no GitHub API tag lookup, bounded retry; Renovate custom manager keeps the pins fresh; PR `validate` + post-merge main `validate` both green) and CLOSE it. Cross-link PR #163.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording observed evidence inline (PR validate run id, local kustomize-build result, helm-pull result, Renovate note). Do NOT add a separate comment for the test plan (see feedback_pr_test_plans).',
        'Follow-ups (CLAUDE.md policy — enhancement shape ## Finding / ## Current state / ## Desired outcome / ## Notes, cross-link #' + args.issue + '): open a `gh issue` for (1) reviewing/adopting the helm v4 major in CI (we pinned helm 3.x conservatively — track when to move to v4 for `helm pull`), and (2) confirming the Renovate custom manager actually opens bump PRs for the two CI pins on its next run (verify cadence). If a suitable follow-up already exists, reference it instead. Return the URLs.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueState', 'prUpdated', 'followUpIssues'],
      properties: {
        issueState: { type: 'string' },
        prUpdated: { type: 'boolean' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    ciWorkflow: '.github/workflows/ci.yml',
    renovateJson: '.github/renovate.json',
    workflowsReadme: '.github/workflows/README.md',
    issue: '164',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'issue-164-ci-pin-kustomize-helm',
    relatedIssues: ['163'],
    ...inputs,
  };

  ctx.log('info', '#164 CI install-flake fix — plan (probe asset URLs) → implement (branch, no push) → local-verify loop → ONE owner gate (merge) → publish+merge (wait for PR validate green) → post-verify (main validate) → closeout');

  // PHASE 1 — plan (read-only).
  const plan = await ctx.task(planTask, {
    repoRoot: cfg.repoRoot, ciWorkflow: cfg.ciWorkflow, renovateJson: cfg.renovateJson,
    workflowsReadme: cfg.workflowsReadme, issue: cfg.issue,
  });
  ctx.log('info', `Plan: kustomize=${plan.kustomizeVersion} (urlOk=${plan.kustomizeUrlOk}); helm=${plan.helmVersion} (urlOk=${plan.helmUrlOk}); noApi=${plan.noGithubApiCalls}; files=${JSON.stringify(plan.filesToEdit)}`);

  if (!plan.kustomizeUrlOk || !plan.helmUrlOk) {
    const proceed = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ': plan could not confirm a direct release-asset URL — kustomize ' + cfg.issue + ' urlOk=' + plan.kustomizeUrlOk +
        ' (' + plan.kustomizeAssetUrl + '), helm urlOk=' + plan.helmUrlOk + ' (' + plan.helmAssetUrl + '). Pinning to an unverified URL ' +
        'risks a different failure. Summary: ' + plan.summary + '\n\nHow to proceed?',
      options: ['Abort (re-plan needed)', 'Proceed anyway (URLs are fine)'],
      expert: 'owner',
      tags: ['approval-gate'],
    });
    const pr = (proceed.response || '').toLowerCase();
    if (!proceed.approved || pr.includes('abort')) {
      return { success: false, decision: 'aborted-bad-urls', merged: false, reason: 'asset-url-unverified', plan };
    }
  }

  // PHASE 2 — implement (branch + local commit, no push).
  let change = await ctx.task(implementTask, {
    repoRoot: cfg.repoRoot, ciWorkflow: cfg.ciWorkflow, renovateJson: cfg.renovateJson,
    workflowsReadme: cfg.workflowsReadme, branch: cfg.branch, issue: cfg.issue,
    newInstallStep: plan.newInstallStep, renovateManagerSnippet: plan.renovateManagerSnippet,
    kustomizeVersion: plan.kustomizeVersion, helmVersion: plan.helmVersion,
  });
  ctx.log('info', `Implemented: branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.changedFiles)} yamlOk=${change.yamlOk} renovateOk=${change.renovateJsonOk}`);

  // PHASE 3 — local-verify LOOP: prove pinned binaries reproduce the checks; refine implement on failure.
  let verify = await ctx.task(verifyLocalTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, ciWorkflow: cfg.ciWorkflow, issue: cfg.issue,
    kustomizeVersion: plan.kustomizeVersion, kustomizeAssetUrl: plan.kustomizeAssetUrl,
    helmVersion: plan.helmVersion, helmAssetUrl: plan.helmAssetUrl,
  });
  for (let attempt = 0; attempt < 2 && !verify.verified; attempt++) {
    ctx.log('warn', `Local verify failed (attempt ${attempt + 1}): ${JSON.stringify(verify.failures)} — refining implement.`);
    change = await ctx.task(implementTask, {
      repoRoot: cfg.repoRoot, ciWorkflow: cfg.ciWorkflow, renovateJson: cfg.renovateJson,
      workflowsReadme: cfg.workflowsReadme, branch: cfg.branch, issue: cfg.issue,
      newInstallStep: plan.newInstallStep, renovateManagerSnippet: plan.renovateManagerSnippet,
      kustomizeVersion: plan.kustomizeVersion, helmVersion: plan.helmVersion,
      feedback: 'Local verify failed: ' + JSON.stringify(verify.failures) + ' | remediation: ' + (verify.remediation || ''),
      attempt: attempt + 2,
    });
    verify = await ctx.task(verifyLocalTask, {
      repoRoot: cfg.repoRoot, branch: change.branch, ciWorkflow: cfg.ciWorkflow, issue: cfg.issue,
      kustomizeVersion: plan.kustomizeVersion, kustomizeAssetUrl: plan.kustomizeAssetUrl,
      helmVersion: plan.helmVersion, helmAssetUrl: plan.helmAssetUrl, attempt: attempt + 2,
    });
  }
  ctx.log('info', `Local verify: verified=${verify.verified}; kustomize=${verify.kustomizeVersionConfirmed}; helmPullOk=${verify.helmPullOk}`);

  // GATE (merge) — ONE mandatory owner approval before push+merge. The required `validate` gate is
  // strict, so a bad edit breaks CI for every future PR. Retry/refine loop on rejection.
  let approved = false;
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      change = await ctx.task(implementTask, {
        repoRoot: cfg.repoRoot, ciWorkflow: cfg.ciWorkflow, renovateJson: cfg.renovateJson,
        workflowsReadme: cfg.workflowsReadme, branch: cfg.branch, issue: cfg.issue,
        newInstallStep: plan.newInstallStep, renovateManagerSnippet: plan.renovateManagerSnippet,
        kustomizeVersion: plan.kustomizeVersion, helmVersion: plan.helmVersion,
        feedback: lastFeedback, attempt: attempt + 5,
      });
      verify = await ctx.task(verifyLocalTask, {
        repoRoot: cfg.repoRoot, branch: change.branch, ciWorkflow: cfg.ciWorkflow, issue: cfg.issue,
        kustomizeVersion: plan.kustomizeVersion, kustomizeAssetUrl: plan.kustomizeAssetUrl,
        helmVersion: plan.helmVersion, helmAssetUrl: plan.helmAssetUrl, attempt: attempt + 5,
      });
    }
    const gate = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ' — harden the `validate` gate so the kustomize/helm install stops flaking.\n\n' +
        'PLAN: pin kustomize ' + plan.kustomizeVersion + ' + helm ' + plan.helmVersion + ', download official release assets DIRECTLY ' +
        '(no GitHub API tag lookup, no curl|bash from a moving branch), bounded retry; Renovate custom manager keeps pins fresh. ' +
        'No-GitHub-API-calls=' + plan.noGithubApiCalls + '.\n' +
        'LOCAL VERIFY (pinned binaries reproduce the checks): verified=' + verify.verified + '; kustomize build all sops-free dirs ok; helm pull spot-check=' + verify.helmPullOk + '.\n' +
        (plan.risks && plan.risks.length ? 'Risks: ' + JSON.stringify(plan.risks) + '\n' : '') +
        'Files: ' + JSON.stringify(change.changedFiles) + '\n\n' +
        'THIS GATE AUTHORIZES: push branch ' + change.branch + ', open PR, wait for the new `validate` to pass on the PR (live proof), then merge --merge per Epaflix policy. The required `validate` check is strict — a bad edit would block every future PR, which is why this is gated.\n\n' +
        '--- diff ---\n' + (change.diff || '(no diff captured)').slice(0, 5000) + '\n\nProceed with push + PR + merge?',
      options: ['Approve (push + PR + merge)', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['destructive-git', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate.response || '').toLowerCase();
    if (gate.approved && r.includes('approve')) { approved = true; break; }
    if (!gate.approved || r.includes('abort')) {
      ctx.log('warn', 'Gate not approved / aborted — nothing pushed.');
      return { success: false, decision: 'aborted', merged: false, reason: 'not-approved', feedback: gate.response || gate.feedback || '', plan, verify };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
    ctx.log('info', `Gate requested changes (attempt ${attempt + 1}); refining + re-verifying.`);
  }
  if (!approved) {
    return { success: false, decision: 'aborted', merged: false, reason: 'not-approved-after-retries', plan, verify };
  }

  // PHASE 4 — publish + merge (wait for PR validate green).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Publish: merged=${pub.merged}; validateGreen=${pub.validateGreen}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  if (!pub.merged || !pub.validateGreen) {
    const recover = await ctx.breakpoint({
      question:
        'Push/PR done but merge did not complete cleanly.\n' +
        'PR: ' + pub.prUrl + '\nvalidate green on PR: ' + pub.validateGreen + '; merged: ' + pub.merged + '\n' +
        (pub.validateError ? 'validate error: ' + pub.validateError + '\n' : '') +
        '\nIf `validate` failed on the NEW step the fix is wrong (do not merge). If it was a transient re-flake, a rerun may pass. How to proceed?',
      options: ['Retry merge (rerun validate + merge)', 'Stop here (leave PR open)'],
      expert: 'owner',
      tags: ['destructive-git', 'verification-gate'],
    });
    const rr = (recover.response || '').toLowerCase();
    if (recover.approved && rr.includes('retry')) {
      const pub2 = await ctx.task(publishMergeTask, {
        repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
        approvedPrTitle: change.prTitle, approvedPrBody: change.prBody, attempt: 2,
      });
      pub.merged = pub2.merged; pub.validateGreen = pub2.validateGreen; pub.prUrl = pub2.prUrl; pub.mergeSha = pub2.mergeSha;
    }
    if (!pub.merged) {
      return { success: false, decision: 'merge-incomplete', merged: false, prUrl: pub.prUrl, validateGreen: pub.validateGreen, reason: 'merge-not-completed', pub };
    }
  }

  // PHASE 5 — post-verify (main validate green).
  let post = await ctx.task(postVerifyTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, prUrl: pub.prUrl,
  });
  if (!post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post verification incomplete.\nMerged: ' + post.merged + '; main `validate` conclusion: ' + post.mainValidateConclusion + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\nSummary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (allow more time)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postVerifyTask, { repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, prUrl: pub.prUrl, attempt: 2 });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, decision: 'verify-stop', merged: true, prUrl: pub.prUrl, validateGreen: pub.validateGreen, reason: 'verification-stop', post };
    }
  }

  // PHASE 6 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, prUrl: pub.prUrl,
    kustomizeVersion: plan.kustomizeVersion, helmVersion: plan.helmVersion,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    decision: 'applied',
    merged: pub.merged,
    prUrl: pub.prUrl,
    validateGreen: post.mainValidateConclusion === 'success' || pub.validateGreen,
    issueState: close.issueState,
    followUpIssues: close.followUpIssues,
  };
}

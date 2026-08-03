/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Deliver Epaflix issue #134 (Option B): front newtarr.epaflix.com with Authentik
 *   SSO using the established single-application forward-auth pattern.
 *
 *   Owner decisions (interview, 2026-06-06):
 *     1. SSO-ONLY — Authentik gates BOTH local and internet. Remove the in-app LAN bypass
 *        (BYPASS_AUTH_FOR_LOCAL_ADDRESSES / LOCAL_NETWORK) so Authentik is the single gate.
 *     2. Access gated by the existing "Servarr users" Authentik group.
 *     3. Create ALL Authentik objects via the API (admin token in secrets.yml as
 *        `authentik_admin_api_token`; NEVER print it).
 *
 *   Two surfaces:
 *     - AUTHENTIK (live, via API, created FIRST): Proxy Provider (forward_single, external host
 *       https://newtarr.epaflix.com) + Application (slug `newtarr`) + group policy binding to
 *       "Servarr users" + assignment to the embedded outpost. Must exist BEFORE the Traefik
 *       middleware goes live, or authenticated requests fail (outpost has no provider for the host).
 *     - GITOPS (branch+PR+merge under the Epaflix merge-commit+rebase policy): attach the
 *       traefik-system/authentik-forwardauth middleware (+priority:10) to the newtarr-https
 *       IngressRoute, add a newtarr-outpost-https IngressRoute (priority:15,
 *       PathPrefix(/outpost.goauthentik.io/) -> authentik-server in app-authentik), and apply the
 *       SSO-only env change to newtarr.yaml. servarr App is selfHeal:true so the change must go
 *       through git.
 *
 *   Critical design nuance the plan MUST resolve at the gate: behind Traefik forward-auth, newtarr
 *   sees Traefik's in-cluster source IP, not the client's. With its STANDARD in-app auth still on,
 *   removing the LAN bypass could cause a SECOND login prompt after Authentik. "SSO-only with no
 *   double login" must be achieved (e.g. by making newtarr trust the proxied upstream, or by the
 *   cleanest mechanism newtarr v1.0.0 supports) — the planner analyzes and recommends, the owner
 *   approves.
 *
 * @inputs { repoRoot, repo, issue, branch, masterSsh, ns, servarrApp, host, slug,
 *           authentikBase, authentikNs, embeddedOutpostPk, authFlowPk, invalidationFlowPk,
 *           groupName, groupPk, middlewareName, middlewareNs, ingressFile, deployFile,
 *           kustomization, appServarrManifest, readmeFile, tokenKey, ssoDecision }
 * @outputs { success, merged, prUrl, authentikCreated, providerPk, appSlug, middlewareAttached,
 *            outpostRouteAdded, ssoOnlyApplied, ssoVerified, issueState, followUpIssues }
 *
 * Breakpoints (low tolerance / alwaysBreakOn architecture+deploy+secrets): plan review (also
 * authorizes the live Authentik object creation), deploy (push+PR+merge), and a conditional
 * anomaly gate on final verification.
 *
 * @agent general-purpose (Authentik-API / kubectl-over-ssh / git / gh / curl executor + verifier)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

const TOKEN_HINT =
  'The Authentik admin API token lives in .github/instructions/secrets.yml under the key ' +
  '`authentik_admin_api_token` (read it with: ' +
  "`auth=$(grep '^authentik_admin_api_token:' .github/instructions/secrets.yml | head -1 | cut -d'\"' -f2)`). " +
  'NEVER print, echo, or commit the token. secrets.yml is git-ignored and guarded by a pre-commit hook.';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// PHASE 1 — analyze the live + git state (NO mutation).
const analyzeStateTask = defineTask('analyze-state', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyze live + git state for the newtarr Authentik SSO change',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD/Authentik/Traefik SRE on the Epaflix k3s cluster',
      task:
        'Confirm the exact current state needed to front ' + args.host + ' with Authentik forward-auth ' +
        '(issue #' + args.issue + ', Option B). DO NOT mutate anything.',
      context: { ...args, tokenHint: TOKEN_HINT },
      instructions: [
        'Work from repoRoot=' + args.repoRoot + '. kubectl is over SSH — prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'GIT/MANIFESTS: read ' + args.ingressFile + ' (newtarr-https / newtarr-http IngressRoutes — confirm they carry NO middleware today, record the service name+port they target) and ' + args.deployFile + ' (record the EXACT env block, especially BYPASS_AUTH_FOR_LOCAL_ADDRESSES and LOCAL_NETWORK, the containerPort, and the image). Confirm the newtarr Service definition and how its port maps to the container 9705.',
        'PATTERN: read 2-k3s/05.traefik-deployment/middleware/authentik-forwardauth.yaml and 2-k3s/05.traefik-deployment/ingress/traefik-dashboard-sso.yaml — these are the canonical single-application forward-auth reference (app route priority:10 + middleware, separate outpost route priority:15 with PathPrefix(/outpost.goauthentik.io/) -> authentik-server@app-authentik:80). Note that NO servarr app uses this pattern yet (newtarr will be the first).',
        'LIVE: `' + args.masterSsh + ' \'kubectl -n ' + args.ns + ' get deploy,svc,ingressroute -l app=newtarr -o wide\'` and get the newtarr-https IngressRoute live YAML. Confirm newtarr pod Running 1/1 and the servarr ArgoCD App is Synced/Healthy (`' + args.masterSsh + ' \'kubectl -n argocd get application ' + args.servarrApp + ' -o jsonpath="{.status.sync.status}/{.status.health.status}"\'`).',
        'SOURCE-IP NUANCE (critical for the SSO-only decision): determine what source IP newtarr sees for requests arriving via Traefik (Traefik pod/cluster IP, NOT the client IP). This decides whether removing BYPASS_AUTH_FOR_LOCAL_ADDRESSES would cause newtarr to show its OWN login after Authentik (double login). Inspect newtarr v1.0.0 auth behaviour from its config/docs if reachable; otherwise reason from the LOCAL_NETWORK=192.168.10.0/24 value vs the pod/flannel CIDR.',
        'AUTHENTIK (read-only, via API at ' + args.authentikBase + '; ' + TOKEN_HINT + '): confirm there is NO existing proxy provider or application for newtarr (`GET /api/v3/providers/proxy/?search=newtarr`, `GET /api/v3/core/applications/?search=newtarr`). Confirm the embedded outpost (pk=' + args.embeddedOutpostPk + ') exists and record its CURRENT providers array (must be preserved when adding newtarr). Confirm the "Servarr users" group (pk=' + args.groupPk + ') exists. Confirm the authorization flow pk=' + args.authFlowPk + ' and invalidation flow pk=' + args.invalidationFlowPk + ' resolve.',
        'Return ONLY the structured JSON state — exact values, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['ingress', 'deployEnv', 'service', 'live', 'sourceIpBehaviour', 'authentik', 'summary'],
      properties: {
        ingress: { type: 'object' },
        deployEnv: { type: 'object' },
        service: { type: 'object' },
        live: { type: 'object' },
        sourceIpBehaviour: { type: 'object' },
        authentik: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 2 — produce the concrete plan (NO mutation).
const planTask = defineTask('plan-sso', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the concrete Authentik SSO implementation plan',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC/platform engineer planning an Authentik forward-auth rollout',
      task:
        'Turn the state analysis into a concrete, ordered, reversible plan to front ' + args.host + ' with ' +
        'Authentik SSO (Option B, SSO-ONLY). Plan only — no changes.',
      context: { ...args, tokenHint: TOKEN_HINT },
      instructions: [
        'AUTHENTIK OBJECTS (created FIRST, via API, idempotent): (1) Proxy Provider — name "newtarr", mode forward_single (Forward auth, single application), external_host "https://' + args.host + '", authorization_flow=' + args.authFlowPk + ', invalidation_flow=' + args.invalidationFlowPk + '. (2) Application — name "Newtarr", slug "' + args.slug + '", linked to the provider. (3) Group policy binding so ONLY the "' + args.groupName + '" group (pk=' + args.groupPk + ') is authorized on the application. (4) Add the new provider PK to the embedded outpost (pk=' + args.embeddedOutpostPk + ') providers array, PRESERVING the existing entries from the analysis. Give the exact API method+path+body for each, and the read-back checks that prove each was created.',
        'GIT CHANGE SET in ' + args.ingressFile + ': attach `middlewares: [{name: ' + args.middlewareName + ', namespace: ' + args.middlewareNs + '}]` and `priority: 10` to the newtarr-https route (keep the existing service/port), and add a NEW IngressRoute `newtarr-outpost-https` (entryPoints websecure, priority 15, match `Host(\\`' + args.host + '\\`) && PathPrefix(\\`/outpost.goauthentik.io/\\`)`, service authentik-server namespace app-authentik port 80, same cloudflare TLS block). Update the file header comment (it currently says routes carry NO forward-auth — flip it to document the SSO). Mirror the traefik-dashboard-sso.yaml shape exactly.',
        'SSO-ONLY DOUBLE-LOGIN RESOLUTION (the issue\'s explicit decide item): owner chose SSO-only (remove BYPASS_AUTH_FOR_LOCAL_ADDRESSES). Using the sourceIpBehaviour analysis, state precisely what newtarr will do after Authentik authenticates: if removing the bypass makes newtarr show its OWN login (double login), recommend the cleanest fix that still means "Authentik is the only gate the user sees" (e.g. keep newtarr trusting the proxied upstream so it never re-prompts, or disable newtarr in-app auth if v1.0.0 supports it). Be explicit and flag this as the key risk for the owner to weigh at the gate. Specify the EXACT env edit to ' + args.deployFile + '.',
        'Update ' + args.readmeFile + ' "Current Forward Auth Applications" list to add Newtarr (' + args.host + ').',
        'ORDER (and why): Authentik objects MUST exist before the Traefik middleware goes live (else the outpost has no provider for ' + args.host + ' and authenticated requests 400/redirect-loop). So: create Authentik objects -> author+validate git -> merge (ArgoCD applies middleware) -> verify.',
        'TEST PLAN: enumerate the post-merge verification — ArgoCD ' + args.servarrApp + ' Synced/Healthy, live newtarr-https IngressRoute shows the middleware, outpost route present, `curl -sI https://' + args.host + '` while unauthenticated returns a 302 to the Authentik flow (not a 200 newtarr page), and an authenticated check / no-double-login confirmation.',
        'Enumerate every breakpoint and the rollback for each surface (delete Authentik objects; revert the git PR).',
        'If feedback from a prior plan rejection is in context, incorporate it.',
        'Return ONLY the structured JSON plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['authentikSteps', 'gitChangeSet', 'ssoOnlyResolution', 'testPlan', 'order', 'risks', 'rollback', 'summary'],
      properties: {
        authentikSteps: { type: 'array', items: { type: 'object' } },
        gitChangeSet: { type: 'array', items: { type: 'object' } },
        ssoOnlyResolution: { type: 'object' },
        testPlan: { type: 'array', items: { type: 'string' } },
        order: { type: 'array', items: { type: 'string' } },
        risks: { type: 'array', items: { type: 'string' } },
        rollback: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 3 — create the Authentik objects via API (LIVE; gated by the approved plan).
const createAuthentikTask = defineTask('create-authentik', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Create the Authentik Proxy Provider + Application + outpost binding via API',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Authentik administrator operating the API',
      task:
        'Create the newtarr Authentik forward-auth objects exactly as the approved plan specifies. ' +
        'Idempotent: if an object already exists, reuse it rather than duplicating.',
      context: { ...args, tokenHint: TOKEN_HINT },
      instructions: [
        TOKEN_HINT,
        'Base URL ' + args.authentikBase + '/api/v3. Use `curl -sS -H "Authorization: Bearer $auth" -H "Content-Type: application/json"`. Treat HTTP >=400 as failure and report the body.',
        'STEP 1 Proxy Provider: POST /providers/proxy/ with {name:"newtarr", mode:"forward_single", external_host:"https://' + args.host + '", authorization_flow:"' + args.authFlowPk + '", invalidation_flow:"' + args.invalidationFlowPk + '"}. Record the returned pk. (If one named newtarr already exists, reuse its pk.)',
        'STEP 2 Application: POST /core/applications/ with {name:"Newtarr", slug:"' + args.slug + '", provider:<providerPk>}. (Reuse if slug exists.)',
        'STEP 3 Group authorization: bind the "' + args.groupName + '" group (pk=' + args.groupPk + ') to the application so only its members are authorized. POST /policies/bindings/ with {target:"<application_pk_uuid>", group:"' + args.groupPk + '", order:0, enabled:true}. (Resolve the application target UUID via GET /core/applications/' + args.slug + '/.) Verify the binding exists.',
        'STEP 4 Outpost: GET /outposts/instances/' + args.embeddedOutpostPk + '/, take its CURRENT providers array, append the new provider pk (dedupe), and PATCH /outposts/instances/' + args.embeddedOutpostPk + '/ with {providers:[...existing, newPk]}. NEVER drop existing providers.',
        'READ-BACK: re-GET the provider, application, policy binding, and outpost; confirm external_host, slug, group binding, and that the provider pk is in the outpost providers array.',
        'Do NOT touch any other Authentik object. Do NOT print the token. Return ONLY the structured JSON result with the created/confirmed PKs.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['providerPk', 'applicationSlug', 'applicationPk', 'groupBound', 'outpostUpdated', 'outpostProviders', 'readBackOk', 'summary'],
      properties: {
        providerPk: { type: ['integer', 'string'] },
        applicationSlug: { type: 'string' },
        applicationPk: { type: 'string' },
        groupBound: { type: 'boolean' },
        outpostUpdated: { type: 'boolean' },
        outpostProviders: { type: 'array' },
        readBackOk: { type: 'boolean' },
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

// PHASE 4 — author the git manifests + local commit on a branch.
const authorManifestsTask = defineTask('author-manifests', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the IngressRoute + env + README changes and commit on a branch',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer editing Kustomize/Traefik manifests under the Epaflix policy',
      task: 'Apply the approved git change set on a fresh branch and make a path-scoped local commit. No push.',
      context: { ...args, tokenHint: TOKEN_HINT },
      instructions: [
        'From repoRoot=' + args.repoRoot + ', create branch `' + args.branch + '` off an up-to-date origin/main (`git fetch origin && git switch -c ' + args.branch + ' origin/main`).',
        'Edit ' + args.ingressFile + ': add `priority: 10` and the `middlewares: [{name: ' + args.middlewareName + ', namespace: ' + args.middlewareNs + '}]` to the newtarr-https route; add the NEW `newtarr-outpost-https` IngressRoute (priority 15, PathPrefix(/outpost.goauthentik.io/) -> authentik-server@app-authentik:80, cloudflare TLS) exactly mirroring 2-k3s/05.traefik-deployment/ingress/traefik-dashboard-sso.yaml. Rewrite the file header comment to document that the route is now Authentik-SSO-fronted (remove the "NO forward-auth / deferred Option B" note) and cross-link #134.',
        'Edit ' + args.deployFile + ' per the approved ssoOnlyResolution (the env change). Preserve all unrelated env/keys, ports, probes, volumes.',
        'Update ' + args.readmeFile + ' "Current Forward Auth Applications" list to add Newtarr (' + args.host + ').',
        'Run `kustomize build 2-k3s/08.servarr >/dev/null` (or `kubectl kustomize`) to confirm it still renders; fix any error before committing.',
        'Commit ONLY the changed manifest/doc files (git add the specific paths — do NOT add .a5c/ or any process scaffolding; this repo keeps process defs untracked). Message: a clear feat/conventional subject referencing #' + args.issue + ', ending with the Co-Authored-By trailer the repo uses. Do NOT push.',
        'Return ONLY the structured JSON with the branch, commit sha, files changed, and render result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'filesChanged', 'renderOk', 'summary'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        renderOk: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 4b — validate (shell quality gate). 08.servarr is a KSOPS dir: full `kustomize build`
// needs the withheld age key + ksops binary, so (mirroring CI) validate offline by YAML-parsing
// the changed manifests and asserting the new SSO objects are present / inert env removed.
const validateTask = defineTask('validate-render', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Offline-safe validation of the newtarr SSO manifests (CI-equivalent)',
  shell: {
    command:
      'cd ' + args.repoRoot + ' && ' +
      "python3 -c \"import yaml; [list(yaml.safe_load_all(open(f))) for f in ['2-k3s/08.servarr/newtarr/ingressroute.yaml','2-k3s/08.servarr/newtarr/newtarr.yaml']]\" && " +
      "grep -q 'authentik-forwardauth' 2-k3s/08.servarr/newtarr/ingressroute.yaml && " +
      "grep -q 'newtarr-outpost-https' 2-k3s/08.servarr/newtarr/ingressroute.yaml && " +
      "grep -q 'PathPrefix(`/outpost.goauthentik.io/`)' 2-k3s/08.servarr/newtarr/ingressroute.yaml && " +
      "! grep -qE '^[[:space:]]*-[[:space:]]*name:[[:space:]]*BYPASS_AUTH_FOR_LOCAL_ADDRESSES' 2-k3s/08.servarr/newtarr/newtarr.yaml && " +
      "! grep -qE '^[[:space:]]*-[[:space:]]*name:[[:space:]]*LOCAL_NETWORK' 2-k3s/08.servarr/newtarr/newtarr.yaml && " +
      'echo YAML_VALIDATE_OK || echo RENDER_FAIL',
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
    stdoutPath: `tasks/${taskCtx.effectId}/stdout.txt`,
    stderrPath: `tasks/${taskCtx.effectId}/stderr.txt`,
  },
}));

// PHASE 5 — push + PR + rebase + merge (Epaflix merge-commit+rebase policy).
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase, wait for validate, merge',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer following the Epaflix merge-commit + mandatory-rebase (semi-linear) policy',
      task: 'Publish branch `' + args.branch + '`, open a PR for #' + args.issue + ', and merge it to main.',
      context: { ...args },
      instructions: [
        'From repoRoot=' + args.repoRoot + ' on branch ' + args.branch + ': `git push -u origin ' + args.branch + '`.',
        'Open the PR with `gh pr create` against ' + args.repo + ' base main. Body MUST include a `## Test plan` checklist with the verification steps from the approved plan (unchecked boxes) and cross-link #' + args.issue + ', #131, #132.',
        'Enforce the policy: `git fetch origin && git rebase origin/main` then `git push --force-with-lease`. Wait for the required `validate` check to pass (`gh pr checks --watch`). If it fails on the KNOWN unpinned-kustomize-install CI flake (fast ~6s fail), just `gh run rerun --failed` and re-watch.',
        'Merge with `gh pr merge <n> --merge` (NOT squash/rebase-merge). Confirm main now has the `Merge pull request #<n>` marker. Report the PR URL and number.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'prNumber', 'merged', 'validatePassed', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        prNumber: { type: ['integer', 'string'] },
        merged: { type: 'boolean' },
        validatePassed: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 5b — set newtarr "No Login Mode" (runtime /config flag) so Authentik is the SOLE gate.
// Runs AFTER merge (Authentik already fronts the route) to avoid any unauthenticated window.
const configureAuthModeTask = defineTask('configure-newtarr-authmode', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Set newtarr to No Login Mode (proxy_auth_bypass=true) so Authentik is the only gate',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE configuring the newtarr app runtime over kubectl-on-SSH',
      task:
        'Disable newtarr v1.0.0 in-app login so the only auth gate is Authentik forward-auth (SSO-only). ' +
        'newtarr does NOT read LOCAL_NETWORK/BYPASS env; the bypass is the persisted /config flag ' +
        '`general.proxy_auth_bypass` ("No Login Mode"). The Authentik middleware is ALREADY live in front of ' +
        'the route at this point, so this only removes the redundant SECOND login.',
      context: { ...args },
      instructions: [
        'kubectl is over SSH — prefix with `' + args.masterSsh + ' \'<kubectl ...>\'`. Namespace ' + args.ns + '.',
        'First READ the current value: exec into the newtarr pod and inspect the general settings JSON under /config (likely /config/settings/general.json or /config/general.json — locate it). Record current `proxy_auth_bypass` and the auth mode. If it is already true (No Login Mode), this is a no-op — just confirm.',
        'If not already set, set newtarr to No Login Mode. Prefer newtarr\'s own API/UI if a login session is available; otherwise patch the persisted JSON in the pod (set general.proxy_auth_bypass=true and the matching auth-mode field) and restart newtarr by deleting the pod (`kubectl -n ' + args.ns + ' delete pod -l app=newtarr`) so the Deployment recreates it and reloads /config. The config PVC persists across the restart.',
        'VERIFY: after restart, the newtarr pod is Running 1/1 and newtarr no longer serves its own login page on a direct (in-cluster) request. Confirm the flag persisted in /config.',
        'Do not change any unrelated setting. Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['previousValue', 'noLoginModeSet', 'podHealthy', 'flagPersisted', 'summary'],
      properties: {
        previousValue: { type: ['string', 'boolean', 'null'] },
        noLoginModeSet: { type: 'boolean' },
        podHealthy: { type: 'boolean' },
        flagPersisted: { type: 'boolean' },
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

// PHASE 6 — verify the live deploy + SSO end-to-end.
const verifyTask = defineTask('verify-deploy', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify ArgoCD sync + live middleware + SSO redirect end-to-end',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the Authentik SSO rollout against live state',
      task: 'Prove ' + args.host + ' is now fronted by Authentik SSO with no regression. Read-only checks.',
      context: { ...args, tokenHint: TOKEN_HINT },
      instructions: [
        'Wait for ArgoCD to reconcile: `' + args.masterSsh + ' \'kubectl -n argocd get application ' + args.servarrApp + ' -o jsonpath="{.status.sync.status}/{.status.health.status}"\'` must be Synced/Healthy. Poll up to a few minutes.',
        'Confirm the live newtarr-https IngressRoute carries the ' + args.middlewareName + ' middleware and that newtarr-outpost-https exists: `' + args.masterSsh + ' \'kubectl -n ' + args.ns + ' get ingressroute newtarr-https newtarr-outpost-https -o yaml\'`.',
        'Confirm the newtarr Deployment env reflects the SSO-only change: `' + args.masterSsh + ' \'kubectl -n ' + args.ns + ' get deploy newtarr -o jsonpath="{.spec.template.spec.containers[0].env}"\'` and that the pod rolled cleanly (Running 1/1).',
        'E2E (unauthenticated): `curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\\n" https://' + args.host + '/` — expect a 302/307 to the Authentik flow (/outpost.goauthentik.io/... or auth.epaflix.com), NOT a 200 newtarr page. Also `curl -sI https://' + args.host + '/outpost.goauthentik.io/auth/traefik` should be handled by the outpost (not 404 from newtarr).',
        'Confirm there is no double-login regression per the approved ssoOnlyResolution (reason about the flow; an authenticated browser check is out of scope for curl but state the expectation).',
        'If attempt>1 this is a re-verify after a transient issue. Return ONLY the structured JSON verdict.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'appSynced', 'middlewareLive', 'outpostRouteLive', 'ssoOnlyApplied', 'ssoRedirects', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        appSynced: { type: 'boolean' },
        middlewareLive: { type: 'boolean' },
        outpostRouteLive: { type: 'boolean' },
        ssoOnlyApplied: { type: 'boolean' },
        ssoRedirects: { type: 'boolean' },
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

// PHASE 7 — closeout: PR test plan, follow-ups, issue close.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Tick the PR test plan, open follow-ups, close #134',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Maintainer closing out the change per Epaflix conventions',
      task: 'Record verification on the PR, open follow-up issues, and close #' + args.issue + '.',
      context: { ...args },
      instructions: [
        'EXECUTE PR TEST PLAN: tick each box in the PR description by EDITING the PR body (gh pr edit / API) with the actual verification outcomes — NEVER add a new PR comment. If a step is N/A, strike it through and note why.',
        'FOLLOW-UPS: open a `gh issue` on ' + args.repo + ' for any deferred item surfaced (use the `## Finding / ## Current state / ## Desired outcome / ## Notes` shape; cross-link #' + args.issue + '). Likely candidates: roll the same forward-auth pattern to other servarr apps, or rotate the Authentik admin token now that it was written to secrets.yml. Only open issues that are genuinely warranted.',
        'Comment on #' + args.issue + ' summarizing what shipped (Authentik provider/app slug ' + args.slug + ', middleware attached, outpost route, SSO-only env, PR ' + (args.prUrl || '<url>') + ') and close it with `gh issue close`.',
        'Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['testPlanRecorded', 'issueState', 'followUpIssues', 'summary'],
      properties: {
        testPlanRecorded: { type: 'boolean' },
        issueState: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
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
// Orchestration
// ---------------------------------------------------------------------------

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: 'SpyrosPsarras/epaflix',
    issue: '134',
    branch: 'option-b-newtarr-authentik-sso-134',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    ns: 'servarr',
    servarrApp: 'servarr',
    host: 'newtarr.epaflix.com',
    slug: 'newtarr',
    authentikBase: 'https://auth.epaflix.com',
    authentikNs: 'app-authentik',
    embeddedOutpostPk: '209f71f9-95f8-4264-91c2-4b065bbd6b07',
    authFlowPk: '847dc682-757c-4bf8-925f-c8c066a0be4f',
    invalidationFlowPk: '436f6b06-d1a1-42db-af49-0eaac2bce88a',
    groupName: 'Servarr users',
    groupPk: '42960149-466e-43e2-b94e-00cdcc34115e',
    middlewareName: 'authentik-forwardauth',
    middlewareNs: 'traefik-system',
    ingressFile: '2-k3s/08.servarr/newtarr/ingressroute.yaml',
    deployFile: '2-k3s/08.servarr/newtarr/newtarr.yaml',
    kustomization: '2-k3s/08.servarr/kustomization.yaml',
    appServarrManifest: '2-k3s/11.argocd/apps/app-servarr.yaml',
    readmeFile: '2-k3s/07.authentik-deployment/README.md',
    tokenKey: 'authentik_admin_api_token',
    ssoDecision: 'sso-only',
    ...inputs,
  };

  ctx.log('info', '#134 newtarr Authentik SSO (Option B, SSO-only): analyze -> plan[BP] -> create-authentik -> author -> validate -> deploy[BP] -> verify -> closeout');

  // PHASE 1 — analyze.
  const state = await ctx.task(analyzeStateTask, {
    repoRoot: cfg.repoRoot, masterSsh: cfg.masterSsh, ns: cfg.ns, servarrApp: cfg.servarrApp,
    host: cfg.host, issue: cfg.issue, ingressFile: cfg.ingressFile, deployFile: cfg.deployFile,
    authentikBase: cfg.authentikBase, embeddedOutpostPk: cfg.embeddedOutpostPk,
    authFlowPk: cfg.authFlowPk, invalidationFlowPk: cfg.invalidationFlowPk,
    groupPk: cfg.groupPk,
  });
  ctx.log('info', `Analyze: ${state.summary}`);

  // PHASE 2 — plan with refine/review loop; the gate ALSO authorizes the live Authentik creation.
  let plan, lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    plan = await ctx.task(planTask, {
      ...cfg, state, feedback: lastFeedback || undefined, attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const gate = await ctx.breakpoint({
      question:
        'Review the PLAN for #' + cfg.issue + ' (Authentik SSO on ' + cfg.host + ', SSO-only).\n\n' +
        'Authentik objects: ' + JSON.stringify(plan.authentikSteps) + '\n' +
        'Git change set: ' + JSON.stringify(plan.gitChangeSet) + '\n' +
        'SSO-only / double-login resolution: ' + JSON.stringify(plan.ssoOnlyResolution) + '\n' +
        'Order: ' + JSON.stringify(plan.order) + '\n' +
        'Test plan: ' + JSON.stringify(plan.testPlan) + '\n' +
        'Risks: ' + JSON.stringify(plan.risks) + '\n' +
        'Rollback: ' + JSON.stringify(plan.rollback) + '\n\n' +
        'Summary: ' + plan.summary + '\n\n' +
        'Approving ALSO authorizes creating the live Authentik Proxy Provider + Application + group ' +
        'binding + outpost assignment via the API. Approve?',
      options: ['Approve plan', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['plan-gate', 'architecture-change', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (gate.approved && !(gate.response || '').toLowerCase().includes('change')) break;
    if (!gate.approved && (gate.response || '').toLowerCase().includes('abort')) {
      ctx.log('warn', 'Plan aborted by owner.');
      return { success: false, reason: 'plan-aborted', feedback: gate.response || gate.feedback || '', state, plan };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
  }

  // PHASE 3 — create Authentik objects FIRST (must precede the live middleware).
  const auth = await ctx.task(createAuthentikTask, { ...cfg, plan, state });
  ctx.log('info', `Authentik: provider=${auth.providerPk} app=${auth.applicationSlug} groupBound=${auth.groupBound} outpost=${auth.outpostUpdated} readBack=${auth.readBackOk}`);
  if (!auth.readBackOk) {
    const ag = await ctx.breakpoint({
      question:
        'Authentik object creation did not fully read back.\n' +
        'provider=' + auth.providerPk + ' app=' + auth.applicationSlug + ' groupBound=' + auth.groupBound +
        ' outpostUpdated=' + auth.outpostUpdated + '\nanomalies: ' + JSON.stringify(auth.anomalies) +
        '\nsummary: ' + auth.summary + '\n\nHow to proceed?',
      options: ['Continue anyway', 'Stop here'],
      expert: 'owner',
      tags: ['anomaly-gate'],
    });
    if (!ag.approved || (ag.response || '').toLowerCase().includes('stop')) {
      return { success: false, reason: 'authentik-create-failed', auth, state, plan };
    }
  }

  // PHASE 4 — author git + validate (refine loop on render failure).
  let change = await ctx.task(authorManifestsTask, { ...cfg, approvedPlan: plan, state });
  ctx.log('info', `Authored: branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.filesChanged)} renderOk=${change.renderOk}`);
  let val = await ctx.task(validateTask, { repoRoot: cfg.repoRoot });
  let valOut = (val && (val.stdout || val.output || '')) || '';
  if (!change.renderOk || /RENDER_FAIL/.test(String(valOut))) {
    change = await ctx.task(authorManifestsTask, { ...cfg, approvedPlan: plan, state, feedback: 'kustomize render failed; fix and re-commit on the same branch', attempt: 2 });
    val = await ctx.task(validateTask, { repoRoot: cfg.repoRoot });
    valOut = (val && (val.stdout || val.output || '')) || '';
  }

  // GATE — deploy (push + PR + rebase + merge => ArgoCD applies the live middleware).
  const deployGate = await ctx.breakpoint({
    question:
      'Approve DEPLOY for #' + cfg.issue + '? Push branch `' + change.branch + '`, open the PR, rebase onto ' +
      'origin/main, wait for `validate`, and `gh pr merge --merge`. ArgoCD (servarr selfHeal:true) will then ' +
      'apply the ' + cfg.middlewareName + ' middleware + outpost route LIVE to ' + cfg.host + '. The Authentik ' +
      'objects are already created. After merge, newtarr will be set to "No Login Mode" ' +
      '(proxy_auth_bypass=true, runtime /config flag) so Authentik is the SOLE gate — no double login. ' +
      'Files: ' + JSON.stringify(change.filesChanged) + '. Proceed?',
    options: ['Approve deploy', 'Skip merge (stop)', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'destructive-git', 'approval-gate'],
  });
  if (!deployGate.approved || (deployGate.response || '').toLowerCase().match(/abort|skip|stop/)) {
    return { success: false, merged: false, reason: 'deploy-not-approved', branch: change.branch, auth, feedback: deployGate.response || '' };
  }

  // PHASE 5 — publish + merge.
  const pub = await ctx.task(publishMergeTask, { ...cfg, branch: change.branch });
  ctx.log('info', `Publish: pr=${pub.prUrl} merged=${pub.merged} validate=${pub.validatePassed}`);
  if (!pub.merged) {
    return { success: false, merged: false, reason: 'merge-failed', prUrl: pub.prUrl, validatePassed: pub.validatePassed, auth };
  }

  // PHASE 5b — disable newtarr in-app login (No Login Mode) now that Authentik fronts the route.
  const authMode = await ctx.task(configureAuthModeTask, { ...cfg, plan });
  ctx.log('info', `AuthMode: prev=${authMode.previousValue} noLogin=${authMode.noLoginModeSet} healthy=${authMode.podHealthy} persisted=${authMode.flagPersisted}`);

  // PHASE 6 — verify, with an anomaly/re-verify gate.
  let verify = await ctx.task(verifyTask, { ...cfg });
  if (!verify.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Final verification found issues.\n' +
        'Synced: ' + verify.appSynced + '; middlewareLive: ' + verify.middlewareLive + '; outpostRoute: ' + verify.outpostRouteLive + '\n' +
        'ssoOnlyApplied: ' + verify.ssoOnlyApplied + '; ssoRedirects: ' + verify.ssoRedirects + '\n' +
        'anomalies: ' + JSON.stringify(verify.anomalies) + '\nsummary: ' + verify.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      verify = await ctx.task(verifyTask, { ...cfg, attempt: 2 });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, merged: true, prUrl: pub.prUrl, reason: 'verification-stop', verify, auth };
    }
  }

  // PHASE 7 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, slug: cfg.slug, host: cfg.host, prUrl: pub.prUrl,
    verify, auth,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    merged: pub.merged,
    prUrl: pub.prUrl,
    authentikCreated: !!auth.readBackOk,
    providerPk: auth.providerPk,
    appSlug: auth.applicationSlug,
    middlewareAttached: verify.middlewareLive,
    outpostRouteAdded: verify.outpostRouteLive,
    ssoOnlyApplied: verify.ssoOnlyApplied,
    noLoginModeSet: authMode.noLoginModeSet,
    ssoVerified: verify.verified && verify.ssoRedirects,
    issueState: close.issueState,
    followUpIssues: close.followUpIssues,
  };
}

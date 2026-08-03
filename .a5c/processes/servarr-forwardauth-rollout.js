/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Roll Authentik forward-auth SSO to the remaining servarr web UIs (issue #176).
 *   Codifies the out-of-band (drifted, non-ArgoCD) public IngressRoutes into git, attaches the
 *   cluster-wide `authentik-forwardauth` middleware + per-app outpost route, and declares the
 *   matching Authentik proxy-provider / application / embedded-outpost binding in the SOPS
 *   blueprint (full GitOps, via the ak-iac SA token from #185). Two-stage deploy: Authentik
 *   objects first (PR-A) then middleware (PR-B), each behind a human merge gate. Tests each app
 *   afterward. All file edits happen in an isolated git worktree because another agent shares the tree.
 * @inputs { issue: number, worktree: string, repo: string, apps: array }
 * @outputs { success: boolean, gatedApps: array, prA: number, prB: number }
 * @skill code-review code-review
 * @skill verify verify
 * @agent general-purpose
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

// ---------------------------------------------------------------------------
// Shared context every worker needs
// ---------------------------------------------------------------------------
const REPO = 'SpyrosPsarras/epaflix';
const WORKTREE = '/home/spy/Documents/Epaflix/wt-servarr-fwauth-176';
const BRANCH_BLUEPRINT = 'servarr-authentik-blueprint-176';
const BRANCH_INGRESS = 'servarr-forwardauth-rollout-176';

// apiBypass: true => app is an INTEGRATION TARGET (other apps/clients hit its /api over the
//   public host). Add a priority-20 Host && PathPrefix(`/api`) route with NO forward-auth so
//   machine/API-key traffic (and external/mobile/deep-link callers) keeps working over the
//   public host; the UI catch-all (priority 10) still gets forward-auth.
// apiBypass: false => pure UI / caller-only (no inbound API). Gate the WHOLE host (like newtarr).
const APPS = [
  { name: 'sonarr',      host: 'sonarr.epaflix.com',      svc: 'sonarr',      port: 8989,  dir: '2-k3s/08.servarr/sonarr',      apiBypass: true,  calledBy: 'seerr, bazarr, cleanuparr, newtarr' },
  { name: 'sonarr2',     host: 'sonarr2.epaflix.com',     svc: 'sonarr2',     port: 8989,  dir: '2-k3s/08.servarr/sonarr2',     apiBypass: true,  calledBy: 'seerr, cleanuparr, newtarr' },
  { name: 'radarr',      host: 'radarr.epaflix.com',      svc: 'radarr',      port: 7878,  dir: '2-k3s/08.servarr/radarr',      apiBypass: true,  calledBy: 'seerr, bazarr, cleanuparr, newtarr' },
  { name: 'prowlarr',    host: 'prowlarr.epaflix.com',    svc: 'prowlarr',    port: 9696,  dir: '2-k3s/08.servarr/prowlarr',    apiBypass: true,  calledBy: 'newtarr (sonarr/radarr->prowlarr use internal DNS)', note: 'live has BOTH prowlarr-https and a duplicate prowlarr-external for the same Host(); consolidate to a single canonical app route and drop the duplicate' },
  { name: 'qbittorrent', host: 'qbittorrent.epaflix.com', svc: 'qbittorrent', port: 8080,  dir: '2-k3s/08.servarr/qbittorrent', apiBypass: true,  calledBy: 'sonarr, sonarr2, radarr, cleanuparr, newtarr', note: 'qBittorrent API is under /api/v2 — /api PathPrefix covers it.' },
  { name: 'bazarr',      host: 'bazarr.epaflix.com',      svc: 'bazarr',      port: 6767,  dir: '2-k3s/08.servarr/bazarr',      apiBypass: true,  calledBy: 'possibly lingarr (bazarr also CALLS sonarr/radarr); exempt /api to be safe' },
  { name: 'cleanuparr',  host: 'cleanuparr.epaflix.com',  svc: 'cleanuparr',  port: 30263, dir: '2-k3s/08.servarr/cleanuparr',  apiBypass: false, calledBy: 'none (caller-only) — gate whole host' },
  { name: 'homarr',      host: 'homarr.epaflix.com',      svc: 'homarr',      port: 7575,  dir: '2-k3s/08.servarr/homarr',      apiBypass: false, calledBy: 'none (dashboard, caller-only) — gate whole host' },
  { name: 'lingarr',     host: 'lingarr.epaflix.com',     svc: 'lingarr',     port: 9876,  dir: '2-k3s/08.servarr/lingarr',     apiBypass: false, calledBy: 'none detected (caller-only)', note: 'ALREADY has 2-k3s/08.servarr/lingarr/ingress.yaml in git (currently no auth) — UPDATE that file in place, do not create a new one' },
  { name: 'wizarr',      host: 'wizarr.epaflix.com',      svc: 'wizarr',      port: 5690,  dir: '2-k3s/08.servarr/wizarr',      apiBypass: false, calledBy: 'none; app DOWN/unused — gate whole host', note: 'If no app dir exists, create 2-k3s/08.servarr/wizarr/ingressroute.yaml.' },
];

const REFERENCE_FILES = {
  newtarrIngress: '2-k3s/08.servarr/newtarr/ingressroute.yaml',
  middleware: '2-k3s/05.traefik-deployment/middleware/authentik-forwardauth.yaml',
  fwauthExample: '2-k3s/05.traefik-deployment/examples/protected-app-with-sso.yaml',
  servarrKustomization: '2-k3s/08.servarr/kustomization.yaml',
  authentikBlueprint: '2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml',
  authentikHelmValues: '2-k3s/07.authentik-deployment/helm-values.yaml',
  authentikReadme: '2-k3s/07.authentik-deployment/README.md',
  outpostIngress: '2-k3s/07.authentik-deployment/ingress/outpost-ingressroute.yaml',
  sopsInstructions: '.github/instructions/sops.instructions.md',
};

const MERGE_POLICY = [
  'Epaflix merge policy: merge-commit + mandatory rebase (semi-linear). Every change lands via branch + PR (0 approvals required).',
  'Before merging: rebase the branch onto origin/main, then `git push --force-with-lease`. The required `validate` status check + strict up-to-date rule block stale branches.',
  'Wait for the `validate` check to pass green, then merge with `gh pr merge <n> --merge`.',
  'NEVER commit secrets or plaintext `kind: Secret` YAML — the pre-commit hook refuses it. Encrypted Secret files use the `.enc.yaml` suffix (SOPS+age).',
  'Do not `git add -f` anything .gitignore matches.',
];

const COMMON_CONTEXT = {
  repo: REPO, worktree: WORKTREE, apps: APPS,
  referenceFiles: REFERENCE_FILES, mergePolicy: MERGE_POLICY,
  rules: [
    'ALL file edits MUST happen inside the worktree at ' + WORKTREE + ' (another agent shares the main checkout). Run git commands with `git -C ' + WORKTREE + '`.',
    'Inter-app servarr API calls use internal cluster service DNS (e.g. http://sonarr:8989), NEVER the public *.epaflix.com host, so forward-auth on the public host does not break integrations.',
    'The authentik-forwardauth middleware lives in namespace traefik-system; reference it as {name: authentik-forwardauth, namespace: traefik-system}.',
    'The newtarr template is the canonical reference: app route priority 10 with the middleware, outpost route priority 15 matching PathPrefix(`/outpost.goauthentik.io/`) backending authentik-server in app-authentik, plus an http->https redirect route.',
    'Gate every app with the existing "Servarr users" group (do not create new per-app groups).',
  ],
};

// ===========================================================================
// PHASE 1 — DISCOVERY (live Authentik facts + blueprint format)
// ===========================================================================
export const discoverTask = defineTask('discover-authentik-state', (args, t) => ({
  kind: 'agent',
  title: 'Discover live Authentik state + blueprint format',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Kubernetes / Authentik platform engineer',
      task: 'Gather every live fact needed to declare 10 new forward-auth apps in the Authentik SOPS blueprint, and to faithfully codify their drifted IngressRoutes. Read-only. Do NOT change anything.',
      context: { ...COMMON_CONTEXT, ...args },
      instructions: [
        'Read the reference files listed in referenceFiles to learn the exact newtarr IngressRoute shape and the middleware definition.',
        'Decode the current Authentik blueprint to understand its format: run `sops -d ' + REFERENCE_FILES.authentikBlueprint + '` from the worktree (the age key is configured for this repo). Capture the YAML structure of stringData -> iac-service-account.yaml: what blueprint `entries` already exist, the metadata, and how providers/applications/groups are expressed. If sops decode fails, report exactly why.',
        'Find the ak-iac API token (#185): it is the `ak-iac-token` declared in that blueprint. Using it, query the LIVE Authentik API at https://auth.epaflix.com/api/v3/ (header `Authorization: Bearer <token>`). Capture: (a) the embedded outpost — its pk and its current `providers` list (provider pks); (b) all existing proxy providers (pk + name) — especially newtarr (pk 82) and the traefik dashboard; (c) the "Servarr users" group — its pk and slug; (d) the default authorization/auth flow pk used by newtarr proxy provider.',
        'For each of the 10 apps in `apps`, confirm the live IngressRoute service name + port match what is in the apps list (kubectl get ingressroute -n servarr). Note any app whose route is already partly in git (lingarr) and the prowlarr duplicate route.',
        'Determine the cleanest declarative way to bind all new providers to the embedded outpost via blueprint: Authentik blueprint `authentik_outposts.outpost` entries set the providers list declaratively — so the blueprint MUST list ALL providers that should remain bound (existing newtarr + traefik + the 10 new ones). Recommend whether to manage the outpost binding via blueprint `!Find` references or to keep it imperative, and explain the risk of clobbering existing bindings.',
      ],
      outputFormat: 'JSON with keys: outpostPk, outpostCurrentProviders (array of {pk,name}), servarrGroup {pk,slug}, newtarrProvider {pk,name,authFlowPk,authorizationFlowPk,externalHost}, blueprintFormatNotes (string), blueprintEntriesSummary (string), perApp (array of {name,svcOk,port,gitState,notes}), outpostBindingStrategy (string), risks (array of strings), sopsDecodeOk (boolean)',
    },
    outputSchema: { type: 'object', required: ['outpostPk', 'servarrGroup', 'newtarrProvider', 'blueprintFormatNotes', 'perApp', 'outpostBindingStrategy'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 2 — DESIGN (single source-of-truth plan, reviewed at a gate)
// ===========================================================================
export const designTask = defineTask('design-rollout', (args, t) => ({
  kind: 'agent',
  title: 'Design the forward-auth rollout (manifests + blueprint + 2-PR deploy + test plan)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'GitOps platform architect',
      task: 'Produce a precise, reviewable design for rolling forward-auth to the 10 servarr apps, using the discovery facts. No file edits yet — design only. Write the design to artifacts/176-design.md in the worktree.',
      context: { ...COMMON_CONTEXT, ...args },
      instructions: [
        'Define exactly what each app IngressRoute file will contain (codified drift + app route priority 10 with authentik-forwardauth + outpost route priority 15 + http->https redirect), modeled on newtarr/ingressroute.yaml. Note lingarr edits its existing file and prowlarr consolidates its duplicate.',
        'Define the exact blueprint additions: one proxy provider + one application per app (gated by the Servarr users group, in-app/forward-auth single-application mode, external host https://<host>), and the embedded-outpost providers binding listing ALL providers (existing + 10 new) per the chosen strategy.',
        'Define the kustomization.yaml changes needed in 2-k3s/08.servarr to include each new/updated IngressRoute file.',
        'Specify the two-stage deploy: PR-A = Authentik blueprint only (branch ' + BRANCH_BLUEPRINT + '); merge + verify outpost has all providers live BEFORE PR-B. PR-B = IngressRoutes + kustomization (branch ' + BRANCH_INGRESS + '). Explain why this order avoids lockout/500 (middleware must not go live before its provider exists on the outpost).',
        'Specify the per-app test plan: anonymous `curl -sI https://<host>/` over PUBLIC DNS must return 302/307 redirect to auth.epaflix.com (gated); confirm an authenticated path works; confirm inter-app integration is unaffected (internal DNS). Include a check that newtarr (pk 82) and traefik dashboard remain bound to the outpost (no regression).',
        'List risks and rollback (remove provider pks from outpost / drop middleware) and any follow-up issues to open.',
        'Write the full design to ' + WORKTREE + '/artifacts/176-design.md and return a concise summary.',
      ],
      outputFormat: 'JSON: { designPath, summary, ingressFilePlan (array), blueprintPlan (string), kustomizationChanges (string), deployPlan (string), testPlan (string), risks (array), followups (array) }',
    },
    outputSchema: { type: 'object', required: ['designPath', 'summary', 'deployPlan', 'testPlan'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 3 — AUTHOR (fan-out: per-app IngressRoute + one blueprint editor)
// ===========================================================================
export const authorIngressTask = defineTask('author-ingress', (args, t) => ({
  kind: 'agent',
  title: `Author IngressRoute for ${args.app.name}`,
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Kubernetes manifest author',
      task: `Create or update the forward-auth IngressRoute manifest for ${args.app.name} in the worktree, modeled exactly on the newtarr template. Do NOT touch any other app's files.`,
      context: { ...COMMON_CONTEXT, app: args.app, design: args.design },
      instructions: [
        'Read ' + REFERENCE_FILES.newtarrIngress + ' as the template.',
        'ARCHITECTURE = UI-gate + /api-bypass (NO internal-DNS repoint; inter-app calls keep using public hosts on /api with their API key). Produce routes for Host(`<host>`), entryPoints [websecure], tls certResolver cloudflare main epaflix.com + sans *.epaflix.com:',
        'Route A (UI, ALWAYS): name <app>-https, priority 10, match Host(`<host>`), middleware {name: authentik-forwardauth, namespace: traefik-system}, service {name: <svc>, port: <port>}.',
        'Route B (outpost, ALWAYS): name <app>-outpost-https, priority 15, match Host(`<host>`) && PathPrefix(`/outpost.goauthentik.io/`), service authentik-server in app-authentik port 80.',
        'Route C (API-bypass, ONLY IF app.apiBypass === true): name <app>-api, priority 20 (HIGHER than UI so it wins for /api), match Host(`<host>`) && PathPrefix(`/api`), NO forward-auth middleware, service {name: <svc>, port: <port>}. This keeps machine/API-key + external/mobile/deep-link traffic working. If app.apiBypass === false (pure UI: cleanuparr, homarr, lingarr, wizarr) DO NOT add Route C — gate the whole host like newtarr.',
        'Route D (http redirect, ALWAYS): name <app>-http, match Host(`<host>`), middleware redirect-https.',
        'Add a clear header comment: which routes exist, that /api is intentionally exempt from SSO for the integration-target apps (protected by the app API key — this matches today`s API exposure, only the UI is newly gated), and reference issue #176. For app.apiBypass apps note in the comment which apps call it (app.calledBy).',
        'For lingarr: UPDATE the existing 2-k3s/08.servarr/lingarr/ingress.yaml in place (add middleware + outpost route; lingarr is apiBypass=false so gate whole host), do not create a duplicate file. For prowlarr: consolidate to a single canonical set of routes, removing the duplicate prowlarr-external definition if present in git.',
        'For wizarr: if no app dir/kustomization include exists in git, create 2-k3s/08.servarr/wizarr/ingressroute.yaml.',
        'Write files ONLY under the worktree (' + WORKTREE + '). Do NOT edit kustomization.yaml (a separate step owns that to avoid write races). Do NOT commit.',
        'Match repo YAML style (2-space indent).',
      ],
      outputFormat: 'JSON: { app, filesWritten (array of absolute paths), notes }',
    },
    outputSchema: { type: 'object', required: ['app', 'filesWritten'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

export const authorKustomizationTask = defineTask('author-kustomization', (args, t) => ({
  kind: 'agent',
  title: 'Wire new IngressRoute files into the servarr kustomization',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Kustomize maintainer',
      task: 'Add every new/updated IngressRoute file to 2-k3s/08.servarr/kustomization.yaml so ArgoCD reconciles them. Run after all per-app IngressRoute files are written.',
      context: { ...COMMON_CONTEXT, ...args },
      instructions: [
        'Read the current 2-k3s/08.servarr/kustomization.yaml in the worktree.',
        'Ensure each app IngressRoute file produced in the author step is listed under resources (lingarr already listed; verify it still points at the updated file).',
        'Keep ordering/style consistent with existing entries (near each app deployment block).',
        'Edit ONLY in the worktree. Do NOT commit.',
        'Run `kustomize build 2-k3s/08.servarr` from the worktree and confirm it succeeds and renders the new middleware on each route.',
      ],
      outputFormat: 'JSON: { kustomizationUpdated (boolean), buildOk (boolean), resourcesAdded (array), buildErrors (string) }',
    },
    outputSchema: { type: 'object', required: ['kustomizationUpdated', 'buildOk'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

export const authorBlueprintTask = defineTask('author-blueprint', (args, t) => ({
  kind: 'agent',
  title: 'Add proxy providers + apps + outpost binding to the Authentik SOPS blueprint',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Authentik blueprint engineer',
      task: 'Edit the SOPS-encrypted Authentik blueprint to declare a proxy provider + application per app and bind all providers to the embedded outpost. Keep the file encrypted with the repo age recipient.',
      context: { ...COMMON_CONTEXT, discovery: args.discovery, design: args.design },
      instructions: [
        'Decrypt in place for editing using the SOPS recipe in ' + REFERENCE_FILES.sopsInstructions + ' (e.g. `sops ' + REFERENCE_FILES.authentikBlueprint + '` or decrypt->edit->encrypt). Follow that file exactly so the pre-commit hook accepts the result.',
        'Use the discovery facts (servarrGroup pk/slug, newtarr provider auth/authorization flow pks, outpost pk, existing providers list) so new entries mirror newtarr exactly: forward-auth single-application proxy provider, external host https://<host>, mode forward_single, gated by the Servarr users group.',
        'Add one application entry per app (name + slug = app name, linked to its provider), gated by the Servarr users group binding, matching how newtarr is expressed.',
        'Bind providers to the embedded outpost using the strategy chosen in discovery. CRITICAL: the outpost providers list must include the existing providers (newtarr pk 82, traefik dashboard) AND all 10 new ones — never drop existing bindings. Prefer `!Find` references by provider name so pks are not hardcoded.',
        'Re-encrypt and verify: `sops -d <file>` round-trips, and the pre-commit hook check (.github/hooks/check-sops-encrypted.sh) passes (no plaintext kind: Secret).',
        'Edit ONLY in the worktree. Do NOT commit.',
      ],
      outputFormat: 'JSON: { blueprintEdited (boolean), providersAdded (array), appsAdded (array), outpostBindingNote (string), encryptedOk (boolean), sopsRoundTripOk (boolean) }',
    },
    outputSchema: { type: 'object', required: ['blueprintEdited', 'encryptedOk', 'sopsRoundTripOk'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 4 — VALIDATE (build/render/encryption gates, refine loop)
// ===========================================================================
export const validateTask = defineTask('validate-manifests', (args, t) => ({
  kind: 'agent',
  title: 'Validate manifests, blueprint encryption, and kustomize/helm render',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'IaC validation engineer',
      task: 'Validate everything authored so far the same way CI `validate` will, and report a clear pass/fail with actionable errors.',
      context: { ...COMMON_CONTEXT, feedback: args.feedback, attempt: args.attempt },
      instructions: [
        'From the worktree, run `kustomize build 2-k3s/08.servarr` and confirm success + that each gated app route now carries the authentik-forwardauth middleware and a priority-15 outpost route.',
        'Render the authentik kustomization if it uses helm (`kustomize build --enable-helm 2-k3s/07.authentik-deployment` or the repo`s documented validate command) and confirm the blueprint secret still decrypts.',
        'Run the SOPS pre-commit guard `.github/hooks/check-sops-encrypted.sh` (or equivalent) against the staged/working changes and confirm no plaintext kind: Secret.',
        'Confirm jellyfin and seerr routes are untouched, and that newtarr is unchanged.',
        'If the repo has a single `validate`-equivalent script, run it. Report every failure verbatim.',
      ],
      outputFormat: 'JSON: { pass (boolean), kustomizeOk (boolean), helmOk (boolean), sopsGuardOk (boolean), middlewarePresentForAllApps (boolean), failures (array of strings) }',
    },
    outputSchema: { type: 'object', required: ['pass', 'failures'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 5 — PREP PR-A (Authentik blueprint) — no merge yet
// ===========================================================================
export const prepBlueprintPrTask = defineTask('prep-blueprint-pr', (args, t) => ({
  kind: 'agent',
  title: 'Commit + open PR-A (Authentik blueprint) and get validate green',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release engineer',
      task: 'Stage ONLY the Authentik blueprint change on branch ' + BRANCH_BLUEPRINT + ', open PR-A against main, rebase, and wait for the validate check to pass. DO NOT MERGE.',
      context: { ...COMMON_CONTEXT, feedback: args.feedback },
      instructions: [
        'In the worktree, create/switch to branch ' + BRANCH_BLUEPRINT + ' from origin/main. Stage ONLY ' + REFERENCE_FILES.authentikBlueprint + ' (and, only if discovery required it, helm-values/README blueprint wiring). Do not include any servarr IngressRoute changes on this branch.',
        'Commit with a clear message referencing #176 and #185, ending with the required Co-Authored-By line for this repo.',
        'Push with --force-with-lease, open the PR via `gh pr create` with a body containing a "## Test plan" checklist (blueprint applies; outpost has all providers; newtarr+traefik still bound; apps visible in Authentik).',
        'Rebase onto origin/main, push --force-with-lease, and POLL the `validate` check until it is success or failure (gh pr checks). Report the PR number and final check state. Do NOT merge.',
      ],
      outputFormat: 'JSON: { prNumber (number), prUrl, validateState (success|failure|pending), checkSummary, commitSha }',
    },
    outputSchema: { type: 'object', required: ['prNumber', 'validateState'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 6 — MERGE PR-A + verify Authentik objects live
// ===========================================================================
export const mergeBlueprintTask = defineTask('merge-and-verify-blueprint', (args, t) => ({
  kind: 'agent',
  title: 'Merge PR-A, sync authentik, verify providers/apps/outpost live',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Deploy operator',
      task: 'Merge the approved PR-A, let ArgoCD apply the blueprint, and VERIFY via the live Authentik API that every new provider + application exists and the embedded outpost binds all of them (plus the pre-existing newtarr pk 82 and traefik). The deploy is NOT done until verified.',
      context: { ...COMMON_CONTEXT, prNumber: args.prNumber, discovery: args.discovery },
      instructions: [
        'Merge with `gh pr merge ' + '${prNumber}' + ' --merge` (per the Epaflix merge policy). authentik ArgoCD app is auto-sync+selfHeal; optionally trigger a sync to speed it up.',
        'Wait for the authentik App to be Synced/Healthy and for the Authentik WORKER to apply the blueprint (this can lag; poll). Confirm via the live API (ak-iac token) that: each of the 10 proxy providers exists, each application exists and is gated by the Servarr users group, and the embedded outpost providers list contains all 10 new pks PLUS newtarr (82) and traefik.',
        'If the worker has not applied yet after a reasonable poll, report what is missing rather than proceeding.',
        'Do NOT touch the servarr IngressRoutes in this step.',
      ],
      outputFormat: 'JSON: { merged (boolean), authentikSynced (boolean), providersLive (array), appsLive (array), outpostProvidersFinal (array), newtarrStillBound (boolean), traefikStillBound (boolean), verified (boolean), problems (array) }',
    },
    outputSchema: { type: 'object', required: ['merged', 'verified'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 7 — PREP PR-B (IngressRoutes) — no merge yet
// ===========================================================================
export const prepIngressPrTask = defineTask('prep-ingress-pr', (args, t) => ({
  kind: 'agent',
  title: 'Commit + open PR-B (IngressRoutes + kustomization) and get validate green',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release engineer',
      task: 'Stage the servarr IngressRoute + kustomization changes on branch ' + BRANCH_INGRESS + ', open PR-B, rebase, wait for validate green. DO NOT MERGE.',
      context: { ...COMMON_CONTEXT, feedback: args.feedback },
      instructions: [
        'In the worktree, switch to branch ' + BRANCH_INGRESS + ' (already created from origin/main). Stage the per-app IngressRoute files and 2-k3s/08.servarr/kustomization.yaml ONLY. Do NOT include the blueprint change (already merged in PR-A).',
        'Commit referencing #176 (note it codifies out-of-band drift + adds forward-auth), with the required Co-Authored-By line.',
        'Push --force-with-lease, open PR-B with a "## Test plan" checklist: each of the 10 hosts returns 302/307 to auth.epaflix.com for anonymous requests; jellyfin/seerr unchanged; newtarr unchanged; inter-app sync still works.',
        'Rebase onto origin/main, push --force-with-lease, POLL the validate check to success/failure. Report PR number + state. Do NOT merge.',
      ],
      outputFormat: 'JSON: { prNumber (number), prUrl, validateState (success|failure|pending), checkSummary }',
    },
    outputSchema: { type: 'object', required: ['prNumber', 'validateState'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 8 — MERGE PR-B (middleware goes live)
// ===========================================================================
export const mergeIngressTask = defineTask('merge-and-verify-ingress', (args, t) => ({
  kind: 'agent',
  title: 'Merge PR-B, sync servarr, verify routes reconciled',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Deploy operator',
      task: 'Merge approved PR-B so the forward-auth middleware goes live, let ArgoCD reconcile the servarr app, and confirm each gated route now has the middleware in the LIVE cluster.',
      context: { ...COMMON_CONTEXT, prNumber: args.prNumber },
      instructions: [
        'Merge with `gh pr merge ' + '${prNumber}' + ' --merge`. servarr ArgoCD app is auto-sync+selfHeal (prune off); optionally trigger a sync.',
        'Wait for servarr App Synced/Healthy. Then `kubectl get ingressroute -n servarr` and confirm each of the 10 <app>-https routes now references authentik-forwardauth and has a priority-15 <app>-outpost-https route. Confirm ArgoCD now OWNS these (tracking label present).',
        'Confirm jellyfin/seerr/newtarr routes are unchanged.',
      ],
      outputFormat: 'JSON: { merged (boolean), servarrSynced (boolean), routesWithMiddleware (array), outpostRoutesPresent (array), argocdOwned (boolean), problems (array) }',
    },
    outputSchema: { type: 'object', required: ['merged', 'servarrSynced'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 9 — TEST (fan-out per app)
// ===========================================================================
export const testAppTask = defineTask('test-app', (args, t) => ({
  kind: 'agent',
  title: `E2E gate test for ${args.app.name}`,
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'QA / security verification engineer',
      task: `Verify ${args.app.name} is now gated by Authentik forward-auth and that its app integration still works. Evidence-based: paste the actual command output.`,
      context: { ...COMMON_CONTEXT, app: args.app },
      instructions: [
        'UI GATE (all apps): `curl -sSI --max-time 15 https://' + args.app.host + '/` over public DNS, no cookies. PASS requires 302/307 to auth.epaflix.com (NOT a 200 from the app, NOT the app login page).',
        'OUTPOST path: `curl -sSI https://' + args.app.host + '/outpost.goauthentik.io/start` returns a redirect to Authentik (not 404).',
        'API BYPASS (ONLY if this app has apiBypass=true: sonarr, sonarr2, radarr, prowlarr, qbittorrent, bazarr): `curl -sSI https://' + args.app.host + '/api` MUST NOT redirect to auth.epaflix.com — it should reach the app (e.g. 200/401/403 from the app itself). Then confirm an UNauthenticated `curl -sS https://' + args.app.host + '/api/v3/system/status` (qbittorrent: `/api/v2/app/version`) returns the APP`s own 401/403/forbidden (proving the API key is still required) rather than an Authentik 302. This proves machines/integrations + deep-links still work over the public host without SSO.',
        'INTEGRATION SPOT-CHECK: confirm a real inter-app call still works post-gate — e.g. trigger sonarr->qbittorrent download-client "Test" via the sonarr API (X-Api-Key) and confirm success; or verify recent logs show no new 302/auth failures to a sibling public host. Report what you checked.',
        'For pure-UI apps (apiBypass=false: cleanuparr, homarr, lingarr, wizarr): the WHOLE host should gate — also confirm `/api` (if any) redirects to auth (no bypass expected).',
        'For wizarr only: app may be DOWN; PASS = the public host now returns a forward-auth redirect (no anonymous 200). Note app health separately.',
        'Report verdict per check with the literal command output.',
      ],
      outputFormat: 'JSON: { app, gated (boolean), anonStatus (string), redirectsToAuth (boolean), outpostStartOk (boolean), integrationOk (boolean), evidence (string), notes }',
    },
    outputSchema: { type: 'object', required: ['app', 'gated', 'redirectsToAuth'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// PHASE 10 — WRAP-UP (issue, PR test plans, follow-ups, docs)
// ===========================================================================
export const wrapupTask = defineTask('wrap-up', (args, t) => ({
  kind: 'agent',
  title: 'Close issue #176, tick PR test plans, open follow-ups, update docs',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Project maintainer',
      task: 'Record outcomes per the Epaflix conventions and close the loop on #176.',
      context: { ...COMMON_CONTEXT, testResults: args.testResults, prA: args.prA, prB: args.prB, design: args.design },
      instructions: [
        'Edit the PR-A and PR-B descriptions to tick the executed Test plan boxes with inline results (NEVER add a new PR comment). Strike through any step that became inapplicable, with a reason.',
        'Update 2-k3s/07.authentik-deployment/README.md (Forward Auth Applications list) and the servarr docs to list the 10 newly gated apps. Make this a tiny follow-up PR from the worktree if docs are tracked, or note it.',
        'Comment on / close issue #176 with the verified evidence (which apps are now gated, anonymous probes redirecting to auth). Only close if every app PASSED its gate test; otherwise leave open and summarize what remains.',
        'Open gh issues on ' + REPO + ' (## Finding / ## Current state / ## Desired outcome / ## Notes, cross-linked to #176/#134/#185) for any follow-ups: e.g. the now-codified-but-was-drift inventory audit, wizarr decommission, any app that could not be gated, and a check that no OTHER namespaces have similar unauth public IngressRoutes.',
        'Return the issue/PR URLs touched.',
      ],
      outputFormat: 'JSON: { issue176 (string), prAUpdated (boolean), prBUpdated (boolean), followupsOpened (array), docsUpdated (boolean) }',
    },
    outputSchema: { type: 'object', required: ['issue176', 'followupsOpened'] },
  },
  io: { inputJsonPath: `tasks/${t.effectId}/input.json`, outputJsonPath: `tasks/${t.effectId}/output.json` },
}));

// ===========================================================================
// ORCHESTRATION
// ===========================================================================
export async function process(inputs, ctx) {
  ctx.log('info', 'Issue #176 — servarr forward-auth rollout starting');

  // --- Phase 1: discovery ---
  const discovery = await ctx.task(discoverTask, {});

  // --- Phase 2: design ---
  const design = await ctx.task(designTask, { discovery });

  // Gate: review design before any edits
  {
    let fb = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const appr = await ctx.breakpoint({
        question: 'Review the rollout design (scope=10 apps, jellyfin/seerr untouched, 2-PR deploy). Approve to author manifests + blueprint?',
        title: 'Design Gate',
        options: ['Approve', 'Request changes'],
        expert: 'owner', tags: ['design-gate'],
        context: { designPath: design.designPath, summary: design.summary, deployPlan: design.deployPlan },
        previousFeedback: fb || undefined, attempt: attempt > 0 ? attempt + 1 : undefined,
      });
      if (appr.approved) break;
      fb = appr.response || appr.feedback || 'Changes requested';
      await ctx.task(designTask, { discovery, feedback: fb, attempt: attempt + 1 });
    }
  }

  // --- Phase 3: author (fan-out IngressRoutes) ---
  const ingressResults = await Promise.all(
    APPS.map((app) => ctx.task(authorIngressTask, { app, design }))
  );
  // kustomization wiring (after all files exist)
  await ctx.task(authorKustomizationTask, { ingressResults });
  // blueprint (independent of ingress files)
  const blueprint = await ctx.task(authorBlueprintTask, { discovery, design });

  // --- Phase 4: validate with refine loop ---
  {
    let fb = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const v = await ctx.task(validateTask, { feedback: fb, attempt: attempt + 1 });
      if (v.pass) break;
      fb = (v.failures || []).join('\n');
      ctx.log('warn', `Validation attempt ${attempt + 1} failed: ${fb}`);
      if (attempt === 2) {
        const cont = await ctx.breakpoint({
          question: 'Validation still failing after 3 attempts. Review failures and decide.',
          title: 'Validation Gate', options: ['Proceed anyway', 'Stop'],
          expert: 'owner', tags: ['validation-gate'], context: { failures: v.failures },
        });
        if (!cont.approved || /stop/i.test(cont.response || '')) {
          return { success: false, stage: 'validation', failures: v.failures };
        }
      }
    }
  }

  // --- Phase 5: prep PR-A (blueprint) ---
  let prA = await ctx.task(prepBlueprintPrTask, {});
  if (prA.validateState !== 'success') {
    prA = await ctx.task(prepBlueprintPrTask, { feedback: `validate=${prA.validateState}: ${prA.checkSummary || ''}` });
  }

  // Gate: approve merge of PR-A (DEPLOY — alwaysBreakOn)
  {
    const appr = await ctx.breakpoint({
      question: `DEPLOY STAGE A: merge PR-A #${prA.prNumber} (Authentik blueprint)? This creates the 10 proxy providers/apps + outpost bindings. Middleware does NOT go live yet.`,
      title: 'Deploy Gate A — Authentik blueprint',
      options: ['Merge PR-A', 'Hold'],
      expert: 'owner', tags: ['deploy', 'merge'],
      context: { prNumber: prA.prNumber, prUrl: prA.prUrl, validateState: prA.validateState },
    });
    if (!appr.approved) return { success: false, stage: 'deploy-A-held', prA: prA.prNumber };
  }
  const blueprintLive = await ctx.task(mergeBlueprintTask, { prNumber: prA.prNumber, discovery });
  if (!blueprintLive.verified) {
    const cont = await ctx.breakpoint({
      question: 'PR-A merged but Authentik objects not fully verified live. Continue to Stage B anyway?',
      title: 'Authentik Verify Gate', options: ['Continue', 'Stop'],
      expert: 'owner', tags: ['verify-gate'], context: { problems: blueprintLive.problems },
    });
    if (!cont.approved) return { success: false, stage: 'authentik-verify', detail: blueprintLive };
  }

  // --- Phase 7: prep PR-B (ingress) ---
  let prB = await ctx.task(prepIngressPrTask, {});
  if (prB.validateState !== 'success') {
    prB = await ctx.task(prepIngressPrTask, { feedback: `validate=${prB.validateState}: ${prB.checkSummary || ''}` });
  }

  // Gate: approve merge of PR-B (DEPLOY — middleware goes live)
  {
    const appr = await ctx.breakpoint({
      question: `DEPLOY STAGE B: merge PR-B #${prB.prNumber} (IngressRoutes + middleware)? After this, all 10 servarr UIs require Authentik login.`,
      title: 'Deploy Gate B — middleware live',
      options: ['Merge PR-B', 'Hold'],
      expert: 'owner', tags: ['deploy', 'merge'],
      context: { prNumber: prB.prNumber, prUrl: prB.prUrl, validateState: prB.validateState },
    });
    if (!appr.approved) return { success: false, stage: 'deploy-B-held', prA: prA.prNumber, prB: prB.prNumber };
  }
  const ingressLive = await ctx.task(mergeIngressTask, { prNumber: prB.prNumber });

  // --- Phase 9: test (fan-out per app) with one refine round ---
  let testResults = await Promise.all(APPS.map((app) => ctx.task(testAppTask, { app })));
  let failed = testResults.filter((r) => !r || !r.gated || !r.redirectsToAuth);
  if (failed.length > 0) {
    const cont = await ctx.breakpoint({
      question: `${failed.length} app(s) not gated after deploy: ${failed.map((f) => f && f.app).join(', ')}. Investigate/fix then re-test, or stop?`,
      title: 'Gate Test Failure', options: ['Fix and re-test', 'Stop'],
      expert: 'owner', tags: ['test-gate'],
      context: { failed: failed.map((f) => f && { app: f.app, anonStatus: f.anonStatus, notes: f.notes }) },
    });
    if (cont.approved && !/stop/i.test(cont.response || '')) {
      testResults = await Promise.all(APPS.map((app) => ctx.task(testAppTask, { app })));
      failed = testResults.filter((r) => !r || !r.gated || !r.redirectsToAuth);
    }
  }

  // --- Phase 10: wrap-up ---
  const wrap = await ctx.task(wrapupTask, { testResults, prA: prA.prNumber, prB: prB.prNumber, design });

  const gatedApps = testResults.filter((r) => r && r.gated && r.redirectsToAuth).map((r) => r.app);
  return {
    success: failed.length === 0,
    gatedApps,
    notGated: failed.map((f) => f && f.app),
    prA: prA.prNumber, prB: prB.prNumber,
    issue176: wrap.issue176, followups: wrap.followupsOpened,
    metadata: { processId: 'servarr-forwardauth-rollout-176', issue: 176 },
  };
}

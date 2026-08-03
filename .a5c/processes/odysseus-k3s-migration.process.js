/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Migrate the Odysseus AI workspace from the TrueNAS Custom App to the k3s cluster
 *   (GitOps/ArgoCD-managed), because Odysseus itself does NOT use the GPU — model inference is served
 *   by the existing GPU-pinned Ollama on TrueNAS (192.168.10.200:30068), which k3s nodes can reach.
 *   Also fix the web-search problem, applied to the LIVE TrueNAS instance first and then carried into
 *   the k3s deployment.
 *
 *   Owner decisions (interview, 2026-06-07):
 *     1. IMAGE: build-from-source (no upstream image). Push the pinned build to **GHCR public**
 *        (ghcr.io/SpyrosPsarras/odysseus:73673258) — cluster already pulls public GHCR images; no
 *        imagePullSecret. The image `odysseus:73673258` already exists on the TrueNAS docker engine.
 *     2. DATA: MIGRATE /mnt/pool1/odysseus/data (auth.json = owner's password, app.db = the shared
 *        Ollama endpoint + sessions/users, settings.json = disabled_tools/search config, chromadb)
 *        to the new k3s PVC. Preserve the owner's setup.
 *     3. SEARCH FIX (apply to live TrueNAS NOW, then carry into k3s): (a) enable JSON output in
 *        SearXNG (its settings only list `formats: [html]` -> JSON API 403; Odysseus falls back to
 *        HTML scraping, which works but is the wrong path), and (b) pare Odysseus's toolset via
 *        data/settings.json `disabled_tools` — disable the code/file/shell tools (bash, python,
 *        write_file, edit_file, read_file, grep, glob, ls, serve_model, etc.), leaving web_search +
 *        web_fetch (+ chat). Root cause observed in logs: a local 7B handed 25 tools emits spurious
 *        fenced tool-blocks (hallucinated `bash: codex > /plugins`) and the agent loop never surfaces
 *        the answer. Fewer tools => it answers; also closes the RCE surface (#186).
 *
 *   Target shape on k3s: namespace `odysseus`; Deployments+Services for odysseus (:7000) + searxng +
 *   chromadb + ntfy mirroring the compose service DNS; local-path PVC(s) for odysseus data + chromadb;
 *   env via ConfigMap + a SOPS *.enc.yaml Secret (ODYSSEUS_ADMIN_PASSWORD, SEARXNG_SECRET, …);
 *   OLLAMA_BASE_URL=http://192.168.10.200:30068/v1; APP_BIND=0.0.0.0; AUTH_ENABLED=true. NO GPU.
 *   Re-point the existing Traefik route (2-k3s/05.traefik-deployment/ingress/odysseus-proxy.yaml) from
 *   the out-of-band Endpoints->192.168.10.200:30070 to a normal IngressRoute -> the in-cluster odysseus
 *   Service, keeping the authentik-forwardauth middleware + outpost route. This RESOLVES #184 (no more
 *   out-of-band Endpoints). Authentik provider pk83 / app slug `odysseus` / group bindings ("Odysseus
 *   users" + "Servarr users") are UNCHANGED (external_host https://odysseus.epaflix.com is the same).
 *   Add an ArgoCD Application for odysseus (manual-sync first per the repo adoption pattern). After k3s
 *   is verified, decommission the TrueNAS Custom App but KEEP /mnt/pool1/odysseus as a backup until soak.
 *
 * @inputs { repoRoot, repo, masterSsh, truenasSsh, truenasIp, ollamaUrl, ghcrImage, pinnedSha,
 *           dataPath, host, slug, authentikBase, embeddedOutpostPk, middlewareName, middlewareNs,
 *           ns, ingressFile, branch }
 * @outputs { success, searchFixedTruenas, imagePushed, deployedK3s, dataMigrated, ssoOk, searchOkK3s,
 *            decommissioned, prUrl, merged, followUpIssues }
 *
 * Breakpoints (low tolerance / alwaysBreakOn deploy+destructive+architecture+secrets): live search-fix
 * on TrueNAS, plan/architecture gate (authorizes GHCR push + k3s deploy + cutover + decommission), the
 * k3s cutover deploy gate, and the TrueNAS decommission gate. Plus conditional verify/anomaly gates.
 *
 * @agent general-purpose (ssh / docker / kubectl-over-ssh / GHCR / SOPS / git / gh / Authentik-API / curl executor + verifier)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

const SECRETS_HINT =
  'Real secret VALUES live in .github/instructions/secrets.yml (git-ignored, pre-commit guarded). ' +
  'NEVER print/echo/commit secrets. Repo files use placeholders; k3s Secrets are SOPS *.enc.yaml (age, ' +
  'single cluster recipient) per .github/instructions/sops.instructions.md. TrueNAS on-host env stays ' +
  'on TrueNAS. truenas_admin has NO passwordless sudo: pipe the password from secrets.yml on stdin ' +
  '(`pw=$(grep ^truenas_admin_password: .github/instructions/secrets.yml|head -1|cut -d\\" -f2)`; ' +
  '`echo "$pw" | sudo -S <cmd>`), and NEVER print it.';

// PHASE 1 — analyze current + target state (read-only).
const analyzeTask = defineTask('analyze', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyze the live Odysseus/TrueNAS state, GHCR push capability, and the k3s target patterns',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer planning a TrueNAS->k3s app migration on the Epaflix cluster',
      task: 'Gather exact facts to migrate Odysseus to k3s (GitOps), migrate its data, fix search, and re-point SSO. DO NOT mutate anything.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'TrueNAS (read-only over SSH, ' + SECRETS_HINT + '): confirm the running app `ix-odysseus-*` containers + the live compose at ' + args.dataPath + '/../docker-compose.yaml (env, ports, the 4 services). Record sizes to migrate: `du -sh ' + args.dataPath + '/app.db ' + args.dataPath + '/chroma ' + args.dataPath + '` and confirm auth.json + settings.json exist. Confirm the image `odysseus:' + args.pinnedSha + '` exists (`docker images odysseus:' + args.pinnedSha + '`).',
        'SEARCH FIX TARGETS: read the SearXNG settings on the dataset (likely ' + args.dataPath + '/../config/searxng/settings.yml) — record the current `formats:` block (expect html-only). Read ' + args.dataPath + '/settings.json — record the current `disabled_tools` (if any) and the full tool list Odysseus offers (from the logs/source the agent sends ~25 tools incl bash, python, web_search, web_fetch, write_file, edit_file, read_file, grep, glob, ls, serve_model, manage_skills, api_call). Identify the EXACT mechanism to enable SearXNG JSON (add json to formats) and to set disabled_tools.',
        'GHCR: confirm push capability — `gh auth status` and whether the gh token (or a PAT) has `write:packages`. Determine the GHCR namespace (ghcr.io/SpyrosPsarras/odysseus). Do NOT push yet. Note how to make the package public after push.',
        'K3S target (read-only, kubectl over `' + args.masterSsh + ' \'<kubectl ...>\'`): how are app namespaces + ArgoCD Applications structured (app-of-apps under 2-k3s/11.argocd/apps, the servarr-style Deployment/Service/PVC + local-path pattern). Confirm a k3s node reaches the Ollama (`' + args.masterSsh + ' \'curl -sf http://' + args.truenasIp + ':30068/api/tags\'`). Read the CURRENT ' + args.ingressFile + ' (the out-of-band Endpoints+Service+IngressRoutes to ' + args.truenasIp + ':30070 with the ' + args.middlewareName + ' middleware + outpost route) — this is what we re-point to an in-cluster Service. Note #184 (Endpoints excluded by ArgoCD) is RESOLVED by moving to a real Service.',
        'AUTHENTIK (read-only, API at ' + args.authentikBase + ', token in secrets.yml key authentik_admin_api_token; ' + SECRETS_HINT + '): confirm provider pk83 / app slug ' + args.slug + ' / external_host https://' + args.host + ' are unchanged and need NO change for the move (only the k3s backend changes, not the public host).',
        'Return ONLY the structured JSON state + gaps.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['truenas', 'searchFix', 'ghcr', 'k3s', 'authentik', 'gaps', 'summary'],
      properties: {
        truenas: { type: 'object' }, searchFix: { type: 'object' }, ghcr: { type: 'object' },
        k3s: { type: 'object' }, authentik: { type: 'object' },
        gaps: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 2 — apply the search fix to the LIVE TrueNAS instance (gated before).
const searchFixTask = defineTask('search-fix-truenas', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Fix Odysseus web search on the live TrueNAS instance (SearXNG JSON + pare toolset)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE applying a config fix to the live Odysseus TrueNAS app',
      task: 'Enable SearXNG JSON output and disable the risky/extra Odysseus tools so web search returns a clean answer, on the LIVE TrueNAS app. Then verify.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        SECRETS_HINT,
        'SEARXNG: edit the dataset SearXNG settings (the path from analysis) so `formats:` includes BOTH `html` and `json` (and `csv`/`rss` optional). Restart the searxng container (`docker restart ix-odysseus-searxng-1`). Verify the JSON API now works from inside odysseus: `docker exec ix-odysseus-odysseus-1 sh -c \'curl -sf "http://searxng:8080/search?q=test&format=json" >/dev/null && echo JSON_OK\'` (no more 403).',
        'TOOLSET: edit ' + args.dataPath + '/settings.json to set `disabled_tools` to the code/file/shell tools — at minimum: bash, python, write_file, edit_file, read_file, grep, glob, ls, serve_model, adopt_served_model — KEEPING web_search and web_fetch enabled (and chat). Use the exact tool names + schema confirmed in analysis. Restart odysseus (`docker restart ix-odysseus-odysseus-1`) so it reloads settings.',
        'VERIFY: containers healthy; then prove a search answer surfaces — POST a chat completion through Odysseus is hard without a session, so instead confirm in the odysseus logs after a manual UI test is NOT required; minimally confirm the JSON search path works and the bash tool is gone from the offered set (grep the next agent-debug log line for tool_names NOT containing bash/python). Report what you changed and the verification.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['searxngJsonEnabled', 'disabledTools', 'containersHealthy', 'verified', 'anomalies', 'summary'],
      properties: {
        searxngJsonEnabled: { type: 'boolean' }, disabledTools: { type: 'array', items: { type: 'string' } },
        containersHealthy: { type: 'boolean' }, verified: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 3 — plan the k3s migration (no mutation).
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the concrete Odysseus->k3s migration plan',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC/platform engineer planning a GitOps app migration',
      task: 'Turn the analysis into a concrete, ordered, reversible plan to run Odysseus on k3s (GitOps), migrate its data, re-point SSO, and decommission the TrueNAS app. Plan only.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'IMAGE: exact steps to tag `odysseus:' + args.pinnedSha + '` -> ' + args.ghcrImage + ', `docker login ghcr.io` (token from secrets.yml / gh), push, and make the package public. ' + SECRETS_HINT,
        'K3S MANIFESTS: full set for namespace `' + args.ns + '` — Deployments+Services for odysseus (:7000, image ' + args.ghcrImage + '), searxng, chromadb, ntfy (mirror the compose service DNS names so intra-namespace URLs like http://searxng:8080, chromadb:8000 work); local-path PVC(s) for odysseus data + chromadb; ConfigMap for non-secret env + a SOPS *.enc.yaml Secret for ODYSSEUS_ADMIN_PASSWORD/SEARXNG_SECRET/etc; env OLLAMA_BASE_URL=' + args.ollamaUrl + ', APP_BIND=0.0.0.0, AUTH_ENABLED=true, SECURE_COOKIES=true, ALLOWED_ORIGINS=https://' + args.host + '; carry the SEARCH FIX in (searxng settings with json format via ConfigMap; disabled_tools baked into the migrated settings.json). NO GPU.',
        'ROUTE CUTOVER: rewrite ' + args.ingressFile + ' from the out-of-band Endpoints+headless Service->' + args.truenasIp + ':30070 to a normal IngressRoute -> the in-cluster odysseus Service in `' + args.ns + '` (keep the ' + args.middlewareName + ' middleware priority:10 + the outpost route priority:15). This resolves #184. Authentik provider pk83/app/groups UNCHANGED.',
        'ARGOCD: add an Application for odysseus (manual-sync first per the repo adoption pattern; selfHeal flip deferred to a follow-up). Respect the adoption-order rule: aligned git pushed/merged BEFORE the Application auto-syncs.',
        'DATA MIGRATION: a concrete, safe method to copy ' + args.dataPath + ' (app.db, auth.json, settings.json incl disabled_tools, chroma/) from TrueNAS to the k3s odysseus PVC — e.g. tar over ssh into a one-shot migration Job/initContainer that lands it on the local-path PVC before odysseus starts; chromadb data into its PVC. Preserve ownership (PUID/PGID 1000). Plan the order so odysseus on k3s starts with the migrated data (so the owner password + Ollama endpoint + settings carry over).',
        'CUTOVER ORDER + ROLLBACK: image push -> author+validate+merge k3s manifests (ArgoCD app created, manual sync) -> migrate data -> bring up k3s odysseus -> verify (no GPU, Ollama reachable, UI, SSO redirect, search, login with existing password) -> re-point Traefik route to k3s -> re-verify -> ONLY THEN decommission the TrueNAS app (keep /mnt/pool1/odysseus as backup). Rollback per surface (revert route to TrueNAS Endpoints; keep TrueNAS app until soak; delete GHCR tag).',
        'TEST PLAN (array). RISKS (array). ROLLBACK (object). If prior feedback is in context, incorporate it. Return ONLY the structured JSON plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['imageSteps', 'manifests', 'routeCutover', 'argocd', 'dataMigration', 'order', 'testPlan', 'risks', 'rollback', 'summary'],
      properties: {
        imageSteps: { type: 'array', items: { type: 'object' } }, manifests: { type: 'array', items: { type: 'object' } },
        routeCutover: { type: 'object' }, argocd: { type: 'object' }, dataMigration: { type: 'object' },
        order: { type: 'array', items: { type: 'string' } }, testPlan: { type: 'array', items: { type: 'string' } },
        risks: { type: 'array', items: { type: 'string' } }, rollback: { type: 'object' }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 4 — build + push image to GHCR (live, low-risk, gated by the approved plan).
const buildPushTask = defineTask('build-push-ghcr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Tag + push odysseus:' + args.pinnedSha + ' to GHCR (public)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer publishing a container image to GHCR',
      task: 'Push the pinned Odysseus image to ' + args.ghcrImage + ' and make it pullable by the k3s nodes.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        SECRETS_HINT,
        'On TrueNAS (the image lives there): `docker tag odysseus:' + args.pinnedSha + ' ' + args.ghcrImage + '`. `docker login ghcr.io` with the GHCR token/PAT (write:packages) from secrets.yml or gh — NEVER print it. `docker push ' + args.ghcrImage + '`.',
        'Make the GHCR package PUBLIC (via gh api / the packages UI) so k3s pulls without a secret. Verify it is pullable: `' + args.masterSsh + ' \'sudo k3s ctr images pull ' + args.ghcrImage + '\'` (or a kubectl test pod) returns success.',
        'Return ONLY the structured JSON result (pushed, digest, public, pullableFromK3s).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['pushed', 'image', 'public', 'pullableFromK3s', 'anomalies', 'summary'],
      properties: {
        pushed: { type: 'boolean' }, image: { type: 'string' }, public: { type: 'boolean' },
        pullableFromK3s: { type: 'boolean' }, anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 5 — author k3s manifests on a branch (no push, no live deploy).
const authorTask = defineTask('author-manifests', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the k3s Odysseus manifests + route cutover + ArgoCD app on a branch',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer authoring Kustomize/ArgoCD manifests under the Epaflix policy',
      task: 'Create the k3s Odysseus manifests + re-point the Traefik route + add the ArgoCD app per the approved plan, on a fresh branch, with a path-scoped local commit. No push, no live deploy.',
      context: { ...args, approvedPlan: args.plan, secretsHint: SECRETS_HINT },
      instructions: [
        'From repoRoot=' + args.repoRoot + ', `git fetch origin && git switch -c ' + args.branch + ' origin/main`.',
        'Author the namespace ' + args.ns + ' Deployments/Services/PVCs/ConfigMap + a SOPS *.enc.yaml Secret (encrypt per .github/instructions/sops.instructions.md — placeholders never plaintext), the data-migration Job/initContainer, the ArgoCD Application (manual sync), and rewrite ' + args.ingressFile + ' to the in-cluster Service (keep middleware + outpost route). Bake the search fix in (searxng json formats ConfigMap + disabled_tools in the migrated settings). Use image ' + args.ghcrImage + '. Mirror the servarr local-path PVC + Deployment conventions.',
        'Validate: `kustomize build` every touched kustomize dir (or kubectl kustomize); YAML-parse; confirm the SOPS pre-commit guard passes (no plaintext kind: Secret).',
        'Commit ONLY the specific changed paths (do NOT add .a5c/ or process scaffolding). Conventional subject referencing the migration; end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Do NOT push.',
        'Return ONLY the structured JSON (branch, commitSha, filesChanged, validated, noPlaintextSecrets, summary).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'filesChanged', 'validated', 'noPlaintextSecrets', 'summary'],
      properties: {
        branch: { type: 'string' }, commitSha: { type: 'string' }, filesChanged: { type: 'array', items: { type: 'string' } },
        validated: { type: 'boolean' }, noPlaintextSecrets: { type: 'boolean' }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 6 — deploy to k3s + migrate data + cut over the route (live, gated).
const deployMigrateTask = defineTask('deploy-migrate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Merge GitOps, deploy Odysseus on k3s, migrate data, cut over the route',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer executing the k3s cutover under the Epaflix merge policy',
      task: 'Push+PR+rebase+merge the k3s manifests, deploy Odysseus on k3s, migrate the TrueNAS data, and cut the Traefik route to the in-cluster Service — per the approved plan and order.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'GITOPS: `git push -u origin ' + args.branch + '`; open PR vs ' + args.repo + ' base main with a `## Test plan` (the approved testPlan, unchecked). Rebase onto origin/main, `push --force-with-lease`, wait for `validate` (`gh pr checks --watch`; on the known unpinned-kustomize CI flake, `gh run rerun --failed`). `gh pr merge <n> --merge`. Confirm the Merge marker.',
        'DEPLOY + MIGRATE: per the plan order — ArgoCD sync the odysseus Application (manual sync); run the data-migration (tar ' + args.dataPath + ' from TrueNAS into the k3s odysseus PVC; chroma into its PVC; preserve 1000:1000) BEFORE odysseus serves traffic; bring up odysseus + searxng + chromadb + ntfy. ' + SECRETS_HINT,
        'CUTOVER: ensure the re-pointed ' + args.ingressFile + ' is live (ArgoCD applied) so https://' + args.host + ' now routes to the in-cluster Service (Authentik objects unchanged). Keep the TrueNAS app RUNNING as fallback (do NOT decommission here).',
        'Report pod status, migration result, route state. Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'prUrl', 'podsUp', 'dataMigrated', 'routeCutOver', 'anomalies', 'summary'],
      properties: {
        merged: { type: 'boolean' }, prUrl: { type: 'string' }, podsUp: { type: 'array', items: { type: 'string' } },
        dataMigrated: { type: 'boolean' }, routeCutOver: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 6b — bump k3s worker VM CPU baseline (kvm64 lacks SSE4.2/POPCNT the image needs).
const cpuBumpTask = defineTask('cpu-bump-workers', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Bump k3s worker VM CPU type to ' + args.cpuType + ' (one node at a time, drained)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Proxmox/k3s SRE changing worker VM CPU baseline safely',
      task: 'Set cpu=' + args.cpuType + ' on k3s worker VMs ' + JSON.stringify(args.workers) + ' ONE AT A TIME with cordon+drain so the Odysseus image (needs SSE4.2/POPCNT) can run. Keep the cluster healthy throughout.',
      context: { ...args },
      instructions: [
        'Proxmox hosts: takaros root@192.168.10.10 (VMIDs 1061,1062), evanthoulaki root@192.168.10.11 (VMIDs 1063,1065). k3s: ' + args.masterSsh + '. Node k3s-worker-6N = VMID 106N = IP 192.168.10.6N (61/62 on takaros, 63/65 on evanthoulaki).',
        'For EACH worker, strictly one at a time (do NOT start the next until the previous is back Ready+uncordoned): (1) `' + args.masterSsh + ' \'kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=180s\'`; (2) on its Proxmox host: `qm stop <vmid>` (wait fully stopped), `qm set <vmid> --cpu ' + args.cpuType + '`, `qm start <vmid>`; (3) wait the node rejoins Ready (`' + args.masterSsh + ' \'kubectl get node <node>\'` Ready); `kubectl uncordon <node>`; (4) verify the bump: `ssh ubuntu@192.168.10.6N \'grep -m1 -o sse4_2 /proc/cpuinfo && grep -m1 -o popcnt /proc/cpuinfo\'` returns both.',
        'After all 4: confirm all nodes Ready; confirm the previously-CrashLoopBackOff odysseus/searxng/chromadb pods no longer fault on the NumPy illegal-instruction (the CPU fault is gone — searxng may still need the enableServiceLinks fix, handled in the next task). If a node fails to rejoin, STOP and report (do NOT drain further nodes).',
        'Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['workersBumped', 'allNodesReady', 'cpuFaultGone', 'anomalies', 'summary'],
      properties: {
        workersBumped: { type: 'array', items: { type: 'string' } }, allNodesReady: { type: 'boolean' },
        cpuFaultGone: { type: 'boolean' }, anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 6c — complete the k3s cutover after the bump (fix searxng, re-point route, bring pods healthy).
const recutoverTask = defineTask('recutover-k3s', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Fix searxng + re-point route to k3s + bring Odysseus healthy on k3s',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer completing the k3s cutover after the CPU bump',
      task: 'Workers now run ' + args.cpuType + '. Complete the switch to k3s: fix the searxng service-link bug, re-point the Traefik route to the in-cluster Service, bring all Odysseus pods healthy.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'On a fresh branch off origin/main: (a) in 2-k3s/13.odysseus/searxng.yaml add `enableServiceLinks: false` to the pod spec (k8s injects SEARXNG_PORT=tcp://... which granian mis-parses as --port → crash). (b) re-point ' + args.ingressFile + ' BACK to the in-cluster Service (odysseus.' + args.ns + ':7000) — re-apply the k3s route that rollback PR #206 reverted; keep authentik-forwardauth priority:10 + outpost priority:15 + http->https redirect. Validate (kustomize/yaml). Path-scoped commit (Co-Authored-By trailer). ' + SECRETS_HINT,
        'Push + PR vs ' + args.repo + ' base main with a `## Test plan`; rebase onto origin/main; `push --force-with-lease`; wait `validate` (rerun the known kustomize flake if needed); `gh pr merge --merge`.',
        'Sync ArgoCD (odysseus app manual + the traefik app) so the route + searxng fix go live. If pods need rescheduling onto bumped nodes: `' + args.masterSsh + ' \'kubectl -n ' + args.ns + ' rollout restart deploy odysseus searxng chromadb ntfy\'`. Wait all Ready 1/1.',
        'Verify: odysseus reaches Ollama from the pod (`curl -sf ' + args.ollamaUrl + '/models`); the migrated auth.json/app.db loaded (NO fresh admin created; the Ollama endpoint row present); searxng JSON API returns 200 in-cluster.',
        'Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'prUrl', 'podsHealthy', 'routeOnK3s', 'ollamaReachable', 'anomalies', 'summary'],
      properties: {
        merged: { type: 'boolean' }, prUrl: { type: 'string' }, podsHealthy: { type: 'boolean' },
        routeOnK3s: { type: 'boolean' }, ollamaReachable: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 7 — verify the k3s deploy end-to-end (read-only).
const verifyTask = defineTask('verify-k3s', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify Odysseus on k3s: pod, Ollama reachability, SSO, search, migrated data',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the k3s Odysseus deployment against live state',
      task: 'Prove Odysseus runs correctly on k3s (no GPU), reaches Ollama, is gated by Authentik SSO, search works, and the migrated data is intact. Read-only.',
      context: { ...args },
      instructions: [
        'kubectl over `' + args.masterSsh + ' \'<...>\'`, namespace ' + args.ns + '. Pods Running/Ready (odysseus, searxng, chromadb, ntfy); no GPU requested. odysseus reaches Ollama: exec `curl -sf ' + args.ollamaUrl + '/models`.',
        'SSO: `curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\\n" https://' + args.host + '/` -> 302 to Authentik (not a 200 page); outpost path handled.',
        'DATA MIGRATED: confirm the migrated app.db/auth.json/settings.json are present in the odysseus PVC (the Ollama endpoint row exists; disabled_tools set; auth.json present so the owner password still works). Confirm searxng JSON works (no 403) and the offered tool set excludes bash/python (search fix carried over).',
        'If attempt>1 this is a re-verify. Return ONLY the structured JSON verdict with anomalies.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'podsReady', 'ollamaReachable', 'ssoOk', 'searchOk', 'dataIntact', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' }, podsReady: { type: 'boolean' }, ollamaReachable: { type: 'boolean' },
        ssoOk: { type: 'boolean' }, searchOk: { type: 'boolean' }, dataIntact: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 8 — decommission the TrueNAS Custom App (destructive, gated). Keep the dataset as backup.
const decommissionTask = defineTask('decommission-truenas', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Decommission the TrueNAS Odysseus Custom App (keep the dataset as backup)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE decommissioning the superseded TrueNAS Custom App',
      task: 'Stop/remove the TrueNAS Odysseus Custom App now that k3s is verified, but KEEP /mnt/pool1/odysseus data as a backup.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        SECRETS_HINT,
        'Stop + delete the TrueNAS `odysseus` Custom App (`midclt call app.stop` then `app.delete`, or the documented equivalent). Confirm the ix-odysseus-* containers are gone.',
        'DO NOT delete /mnt/pool1/odysseus — it is the data backup until soak. Confirm the dataset still exists with app.db/auth.json/chroma intact.',
        'Re-confirm https://' + args.host + ' still serves (now via k3s) AFTER the TrueNAS app is gone (proves no residual dependency). Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['appRemoved', 'datasetKept', 'hostStillServes', 'anomalies', 'summary'],
      properties: {
        appRemoved: { type: 'boolean' }, datasetKept: { type: 'boolean' }, hostStillServes: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 9 — closeout.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'PR test plan, follow-ups, history log, close #184',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Maintainer closing out the Odysseus k3s migration per Epaflix conventions',
      task: 'Record verification on the PR, open/close follow-up issues, and log the run.',
      context: { ...args },
      instructions: [
        'EXECUTE PR TEST PLAN: tick each box by EDITING the PR body (gh pr edit) with real outcomes — NEVER a new comment. Strike-through N/A.',
        'ISSUES: #184 (out-of-band Endpoints) is RESOLVED by the real Service — close it with a note. Open `gh issue`s (## Finding/## Current state/## Desired outcome/## Notes; cross-link) for genuine follow-ups: ArgoCD selfHeal flip for the odysseus app after soak; GHCR image-update tracking (pinned SHA, no auto-update); delete /mnt/pool1/odysseus backup after soak; revisit #186 (shell tool now disabled via disabled_tools — confirm/close or keep). ',
        'Log a short .history summary (do NOT git add .history — it is git-ignored).',
        'Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['testPlanRecorded', 'issuesClosed', 'followUpIssues', 'historyLogged', 'summary'],
      properties: {
        testPlanRecorded: { type: 'boolean' }, issuesClosed: { type: 'array', items: { type: 'string' } },
        followUpIssues: { type: 'array', items: { type: 'string' } }, historyLogged: { type: 'boolean' }, summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: 'SpyrosPsarras/epaflix',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    truenasSsh: 'ssh truenas_admin@192.168.10.200',
    truenasIp: '192.168.10.200',
    ollamaUrl: 'http://192.168.10.200:30068/v1',
    ghcrImage: 'ghcr.io/SpyrosPsarras/odysseus:73673258',
    pinnedSha: '73673258',
    dataPath: '/mnt/pool1/odysseus/data',
    host: 'odysseus.epaflix.com',
    slug: 'odysseus',
    authentikBase: 'https://auth.epaflix.com',
    embeddedOutpostPk: '209f71f9-95f8-4264-91c2-4b065bbd6b07',
    middlewareName: 'authentik-forwardauth',
    middlewareNs: 'traefik-system',
    ns: 'odysseus',
    ingressFile: '2-k3s/05.traefik-deployment/ingress/odysseus-proxy.yaml',
    branch: 'odysseus-k3s-migration',
    cpuType: 'host',
    workers: ['k3s-worker-61', 'k3s-worker-62', 'k3s-worker-63', 'k3s-worker-65'],
    ...inputs,
  };

  ctx.log('info', 'Odysseus k3s migration: analyze -> search-fix[BP] -> plan[BP] -> build/push -> author -> deploy/migrate[BP] -> verify -> decommission[BP] -> closeout');

  // PHASE 1 — analyze.
  const state = await ctx.task(analyzeTask, { ...cfg });
  ctx.log('info', `Analyze: ${state.summary}`);

  // GATE — apply the live search fix on TrueNAS (owner asked for it now).
  const sfGate = await ctx.breakpoint({
    question:
      'Apply the SEARCH FIX to the LIVE TrueNAS Odysseus now? Enables SearXNG JSON output (kills the 403) ' +
      'and disables the code/file/shell tools via settings.json (keeps web_search + web_fetch) so the model ' +
      'stops derailing and the answer surfaces — also closes the shell-tool RCE surface (#186). Restarts the ' +
      'searxng + odysseus containers. Proceed?',
    options: ['Apply search fix', 'Skip (do it only in k3s)'],
    expert: 'owner', tags: ['deploy', 'approval-gate'],
  });
  let searchFix = null;
  if (sfGate.approved && !(sfGate.response || '').toLowerCase().match(/skip/)) {
    searchFix = await ctx.task(searchFixTask, { ...cfg, state });
    ctx.log('info', `Search fix: json=${searchFix.searxngJsonEnabled} disabled=${JSON.stringify(searchFix.disabledTools)} verified=${searchFix.verified}`);
  }

  // PHASE 3 — plan with refine/architecture gate.
  let plan, lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    plan = await ctx.task(planTask, { ...cfg, state, searchFix, feedback: lastFeedback || undefined, attempt: attempt > 0 ? attempt + 1 : undefined });
    const gate = await ctx.breakpoint({
      question:
        'Review the PLAN to migrate Odysseus to k3s.\n\n' +
        'Image: ' + JSON.stringify(plan.imageSteps) + '\n' +
        'Manifests: ' + JSON.stringify(plan.manifests) + '\n' +
        'Route cutover: ' + JSON.stringify(plan.routeCutover) + '\n' +
        'ArgoCD: ' + JSON.stringify(plan.argocd) + '\n' +
        'Data migration: ' + JSON.stringify(plan.dataMigration) + '\n' +
        'Order: ' + JSON.stringify(plan.order) + '\n' +
        'Test plan: ' + JSON.stringify(plan.testPlan) + '\n' +
        'Risks: ' + JSON.stringify(plan.risks) + '\nRollback: ' + JSON.stringify(plan.rollback) + '\n\n' +
        'Summary: ' + plan.summary + '\n\n' +
        'Approving authorizes the later live mutations: GHCR push, GitOps merge, k3s deploy, data ' +
        'migration, route cutover, and (gated again) TrueNAS decommission. Approve?',
      options: ['Approve plan', 'Request changes', 'Abort'],
      expert: 'owner', tags: ['plan-gate', 'architecture-change', 'approval-gate'],
      previousFeedback: lastFeedback || undefined, attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (gate.approved && !(gate.response || '').toLowerCase().includes('change')) break;
    if (!gate.approved && (gate.response || '').toLowerCase().includes('abort')) {
      return { success: false, reason: 'plan-aborted', searchFixedTruenas: !!(searchFix && searchFix.verified), state, plan };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
  }

  // PHASE 4 — build + push image (live, low-risk, authorized by the plan gate).
  const img = await ctx.task(buildPushTask, { ...cfg, plan });
  ctx.log('info', `Image: pushed=${img.pushed} public=${img.public} pullable=${img.pullableFromK3s}`);
  if (!img.pushed || !img.pullableFromK3s) {
    const g = await ctx.breakpoint({
      question: 'GHCR image not confirmed pullable from k3s.\n' + JSON.stringify(img.anomalies) + '\n' + img.summary + '\nProceed anyway or stop?',
      options: ['Continue', 'Stop here'], expert: 'owner', tags: ['anomaly-gate'],
    });
    if (!g.approved || (g.response || '').toLowerCase().includes('stop')) {
      return { success: false, reason: 'image-push-failed', img, searchFixedTruenas: !!(searchFix && searchFix.verified) };
    }
  }

  // PHASE 5 — author manifests on a branch.
  const change = await ctx.task(authorTask, { ...cfg, plan, state });
  ctx.log('info', `Authored: branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.filesChanged)} validated=${change.validated}`);

  // GATE — k3s cutover deploy (merge + deploy + migrate + route cutover).
  const deployGate = await ctx.breakpoint({
    question:
      'Approve the k3s CUTOVER? Push branch `' + change.branch + '`, open+rebase+merge the PR (ArgoCD creates ' +
      'the odysseus app), MIGRATE the TrueNAS data into the k3s PVC, bring up Odysseus on k3s, and re-point ' +
      'the Traefik route for ' + cfg.host + ' to the in-cluster Service (Authentik unchanged). The TrueNAS app ' +
      'stays running as fallback (decommission is a separate later gate). Files: ' + JSON.stringify(change.filesChanged) + '. Proceed?',
    options: ['Approve cutover', 'Stop here'], expert: 'owner', tags: ['deploy', 'architecture-change', 'secrets', 'approval-gate'],
  });
  if (!deployGate.approved || (deployGate.response || '').toLowerCase().match(/stop|abort/)) {
    return { success: false, deployedK3s: false, reason: 'cutover-not-approved', branch: change.branch, img, searchFixedTruenas: !!(searchFix && searchFix.verified) };
  }

  // PHASE 6 — deploy + migrate + cut over.
  const dep = await ctx.task(deployMigrateTask, { ...cfg, branch: change.branch, plan, state });
  ctx.log('info', `Deploy/migrate: merged=${dep.merged} pods=${JSON.stringify(dep.podsUp)} migrated=${dep.dataMigrated} route=${dep.routeCutOver}`);
  if (!dep.merged) {
    return { success: false, deployedK3s: false, reason: 'cutover-merge-failed', dep, branch: change.branch };
  }

  // PHASE 6b — CPU-baseline blocker (discovered at deploy): the GHCR image needs SSE4.2/POPCNT
  // (x86-64-v2) but the worker VMs are kvm64 -> crashloop. Owner decision (2026-06-07): bump all 4
  // workers to cpu=host (both Proxmox hosts are identical E5-2623 v4, migration-safe), then re-cutover.
  const cpuGate = await ctx.breakpoint({
    question:
      'The k3s deploy crashlooped: the Odysseus image needs SSE4.2/POPCNT but the worker VMs are kvm64. ' +
      'Bump worker VMs ' + JSON.stringify(cfg.workers) + ' to cpu=' + cfg.cpuType + ' (both Proxmox hosts are ' +
      'identical E5-2623 v4, so host is migration-safe), ONE AT A TIME with cordon+drain, then re-point the ' +
      'route to k3s and bring Odysseus up? Each worker VM is drained then restarted.',
    options: ['Approve CPU bump + retry', 'Stop here'],
    expert: 'owner', tags: ['deploy', 'destructive', 'approval-gate'],
  });
  if (!cpuGate.approved) {
    return { success: false, deployedK3s: true, decommissioned: false, reason: 'cpu-bump-declined', dep, prUrl: dep.prUrl };
  }
  const cpu = await ctx.task(cpuBumpTask, { ...cfg });
  ctx.log('info', `CPU bump: bumped=${JSON.stringify(cpu.workersBumped)} ready=${cpu.allNodesReady} faultGone=${cpu.cpuFaultGone}`);
  const recut = await ctx.task(recutoverTask, { ...cfg });
  ctx.log('info', `Re-cutover: merged=${recut.merged} podsHealthy=${recut.podsHealthy} routeK3s=${recut.routeOnK3s} ollama=${recut.ollamaReachable}`);

  // PHASE 7 — verify, with anomaly/re-verify gate.
  let v = await ctx.task(verifyTask, { ...cfg });
  if (!v.verified) {
    const recover = await ctx.breakpoint({
      question:
        'k3s verification found issues.\npodsReady=' + v.podsReady + ' ollama=' + v.ollamaReachable + ' sso=' + v.ssoOk +
        ' search=' + v.searchOk + ' data=' + v.dataIntact + '\nanomalies: ' + JSON.stringify(v.anomalies) + '\n' + v.summary +
        '\n\nThe TrueNAS app is still up as fallback. How to proceed?',
      options: ['Re-verify (transient)', 'Roll back route to TrueNAS (stop)', 'Continue to decommission (accept)'],
      expert: 'owner', tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      v = await ctx.task(verifyTask, { ...cfg, attempt: 2 });
    } else if (r.includes('roll back') || r.includes('stop') || !recover.approved) {
      return { success: false, deployedK3s: true, decommissioned: false, reason: 'verify-stop-fallback-truenas', verify: v, prUrl: dep.prUrl };
    }
  }

  // GATE — decommission the TrueNAS app (destructive).
  const decGate = await ctx.breakpoint({
    question:
      'k3s Odysseus is verified. Decommission the TrueNAS Custom App now? Stops + deletes the `odysseus` ' +
      'TrueNAS app (ix-odysseus-* containers). KEEPS /mnt/pool1/odysseus as a data backup (not deleted). ' +
      'Then re-confirms ' + cfg.host + ' still serves via k3s. Proceed?',
    options: ['Approve decommission', 'Keep TrueNAS app for now (skip)'],
    expert: 'owner', tags: ['destructive', 'approval-gate'],
  });
  let dec = null;
  if (decGate.approved) {
    dec = await ctx.task(decommissionTask, { ...cfg });
    ctx.log('info', `Decommission: removed=${dec.appRemoved} backupKept=${dec.datasetKept} stillServes=${dec.hostStillServes}`);
  }

  // PHASE 9 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, host: cfg.host, prUrl: dep.prUrl, verify: v, decommission: dec, searchFix,
  });
  ctx.log('info', `Closeout: closed=${JSON.stringify(close.issuesClosed)} follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    searchFixedTruenas: !!(searchFix && searchFix.verified),
    imagePushed: img.pushed,
    deployedK3s: true,
    dataMigrated: dep.dataMigrated,
    ssoOk: v.ssoOk,
    searchOkK3s: v.searchOk,
    decommissioned: !!(dec && dec.appRemoved),
    prUrl: dep.prUrl,
    merged: dep.merged,
    followUpIssues: close.followUpIssues,
  };
}

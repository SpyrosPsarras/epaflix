/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Deploy the Odysseus self-hosted AI workspace (github.com/pewdiepie-archdaemon/odysseus)
 *   on the Epaflix TrueNAS host (192.168.10.200) as a managed app, with the NVIDIA GPU passed
 *   through for LOCAL model serving, then front it with Authentik SSO via the established
 *   Traefik forward-auth pattern.
 *
 *   Owner decisions (interview, 2026-06-06):
 *     1. GPU: TrueNAS has an NVIDIA RTX 2070 SUPER (8 GB VRAM), driver 570.172.08 / CUDA 12.8,
 *        kernel modules loaded, GPU idle. TrueNAS 25.10.0.1, Docker 28.3.1. Pass it through.
 *     2. Backend: run LOCAL models on the GPU (Odysseus ships a gpu-nvidia overlay that attaches the
 *        GPU to the odysseus service; it can also drive Ollama/llama.cpp/vLLM). 8 GB fits a quantized
 *        7-8B model. The plan picks the cleanest local-serving wiring and a model that fits 8 GB.
 *     3. Deploy: install as a TrueNAS Custom App (compose YAML in the Apps UI / app API), NOT on k3s.
 *     4. Access: front odysseus.epaflix.com with Authentik SSO (Traefik forward-auth), mirroring the
 *        newtarr (#134) / traefik-dashboard-sso pattern, plus a Cloudflare DNS-only shadow A record.
 *
 *   Odysseus facts (from the repo, 2026-06-06): FastAPI + JS; built from a local Dockerfile (no
 *   published image); base docker-compose.yml services = odysseus (:7000), chromadb (:8100->8000),
 *   searxng (:8080, healthcheck), ntfy (:8091->80); GPU overlay (docker-compose.gpu-nvidia.yml)
 *   attaches the GPU to ONLY the odysseus service via deploy.resources.reservations.devices
 *   (driver: nvidia, count: all, capabilities: [gpu]) + NVIDIA_VISIBLE_DEVICES=all +
 *   NVIDIA_DRIVER_CAPABILITIES=compute,utility; requires the NVIDIA Container Toolkit on the host.
 *   Volumes are bind mounts (./data, ./logs, ./data/ssh:/app/.ssh, ./data/huggingface, ./data/local).
 *   Notable env: APP_BIND (default 127.0.0.1), APP_PORT (7000), AUTH_ENABLED (default true),
 *   LOCALHOST_BYPASS, ODYSSEUS_ADMIN_USER/PASSWORD (temp admin pw printed to logs), SEARXNG_SECRET,
 *   HF_TOKEN, OLLAMA_BASE_URL, LLM_HOST(S), OPENAI_API_KEY, ALLOWED_ORIGINS, SECURE_COOKIES,
 *   ODYSSEUS_SCRIPT_HOST, PUID/PGID.
 *
 *   Security posture the plan/verify MUST resolve: this is a third-party community AI app that builds
 *   from source, runs an autonomous agent/script host, and will be GPU-enabled and (after the expose
 *   gate) reachable behind SSO. So: pin the build to a reviewed commit SHA (no floating main), review
 *   the Dockerfile + compose for risky mounts/capabilities, keep NO plaintext secrets in git
 *   (placeholders in repo; real values only on TrueNAS / secrets.yml), decide AUTH_ENABLED vs the
 *   forward-auth gate (avoid double login but never leave it ungated), and set ALLOWED_ORIGINS /
 *   SECURE_COOKIES for the public host.
 *
 * @inputs { repoRoot, repo, masterSsh, truenasSsh, gpuName, vram, host, slug, odysseusRepo,
 *           authentikBase, authentikNs, embeddedOutpostPk, authFlowPk, invalidationFlowPk,
 *           middlewareName, middlewareNs, traefikLbIp, truenasIp, appPort, branch }
 * @outputs { success, deployed, gpuVisibleInContainer, modelServedOnGpu, exposed, ssoVerified,
 *            prUrl, merged, followUpIssues }
 *
 * Breakpoints (low tolerance / alwaysBreakOn deploy+architecture+secrets): plan/architecture review
 * (authorizes the live TrueNAS + Authentik mutations that follow), TrueNAS deploy (build + app create
 * + model serve), public exposure (Authentik objects + GitOps merge + Cloudflare). Plus conditional
 * anomaly/re-verify gates after each verification.
 *
 * @agent general-purpose (ssh / docker / TrueNAS-midclt / kubectl-over-ssh / Authentik-API / git / gh / curl executor + verifier)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

const SECRETS_HINT =
  'Real secret VALUES (Authentik admin API token under `authentik_admin_api_token`, Cloudflare token, ' +
  'and any odysseus admin/HF/API keys) live in .github/instructions/secrets.yml (git-ignored, pre-commit ' +
  'guarded). NEVER print, echo, or commit any secret. Repo files must use placeholders (e.g. ' +
  '<ODYSSEUS_ADMIN_PASSWORD>, <SEARXNG_SECRET>, <HF_TOKEN>); real values go only onto TrueNAS or stay in ' +
  'secrets.yml. Encrypted Secret files use the *.enc.yaml SOPS+age convention.';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// PHASE 1 — analyze Odysseus + the live TrueNAS host + the exposure pattern. NO mutation.
const analyzeTask = defineTask('analyze', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyze Odysseus, the TrueNAS GPU host, and the Authentik/Traefik exposure pattern',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer onboarding a new GPU container app onto Epaflix TrueNAS',
      task:
        'Gather the exact facts needed to deploy Odysseus on TrueNAS (' + args.truenasIp + ') as a Custom App ' +
        'with NVIDIA GPU passthrough for local model serving, then front it with Authentik SSO. DO NOT mutate anything.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'ODYSSEUS REPO (' + args.odysseusRepo + '): read docker-compose.yml, docker-compose.gpu-nvidia.yml (and any docker/gpu.nvidia.yml overlay), the Dockerfile, .env / .env.example, and the README deploy section. Record: every service + image/build, exact ports, the GPU attachment block, all env vars (mark which are secrets), bind-mount paths, healthchecks, the AGPL extras build arg, and the default admin-bootstrap behaviour. Record the latest commit SHA on the default branch (we will PIN to a reviewed SHA, not float on main).',
        'SECURITY REVIEW of the repo: flag risky mounts/capabilities (e.g. ./data/ssh:/app/.ssh, ODYSSEUS_SCRIPT_HOST / agent script execution, any privileged/host mounts), and note what AUTH_ENABLED / LOCALHOST_BYPASS / ALLOWED_ORIGINS / SECURE_COOKIES do, since this app will be GPU-enabled and SSO-exposed.',
        'TRUENAS HOST (read-only over SSH: `' + args.truenasSsh + ' \'<cmd>\'`): confirm `docker info` shows the nvidia runtime AND/OR `nvidia-ctk --version` / CDI is present (Container Toolkit), confirm `nvidia-smi` (expect ' + args.gpuName + ', ' + args.vram + '). Determine the supported way to run a Custom App on TrueNAS 25.10 that BUILDS from a Dockerfile: does the Apps "Install via YAML"/custom-app path accept a compose `build:` context, or must we pre-`docker build` the image to a local tag first and reference it? Inspect the app/midclt API surface (`midclt call app.query` / app schema) to learn how Custom Apps are created and where their compose/datasets live. Pick candidate ZFS dataset path(s) for odysseus data (e.g. under pool1/dataset01 or the apps/ix-apps dataset) and report free space + VRAM headroom.',
        'GPU RUNTIME CHECK (minimal, read-only): determine WITHOUT deploying whether a container can see the GPU on this host (e.g. confirm the nvidia runtime/CDI is registered; only if trivially safe, a throwaway `docker run --rm --gpus all <existing-cuda-image> nvidia-smi` may be used, otherwise just report the runtime/CDI evidence). Report exactly how a compose service should request the GPU on THIS host (deploy.resources.reservations.devices vs `runtime: nvidia` vs CDI `--device`).',
        'LOCAL-SERVING OPTIONS: given 8 GB VRAM, enumerate the viable local-model paths for Odysseus (its native gpu-nvidia overlay / in-process cookbook serving vs a sidecar Ollama container) and 1-2 concrete models that fit 8 GB quantized (e.g. an 8B Q4). Note if TrueNAS already runs an Ollama app that could be reused.',
        'EXPOSURE PATTERN (repo + live, read-only): find how an EXISTING TrueNAS-hosted service is reverse-proxied through k3s Traefik to 192.168.10.200 (the CLAUDE.md note "truenas -> ' + args.traefikLbIp + ' via Traefik proxy"); locate that IngressRoute + any ExternalName/endpoints Service so we can mirror it for odysseus.epaflix.com -> ' + args.truenasIp + ':<appPort>. Read the canonical forward-auth reference (2-k3s/05.traefik-deployment/middleware/authentik-forwardauth.yaml + the dashboard-sso / newtarr ingressroute) and the Cloudflare DNS-only shadow-record convention. Authentik (read-only via API at ' + args.authentikBase + ', ' + SECRETS_HINT + '): confirm the embedded outpost pk=' + args.embeddedOutpostPk + ' exists (record its current providers), the auth flow pk=' + args.authFlowPk + ' and invalidation flow pk=' + args.invalidationFlowPk + ' resolve, and list candidate access groups (is there an "Odysseus users"/general group, or should one be created?).',
        'Return ONLY the structured JSON state — exact values and gaps, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['odysseus', 'security', 'truenas', 'gpuRuntime', 'localServing', 'exposure', 'authentik', 'gaps', 'summary'],
      properties: {
        odysseus: { type: 'object' },
        security: { type: 'object' },
        truenas: { type: 'object' },
        gpuRuntime: { type: 'object' },
        localServing: { type: 'object' },
        exposure: { type: 'object' },
        authentik: { type: 'object' },
        gaps: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 2 — produce the concrete deploy + exposure plan. NO mutation.
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the concrete Odysseus TrueNAS-GPU deploy + SSO exposure plan',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC/platform engineer planning a GPU container deploy + SSO exposure',
      task:
        'Turn the analysis into a concrete, ordered, reversible plan to deploy Odysseus on TrueNAS with GPU ' +
        'passthrough for local model serving and front it with Authentik SSO. Plan only — no changes.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'COMPOSE / APP SPEC: produce the exact Custom App compose YAML (base + gpu-nvidia overlay merged) for TrueNAS: services, the GPU request block as confirmed correct for THIS host, APP_BIND=0.0.0.0 + APP_PORT, bind mounts remapped to the chosen ZFS dataset path(s), and the full env with secret values as PLACEHOLDERS only. PIN the build to the reviewed commit SHA (no floating main). Decide build-vs-prebuilt-image per the analysis (if the Custom App path cannot build, specify the `docker build -t odysseus:<sha>` step and reference that tag).',
        'LOCAL MODEL SERVING: specify the chosen local-serving wiring (native overlay GPU->odysseus, or a sidecar Ollama with the GPU reservation) and the EXACT model to pull/serve that fits ' + args.vram + ' (name + quantization + approx VRAM). Specify how odysseus is pointed at it (OLLAMA_BASE_URL / LLM_HOST / cookbook) and how we will later prove a completion ran ON the GPU (nvidia-smi shows util / VRAM use).',
        'SECRETS: list every secret env (ODYSSEUS_ADMIN_PASSWORD, SEARXNG_SECRET, HF_TOKEN, any API keys) and exactly where each real value comes from / is stored (secrets.yml or generated on TrueNAS) and how it is injected into the app WITHOUT entering git. ' + SECRETS_HINT,
        'AUTH MODEL: resolve how Odysseus in-app auth (AUTH_ENABLED / LOCALHOST_BYPASS) coexists with the Authentik forward-auth gate so the user sees ONE login (Authentik) and the app is NEVER left ungated. Be explicit (e.g. keep AUTH_ENABLED but trust the proxied upstream, or the cleanest mechanism Odysseus supports), and set ALLOWED_ORIGINS / SECURE_COOKIES for https://' + args.host + '.',
        'EXPOSURE CHANGE SET (GitOps, branch+PR+merge under the Epaflix merge-commit+rebase policy): the k3s Traefik IngressRoute (+ ExternalName/endpoints Service) that routes https://' + args.host + ' to ' + args.truenasIp + ':' + args.appPort + ', mirroring the existing TrueNAS-proxy route; attach the ' + args.middlewareName + ' middleware (priority 10) + add the outpost route (priority 15, PathPrefix(/outpost.goauthentik.io/) -> authentik-server@' + args.authentikNs + ':80); the Cloudflare DNS-only shadow A record ' + args.host + ' -> ' + args.traefikLbIp + '. Specify repo file placement for BOTH the TrueNAS compose (codified with placeholders) and the k3s manifests + a short README/runbook.',
        'AUTHENTIK OBJECTS (live, via API, created at the expose step): Proxy Provider (forward_single, external_host https://' + args.host + ', authorization_flow=' + args.authFlowPk + ', invalidation_flow=' + args.invalidationFlowPk + '), Application (slug "' + args.slug + '"), group policy binding (which group), and add the provider pk to the embedded outpost pk=' + args.embeddedOutpostPk + ' PRESERVING existing providers.',
        'ORDER (and why): (a) author + validate repo artifacts; (b) DEPLOY on TrueNAS (datasets, secrets, build, app create, serve model) and verify GPU + UI + a GPU-backed completion BEFORE any public exposure; (c) only then create Authentik objects + merge the GitOps route + add the Cloudflare record (Authentik objects must exist before the middleware goes live). ',
        'TEST PLAN: enumerate verifiable checks — `docker exec <odysseus> nvidia-smi` shows the GPU; a chat/completion runs and nvidia-smi shows GPU util/VRAM; the UI is reachable on the LAN; searxng/chromadb healthy; post-expose `curl -sI https://' + args.host + '/` returns a 302 to the Authentik flow (not a 200 app page); group gating works.',
        'RISKS + ROLLBACK per surface (delete the TrueNAS app + dataset; delete Authentik objects; revert the GitOps PR; remove the Cloudflare record). Enumerate every breakpoint.',
        'If feedback from a prior rejection is in context, incorporate it. Return ONLY the structured JSON plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['composeSpec', 'localServing', 'secrets', 'authModel', 'exposureChangeSet', 'authentikSteps', 'order', 'testPlan', 'risks', 'rollback', 'summary'],
      properties: {
        composeSpec: { type: 'object' },
        localServing: { type: 'object' },
        secrets: { type: 'array', items: { type: 'object' } },
        authModel: { type: 'object' },
        exposureChangeSet: { type: 'array', items: { type: 'object' } },
        authentikSteps: { type: 'array', items: { type: 'object' } },
        order: { type: 'array', items: { type: 'string' } },
        testPlan: { type: 'array', items: { type: 'string' } },
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

// PHASE 3 — author the repo artifacts on a branch (compose codified w/ placeholders + k3s manifests + runbook). NO push, NO live deploy.
const authorTask = defineTask('author-artifacts', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the codified compose + k3s SSO manifests + runbook on a branch',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer codifying a new TrueNAS app + its k3s exposure under the Epaflix policy',
      task: 'Apply the approved change set on a fresh branch and make a path-scoped local commit. No push, no live deploy.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'From repoRoot=' + args.repoRoot + ', create branch `' + args.branch + '` off an up-to-date origin/main (`git fetch origin && git switch -c ' + args.branch + ' origin/main`).',
        'Write the TrueNAS Odysseus Custom App compose YAML (placeholders for ALL secrets) and the k3s Traefik IngressRoute (+ ExternalName/endpoints Service) + forward-auth + outpost route exactly as the approved plan specifies, mirroring the existing TrueNAS-proxy route and the authentik-forwardauth reference. Place files where the plan specified; add a short README/runbook documenting the deploy + GPU + SSO + rollback. ' + SECRETS_HINT,
        'Validate what is locally renderable: `kustomize build` (or `kubectl kustomize`) any kustomize dir touched, and YAML-parse the compose + manifests. Confirm the pre-commit SOPS guard passes (no plaintext kind: Secret; secrets are placeholders).',
        'Commit ONLY the changed/added files (git add specific paths — do NOT add .a5c/ or process scaffolding; this repo keeps process defs untracked). Conventional commit subject; end with the Co-Authored-By trailer the repo uses. Do NOT push.',
        'Return ONLY the structured JSON with branch, commit sha, files changed, and validation results.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'filesChanged', 'validated', 'summary'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        validated: { type: 'boolean' },
        noPlaintextSecrets: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 4 — deploy on TrueNAS: datasets + secrets + build + Custom App + serve model. LIVE (gated).
const deployTruenasTask = defineTask('deploy-truenas', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Deploy Odysseus on TrueNAS with GPU passthrough and serve a local model',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE deploying a GPU Custom App on TrueNAS over SSH/midclt',
      task:
        'Stand up Odysseus on TrueNAS (' + args.truenasIp + ') exactly as the approved plan specifies: create the ' +
        'dataset(s), inject secrets WITHOUT committing them, build the pinned image, create the Custom App with the ' +
        'GPU attached, and serve the chosen local model. Idempotent where possible.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        'TrueNAS over SSH: `' + args.truenasSsh + ' \'<cmd>\'`. ' + SECRETS_HINT,
        'Create the ZFS dataset(s) for odysseus data per the plan (midclt or zfs). Clone the odysseus repo at the PINNED commit SHA into the build/context dataset.',
        'Materialize the env/compose on TrueNAS with REAL secret values pulled from secrets.yml or generated locally (e.g. SEARXNG_SECRET, ODYSSEUS_ADMIN_PASSWORD) — write them only to the on-host env file (chmod 600), never to git. Confirm APP_BIND=0.0.0.0 and the GPU block are present.',
        'Build the image (`docker build -t odysseus:<sha>`) if the plan requires a prebuilt image; then create/install the TrueNAS Custom App from the compose (Apps UI YAML / `midclt call app.create` with the custom compose, whichever the analysis confirmed). Bring the stack up.',
        'Serve the chosen local model on the GPU per the plan (e.g. pull the model into Ollama / the odysseus cookbook). ',
        'Sanity: all containers Running/healthy (odysseus, chromadb, searxng, ntfy [+ ollama if used]); capture the bootstrap admin password from logs into the on-host runbook location (NOT git) if one was generated. Report container status + the in-container GPU evidence command you will hand to verification.',
        'Do NOT expose anything publicly yet (no Authentik, no Traefik merge, no Cloudflare). Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['datasetsCreated', 'imageBuilt', 'appCreated', 'containersUp', 'modelServed', 'anomalies', 'summary'],
      properties: {
        datasetsCreated: { type: 'array', items: { type: 'string' } },
        imageBuilt: { type: ['boolean', 'string'] },
        appCreated: { type: 'boolean' },
        containersUp: { type: 'array', items: { type: 'string' } },
        modelServed: { type: 'object' },
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

// PHASE 5 — verify the LOCAL deploy: GPU in container + UI + a GPU-backed completion. Read-only.
const verifyDeployTask = defineTask('verify-deploy', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify GPU passthrough, UI reachability, and a GPU-backed model completion',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying a GPU container deploy on TrueNAS',
      task: 'Prove Odysseus is running with the GPU usable for local inference, before any public exposure. Read-only.',
      context: { ...args },
      instructions: [
        'TrueNAS over SSH: `' + args.truenasSsh + ' \'<cmd>\'`.',
        'GPU IN CONTAINER: `docker exec <odysseus-or-serving-container> nvidia-smi` must list ' + args.gpuName + '. Confirm NVIDIA_VISIBLE_DEVICES / the CDI device is actually present in the container.',
        'UI: from the host, `curl -sS -o /dev/null -w "%{http_code}" http://' + args.truenasIp + ':' + args.appPort + '/` (or the mapped port) returns a 200/302 app response; searxng + chromadb containers healthy.',
        'GPU-BACKED COMPLETION: trigger a small local model completion (via the served model API / odysseus) and confirm `nvidia-smi` shows GPU utilization or VRAM in use during/after it (prove the model ran on the GPU, not CPU). Record the model + the observed VRAM/util.',
        'If attempt>1 this is a re-verify after a transient issue. Return ONLY the structured JSON verdict with anomalies.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'gpuVisibleInContainer', 'uiReachable', 'modelServedOnGpu', 'observedGpuUse', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        gpuVisibleInContainer: { type: 'boolean' },
        uiReachable: { type: 'boolean' },
        modelServedOnGpu: { type: 'boolean' },
        observedGpuUse: { type: 'string' },
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

// PHASE 6 — expose: create Authentik objects, merge the GitOps route, add the Cloudflare record. LIVE (gated).
const exposeTask = defineTask('expose', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Create Authentik objects, merge the Traefik SSO route, add the Cloudflare DNS-only record',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer exposing a TrueNAS app via Authentik SSO + k3s Traefik under the Epaflix policy',
      task:
        'Front https://' + args.host + ' with Authentik SSO exactly as the approved plan specifies. Create the ' +
        'Authentik objects FIRST (must exist before the middleware goes live), then merge the GitOps route, then ' +
        'add the Cloudflare DNS-only record.',
      context: { ...args, secretsHint: SECRETS_HINT },
      instructions: [
        SECRETS_HINT,
        'AUTHENTIK (API at ' + args.authentikBase + '/api/v3, idempotent — reuse if exists): create the Proxy Provider (forward_single, external_host https://' + args.host + ', authorization_flow=' + args.authFlowPk + ', invalidation_flow=' + args.invalidationFlowPk + '), the Application (slug "' + args.slug + '"), the group policy binding per the plan, and PATCH the embedded outpost pk=' + args.embeddedOutpostPk + ' to append the new provider pk PRESERVING existing providers. Read back each object to confirm.',
        'GITOPS: `git push -u origin ' + args.branch + '`; open the PR against ' + args.repo + ' base main with a `## Test plan` checklist (the approved testPlan, unchecked). Enforce the policy: `git fetch origin && git rebase origin/main` then `git push --force-with-lease`; wait for the required `validate` check (`gh pr checks --watch`); if it fails on the KNOWN unpinned-kustomize-install CI flake (fast ~6s fail), `gh run rerun --failed` and re-watch. Merge with `gh pr merge <n> --merge` (NOT squash/rebase). Confirm the `Merge pull request #<n>` marker on main; ArgoCD then applies the live route + middleware.',
        'CLOUDFLARE: add the DNS-only shadow A record ' + args.host + ' -> ' + args.traefikLbIp + ' (using the Cloudflare token from secrets.yml) per the repo convention, so the proxied wildcard does not hijack the LAN-only host.',
        'Return ONLY the structured JSON result with the Authentik PKs, PR url/number, merged flag, and the Cloudflare record id.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['providerPk', 'applicationSlug', 'outpostUpdated', 'prUrl', 'prNumber', 'merged', 'cloudflareRecord', 'anomalies', 'summary'],
      properties: {
        providerPk: { type: ['integer', 'string'] },
        applicationSlug: { type: 'string' },
        outpostUpdated: { type: 'boolean' },
        prUrl: { type: 'string' },
        prNumber: { type: ['integer', 'string'] },
        merged: { type: 'boolean' },
        cloudflareRecord: { type: ['string', 'object', 'null'] },
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

// PHASE 7 — verify SSO end-to-end. Read-only.
const verifySsoTask = defineTask('verify-sso', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify the Authentik SSO route is live and gating odysseus.epaflix.com',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the SSO exposure against live state',
      task: 'Prove https://' + args.host + ' is fronted by Authentik SSO with no ungated path. Read-only checks.',
      context: { ...args },
      instructions: [
        'Wait for ArgoCD to reconcile the route (poll the owning Application Synced/Healthy via `' + args.masterSsh + ' \'kubectl -n argocd get application <app> -o jsonpath="{.status.sync.status}/{.status.health.status}"\'`).',
        'Confirm the live IngressRoute carries the ' + args.middlewareName + ' middleware and the outpost route exists (`' + args.masterSsh + ' \'kubectl -n <ns> get ingressroute -o yaml\'`).',
        'E2E (unauthenticated): `curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\\n" https://' + args.host + '/` expects a 302/307 to the Authentik flow, NOT a 200 Odysseus page. `curl -sI https://' + args.host + '/outpost.goauthentik.io/auth/traefik` is handled by the outpost (not 404). Confirm no double-login regression per the approved authModel.',
        'If attempt>1 this is a re-verify. Return ONLY the structured JSON verdict.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'middlewareLive', 'outpostRouteLive', 'ssoRedirects', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        middlewareLive: { type: 'boolean' },
        outpostRouteLive: { type: 'boolean' },
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

// PHASE 8 — closeout: PR test plan, follow-up issues, history log.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Tick the PR test plan, open follow-ups, log to .history',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Maintainer closing out the Odysseus deploy per Epaflix conventions',
      task: 'Record verification on the PR, open follow-up issues, and log the run.',
      context: { ...args },
      instructions: [
        'EXECUTE PR TEST PLAN: tick each box in the PR description by EDITING the PR body (gh pr edit / API) with the actual outcomes — NEVER add a new PR comment. Strike through + note any N/A step.',
        'FOLLOW-UPS: open a `gh issue` on ' + args.repo + ' (shape: `## Finding / ## Current state / ## Desired outcome / ## Notes`) for any genuinely deferred item. Likely candidates: pin/track Odysseus image updates (it builds from a community repo at a pinned SHA), rotate any secret that touched disk, evaluate moving the app under a managed/GitOps lifecycle, or VRAM headroom / larger-model follow-up. Only open warranted issues; cross-link them.',
        'Log a short summary of the run to .history (per the repo history convention) — what was deployed, the GPU/model proven, the SSO exposure, and the PR.',
        'Return ONLY the structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['testPlanRecorded', 'followUpIssues', 'historyLogged', 'summary'],
      properties: {
        testPlanRecorded: { type: 'boolean' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        historyLogged: { type: 'boolean' },
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
    masterSsh: 'ssh ubuntu@192.168.10.51',
    truenasSsh: 'ssh truenas_admin@192.168.10.200',
    odysseusRepo: 'https://github.com/pewdiepie-archdaemon/odysseus',
    gpuName: 'NVIDIA GeForce RTX 2070 SUPER',
    vram: '8 GB VRAM',
    truenasIp: '192.168.10.200',
    traefikLbIp: '192.168.10.101',
    appPort: '7000',
    host: 'odysseus.epaflix.com',
    slug: 'odysseus',
    authentikBase: 'https://auth.epaflix.com',
    authentikNs: 'app-authentik',
    embeddedOutpostPk: '209f71f9-95f8-4264-91c2-4b065bbd6b07',
    authFlowPk: '847dc682-757c-4bf8-925f-c8c066a0be4f',
    invalidationFlowPk: '436f6b06-d1a1-42db-af49-0eaac2bce88a',
    middlewareName: 'authentik-forwardauth',
    middlewareNs: 'traefik-system',
    branch: 'odysseus-truenas-gpu-deploy',
    ...inputs,
  };

  ctx.log('info', 'Odysseus TrueNAS GPU deploy: analyze -> plan[BP] -> author -> deploy[BP] -> verify -> expose[BP] -> verify-sso -> closeout');

  // PHASE 1 — analyze (read-only).
  const state = await ctx.task(analyzeTask, { ...cfg });
  ctx.log('info', `Analyze: ${state.summary}`);

  // PHASE 2 — plan with refine/review loop; the gate authorizes the live TrueNAS + Authentik mutations.
  let plan, lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    plan = await ctx.task(planTask, {
      ...cfg, state, feedback: lastFeedback || undefined, attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const gate = await ctx.breakpoint({
      question:
        'Review the PLAN to deploy Odysseus on TrueNAS (GPU passthrough, local model serving) and front ' +
        cfg.host + ' with Authentik SSO.\n\n' +
        'Compose/app spec: ' + JSON.stringify(plan.composeSpec) + '\n' +
        'Local serving + model: ' + JSON.stringify(plan.localServing) + '\n' +
        'Secrets handling: ' + JSON.stringify(plan.secrets) + '\n' +
        'Auth model (no double login, never ungated): ' + JSON.stringify(plan.authModel) + '\n' +
        'Exposure change set: ' + JSON.stringify(plan.exposureChangeSet) + '\n' +
        'Authentik objects: ' + JSON.stringify(plan.authentikSteps) + '\n' +
        'Order: ' + JSON.stringify(plan.order) + '\n' +
        'Test plan: ' + JSON.stringify(plan.testPlan) + '\n' +
        'Risks: ' + JSON.stringify(plan.risks) + '\n' +
        'Rollback: ' + JSON.stringify(plan.rollback) + '\n\n' +
        'Summary: ' + plan.summary + '\n\n' +
        'Approving authorizes the LATER live mutations (TrueNAS dataset/build/app create + model serve, and ' +
        'at the expose gate the Authentik objects + GitOps merge + Cloudflare record). Approve the plan?',
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

  // PHASE 3 — author repo artifacts on a branch (no push, no live deploy).
  const change = await ctx.task(authorTask, { ...cfg, approvedPlan: plan, state });
  ctx.log('info', `Authored: branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.filesChanged)} validated=${change.validated}`);

  // GATE — TrueNAS deploy (build + app create + model serve). alwaysBreakOn deploy.
  const deployGate = await ctx.breakpoint({
    question:
      'Approve the TrueNAS DEPLOY? This will create the ZFS dataset(s), inject real secrets onto the host ' +
      '(never into git), build the pinned Odysseus image, create the Custom App with the ' + cfg.gpuName +
      ' attached, bring the stack up, and serve the chosen local model on the GPU. Nothing is exposed publicly ' +
      'in this step. Compose/app per the approved plan. Proceed?',
    options: ['Approve deploy', 'Stop here'],
    expert: 'owner',
    tags: ['deploy', 'secrets', 'approval-gate'],
  });
  if (!deployGate.approved || (deployGate.response || '').toLowerCase().match(/stop|abort/)) {
    return { success: false, deployed: false, reason: 'deploy-not-approved', branch: change.branch, feedback: deployGate.response || '' };
  }

  // PHASE 4 — deploy on TrueNAS (live).
  const deploy = await ctx.task(deployTruenasTask, { ...cfg, plan, state });
  ctx.log('info', `Deploy: app=${deploy.appCreated} containers=${JSON.stringify(deploy.containersUp)} model=${JSON.stringify(deploy.modelServed)}`);

  // PHASE 5 — verify the local deploy (GPU + UI + GPU-backed completion), with an anomaly/re-verify gate.
  let vdep = await ctx.task(verifyDeployTask, { ...cfg, deploy, plan });
  if (!vdep.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Local deploy verification found issues.\n' +
        'gpuVisibleInContainer: ' + vdep.gpuVisibleInContainer + '; uiReachable: ' + vdep.uiReachable +
        '; modelServedOnGpu: ' + vdep.modelServedOnGpu + ' (' + vdep.observedGpuUse + ')\n' +
        'anomalies: ' + JSON.stringify(vdep.anomalies) + '\nsummary: ' + vdep.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to expose (accept)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      vdep = await ctx.task(verifyDeployTask, { ...cfg, deploy, plan, attempt: 2 });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, deployed: true, exposed: false, reason: 'deploy-verification-stop', verifyDeploy: vdep, branch: change.branch };
    }
  }

  // GATE — public exposure (Authentik + GitOps merge + Cloudflare). Outward-facing.
  const exposeGate = await ctx.breakpoint({
    question:
      'Approve PUBLIC EXPOSURE of ' + cfg.host + '? This creates the Authentik Proxy Provider + Application + ' +
      'group binding + outpost assignment (live, via API), pushes branch `' + change.branch + '`, opens + rebases + ' +
      'merges the GitOps PR (ArgoCD then applies the Traefik route + ' + cfg.middlewareName + ' middleware live), and ' +
      'adds the Cloudflare DNS-only record ' + cfg.host + ' -> ' + cfg.traefikLbIp + '. The app is verified GPU-working ' +
      'locally. Proceed?',
    options: ['Approve expose', 'Skip exposure (stop, leave LAN-only)', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'architecture-change', 'approval-gate'],
  });
  if (!exposeGate.approved || (exposeGate.response || '').toLowerCase().match(/abort|skip|stop/)) {
    return { success: true, deployed: true, exposed: false, reason: 'exposure-not-approved', branch: change.branch, verifyDeploy: vdep, feedback: exposeGate.response || '' };
  }

  // PHASE 6 — expose (live).
  const expose = await ctx.task(exposeTask, { ...cfg, branch: change.branch, plan, state });
  ctx.log('info', `Expose: provider=${expose.providerPk} app=${expose.applicationSlug} pr=${expose.prUrl} merged=${expose.merged} cf=${JSON.stringify(expose.cloudflareRecord)}`);
  if (!expose.merged) {
    return { success: false, deployed: true, exposed: false, reason: 'expose-merge-failed', expose, branch: change.branch, verifyDeploy: vdep };
  }

  // PHASE 7 — verify SSO end-to-end, with an anomaly/re-verify gate.
  let vsso = await ctx.task(verifySsoTask, { ...cfg });
  if (!vsso.verified) {
    const recover = await ctx.breakpoint({
      question:
        'SSO verification found issues.\n' +
        'middlewareLive: ' + vsso.middlewareLive + '; outpostRouteLive: ' + vsso.outpostRouteLive +
        '; ssoRedirects: ' + vsso.ssoRedirects + '\nanomalies: ' + JSON.stringify(vsso.anomalies) +
        '\nsummary: ' + vsso.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      vsso = await ctx.task(verifySsoTask, { ...cfg, attempt: 2 });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, deployed: true, exposed: true, reason: 'sso-verification-stop', verifySso: vsso, expose };
    }
  }

  // PHASE 8 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, host: cfg.host, slug: cfg.slug, prUrl: expose.prUrl,
    verifyDeploy: vdep, verifySso: vsso, expose, deploy,
  });
  ctx.log('info', `Closeout: follow-ups=${JSON.stringify(close.followUpIssues)} history=${close.historyLogged}`);

  return {
    success: true,
    deployed: true,
    gpuVisibleInContainer: !!vdep.gpuVisibleInContainer,
    modelServedOnGpu: !!vdep.modelServedOnGpu,
    exposed: true,
    ssoVerified: vsso.verified && vsso.ssoRedirects,
    prUrl: expose.prUrl,
    merged: expose.merged,
    followUpIssues: close.followUpIssues,
  };
}

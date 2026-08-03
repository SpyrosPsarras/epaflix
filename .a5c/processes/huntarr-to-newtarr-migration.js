/**
 * @process specializations/code-migration-modernization/configuration-migration
 * @description Deliver Epaflix issue #131: FULL rename migration huntarr -> newtarr with NO
 *   leftovers and ALL prior functionality preserved. Upstream Huntarr is discontinued (image
 *   gone, ImagePullBackOff); migrate to the maintained ElfHosted fork Newtarr
 *   (ghcr.io/elfhosted/newtarr:rolling), a config-compatible drop-in.
 *
 *   User decision: rename EVERYTHING (k8s resources, PVC, ingress, DNS host, Authentik SSO app)
 *   from `huntarr` to `newtarr`, but carry the existing config forward by copying the SQLite
 *   state (huntarr.db/logs.db/backups) from the old huntarr-config PVC into the new
 *   newtarr-config PVC — so the prowlarr/qbittorrent/servarr (Sonarr+Sonarr2) integrations and
 *   the tracked-items state survive the rename.
 *
 *   Surface spans BOTH GitOps (manifests, image-updater list, docs — via branch+PR+merge under
 *   the Epaflix merge-commit+rebase policy) AND runtime (Authentik Application/Proxy-Provider,
 *   Pi-hole dnsmasq + Cloudflare shadow record, live config-data copy, orphan cleanup since the
 *   servarr App has prune:OFF). selfHeal:true on the servarr App means the rename must go
 *   through git; the old huntarr live resources become orphans that must be MANUALLY deleted
 *   (prune off).
 *
 * @inputs { repoRoot, repo, issue, branch, masterSsh, ns, servarrApp, oldName, newName,
 *           newImage, oldDir, newDir, kustomization, appServarrManifest, port, metricsPort,
 *           oldHost, newHost, traefikLbIp, authentikNs }
 * @outputs { success, merged, prUrl, configCarried, authentikMigrated, dnsCutover,
 *            oldResourcesRemoved, leftoversZero, issueState, followUpIssues }
 *
 * Safe ordering: keep the OLD huntarr live (serving) through the migration. Merge the rename ->
 * ArgoCD creates the new `newtarr` Deployment + empty `newtarr-config` PVC alongside the still-
 * running orphaned `huntarr`. THEN copy config into newtarr, verify newtarr internally, migrate
 * Authentik to newtarr, cut DNS over, and ONLY THEN delete the old huntarr resources + PVC.
 *
 * Breakpoints (low tolerance / alwaysBreakOn destructive+deploy+secrets): plan review, deploy
 * (push+PR+merge), config data copy (destructive), Authentik change (secrets/SSO), DNS cutover,
 * delete-old-huntarr (destructive), plus an anomaly gate on final verify.
 *
 * @agent general-purpose (kubectl-over-ssh / git / gh / Authentik-API / Pi-hole / Cloudflare executor + verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// PHASE 1 — discover the full live huntarr footprint (NO mutation).
const discoverRuntimeTask = defineTask('discover-runtime', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Inventory the complete live + git huntarr footprint before any change',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD/Authentik/DNS SRE on the Epaflix k3s cluster',
      task:
        'Build a COMPLETE inventory of everything named/related to `' + args.oldName + '` across git AND ' +
        'runtime, so a full rename to `' + args.newName + '` can be planned with zero leftovers and config ' +
        'preserved. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'Run git/grep locally from repoRoot=' + args.repoRoot + '. kubectl access is over SSH — prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'GIT: `grep -rn -i huntarr` across the repo; classify each hit as (a) functional manifest, (b) image-updater list/annotation, (c) doc, (d) verbatim third-party research artifact (e.g. 2-k3s/08.servarr/research/user_config.yaml — a captured TrueNAS catalog snapshot) or (e) dead *.backup. Record file:line for every hit.',
        'LIVE K8S: enumerate huntarr resources in namespace ' + args.ns + ': `kubectl -n ' + args.ns + ' get deploy,svc,pvc,pdb,pods -l app=' + args.oldName + ' -o wide` plus `kubectl -n ' + args.ns + ' get pvc ' + args.oldName + '-config -o json`. Record the PVC\'s boundvolume, the node it is on, and (critical for the later data copy) the on-disk local-path directory of the huntarr-config PVC on that node (pattern: /var/lib/rancher/k3s/storage/<pvc-id>_' + args.ns + '_' + args.oldName + '-config/). List the SQLite files present there (huntarr.db, logs.db, backups/...) with sizes/mtimes by SSHing the worker node.',
        'INGRESS/ROUTING: find how ' + args.oldHost + ' is served — search live IngressRoutes/Ingress and Traefik config (`kubectl -n ' + args.ns + ' get ingressroute,ingress -A | grep -i huntarr` and check the authentik outpost ingressroute in 2-k3s/07.authentik-deployment/ingress). Determine whether huntarr is fronted by an Authentik embedded-outpost proxy provider (most likely, given there is NO huntarr IngressRoute in git) or a Traefik forward-auth middleware.',
        'AUTHENTIK (runtime): using the Authentik API (base https://authentik.epaflix.com or the in-cluster service; token from .github/instructions/secrets.yml — look for an authentik api/admin token; NEVER print it), enumerate the huntarr objects: Application (slug/name), Proxy Provider (external host, internal/upstream URL, mode), the embedded/outpost assignment, and any policy/group bindings (which groups gate login). Record their PKs/slugs so they can be recreated as newtarr and the old ones deleted.',
        'DNS: inspect Pi-hole at 192.168.10.30 — `ssh root@192.168.10.30 \'grep -rn -i huntarr /etc/dnsmasq.d/\'` (golden rule: dnsmasq.d only). Determine if ' + args.oldHost + ' has an explicit A record or is only covered by the `*.epaflix.com -> ' + args.traefikLbIp + '` wildcard. Check Cloudflare for a shadow `huntarr` record (DNS-only A -> ' + args.traefikLbIp + ') — note the wildcard-hijack gotcha that LAN-only hosts need a DNS-only shadow record. Use the Cloudflare token referenced in secrets.yml/memory if a CF check is needed (read-only).',
        'Return ONLY the structured JSON inventory — exhaustive, with exact paths/PKs/on-disk dirs — not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['gitHits', 'liveK8s', 'configPvc', 'sqliteFiles', 'routing', 'authentik', 'dns', 'summary'],
      properties: {
        gitHits: { type: 'array', items: { type: 'object' } },
        liveK8s: { type: 'object' },
        configPvc: { type: 'object' },
        sqliteFiles: { type: 'array', items: { type: 'string' } },
        routing: { type: 'object' },
        authentik: { type: 'object' },
        dns: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 2 — produce the concrete migration plan from the inventory (NO mutation).
const planMigrationTask = defineTask('plan-migration', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the concrete, ordered migration plan from the inventory',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC/platform engineer planning a zero-leftover rename migration',
      task:
        'Turn the huntarr inventory into a concrete, ordered, reversible migration plan to fully rename ' +
        '`' + args.oldName + '` -> `' + args.newName + '` with config carried forward. Plan only — no changes.',
      context: { ...args },
      instructions: [
        'Produce the exact GIT change set: rename dir ' + args.oldDir + ' -> ' + args.newDir + '; in the manifest rename Deployment/Service/PVC(claimName ' + args.newName + '-config)/PDB/labels/selector to ' + args.newName + ' and set image to ' + args.newImage + ' (keep HUNTARR_PORT env value ' + args.port + ' but rename the env var to NEWTARR_PORT if newtarr expects it — verify the fork\'s env contract; default to keeping behaviour identical); update ' + args.kustomization + ' resource paths; update ' + args.appServarrManifest + ' image-updater list key to `' + args.newName + '=' + args.newImage + '` with annotations `' + args.newName + '.update-strategy: digest` and `' + args.newName + '.allow-tags: regexp:^rolling$`; update docs (11.argocd/README.md table, 08.servarr/README.md PVC list, 04.coredns/README.md host) and remove the dead huntarr stanza from internal-routes.yaml.backup. Decide explicitly how to handle verbatim research artifacts (leave snapshot intact, flag for verify).',
        'Produce the RUNTIME plan, ORDERED for safety (old huntarr stays live & serving until the very end): (1) merge git rename -> ArgoCD creates newtarr + empty newtarr-config PVC; (2) copy SQLite config huntarr-config -> newtarr-config while newtarr is quiesced (stop the newtarr pod for SQLite integrity; restore after); (3) verify newtarr internally (config carried); (4) migrate Authentik app/provider to newtarr (new external host ' + args.newHost + ', upstream http://' + args.newName + '.' + args.ns + '.svc:' + args.port + '), reattach outpost + group bindings, delete old huntarr app/provider; (5) DNS cutover (Pi-hole + Cloudflare shadow) add ' + args.newHost + ', remove ' + args.oldHost + '; (6) delete orphaned live huntarr resources + old PVC (prune is OFF on the servarr App so this is manual).',
        'For the config copy, give the EXACT commands using the on-disk PVC dirs from the inventory (rm empty newtarr dir contents, cp huntarr.db/logs.db/backups, chown 568:568). Include how to quiesce newtarr without selfHeal fighting you (e.g. `argocd app set ' + args.servarrApp + ' --sync-policy none` or kubectl scale + immediate copy, then restore) and note the tradeoff.',
        'List every breakpoint the migration should pause at and why. Enumerate the FINAL leftover-check: git grep huntarr (functional==0), live k8s huntarr resources==0, Authentik huntarr objects==0, DNS huntarr==0.',
        'If feedback from a prior plan rejection is in context, incorporate it.',
        'Return ONLY the structured JSON plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['gitChangeSet', 'runtimeSteps', 'configCopyCommands', 'authentikSteps', 'dnsSteps', 'cleanupSteps', 'leftoverChecks', 'risks', 'summary'],
      properties: {
        gitChangeSet: { type: 'array', items: { type: 'object' } },
        runtimeSteps: { type: 'array', items: { type: 'string' } },
        configCopyCommands: { type: 'array', items: { type: 'string' } },
        authentikSteps: { type: 'array', items: { type: 'string' } },
        dnsSteps: { type: 'array', items: { type: 'string' } },
        cleanupSteps: { type: 'array', items: { type: 'string' } },
        leftoverChecks: { type: 'array', items: { type: 'string' } },
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

// PHASE 3 — author the git rename on a branch + local commit (reversible, no push).
const authorManifestsTask = defineTask('author-manifests', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply the git rename (manifests, image, image-updater, docs) + local commit on branch',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer editing Kustomize + ArgoCD manifests in the Epaflix repo',
      task:
        'Execute the approved git change set: rename huntarr -> newtarr across manifests, set the Newtarr ' +
        'image, update the image-updater list + docs, validate the render, then branch + ONE local commit. ' +
        'Do NOT push, open a PR, touch runtime, or touch issues.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md (never commit secrets; encrypted Secrets stay *.enc.yaml).',
        'Apply the approvedPlan.gitChangeSet exactly: git mv ' + args.oldDir + ' ' + args.newDir + '; rename the manifest file inside; rename Deployment/Service/PVC(claimName ' + args.newName + '-config)/PDB/labels/selector -> ' + args.newName + '; set image: ' + args.newImage + '; add a comment noting the ElfHosted Newtarr fork is community-maintained with no support guarantee.',
        'Update ' + args.kustomization + ' resource paths to the new dir/files. Update ' + args.appServarrManifest + ': replace the `huntarr=...` image-list entry with `' + args.newName + '=' + args.newImage + ',` and rename the two `huntarr.update-strategy`/`huntarr.allow-tags` annotations to `' + args.newName + '.*`, setting allow-tags to `regexp:^rolling$` (keep update-strategy: digest).',
        'Update docs: 2-k3s/11.argocd/README.md image table, 2-k3s/08.servarr/README.md PVC list, 2-k3s/04.coredns/README.md host (' + args.oldHost + ' -> ' + args.newHost + '). Remove the dead huntarr stanza from 2-k3s/08.servarr/_shared/ingress/internal-routes.yaml.backup. Leave the verbatim TrueNAS research snapshot (research/user_config.yaml) intact but note it for the verify leftover-report.',
        'VALIDATE: run `kustomize build 2-k3s/08.servarr` (add `--enable-alpha-plugins --enable-exec` if a ksops generator is present). If it fails ONLY on a ksops/age decryption step (no age key on this host), that is expected — confirm the newtarr resources themselves render and the failure is solely the encrypted-secret generator; otherwise fix real errors. Also `yamllint`/`kubectl --dry-run=client` the changed manifests if available.',
        'Create branch ' + args.branch + ' off origin/main (reuse if exists). Stage ONLY the changed/renamed files. ONE commit referencing #' + args.issue + '. End the commit body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'If feedback from a prior rejection is in context, incorporate it.',
        'Return ONLY the structured JSON result, including the full diff, renamed paths, render validation outcome, and a proposed PR title + body. The PR body MUST contain a Test Plan section with checkboxes for: newtarr pod Running 1/1, ArgoCD servarr Synced+Healthy, config carried (tracked items + prowlarr/qbit/servarr connections), SSO login at ' + args.newHost + ', DNS resolves, ZERO huntarr leftovers (git/live/Authentik/DNS).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'renamedPaths', 'changedFiles', 'commitSha', 'diff', 'renderOk', 'renderNotes', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        renamedPaths: { type: 'array', items: { type: 'string' } },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
        renderOk: { type: 'boolean' },
        renderNotes: { type: 'string' },
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

// PHASE 4 — push + PR + rebase + validate + merge (the GitOps deploy).
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase, await validate, merge per Epaflix policy',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open the PR, rebase onto origin/main with --force-with-lease, wait for the required ' +
        '`validate` check, and merge per the Epaflix merge-commit policy. Merging creates the live newtarr.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Push branch ' + args.branch + ' to origin.',
        'Open a PR to main with the approved title/body (approvedPrTitle/approvedPrBody in context). Cross-link #' + args.issue + '.',
        'Per Epaflix policy: rebase the branch onto origin/main and `git push --force-with-lease` (strict up-to-date + required `validate` check block stale branches). Wait for `validate` to pass.',
        'Merge with the merge-commit flow: `gh pr merge ' + args.branch + ' --merge` (NOT squash/rebase; --admin if needed for the 0-approval ruleset). Confirm MERGED and capture the merge commit SHA + PR URL before returning.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha', 'validatePassed'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
        validatePassed: { type: 'boolean' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 5 — copy SQLite config huntarr-config -> newtarr-config (DESTRUCTIVE on the new PVC).
const configDataMigrationTask = defineTask('config-data-migration', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Carry config forward: copy SQLite state from huntarr-config into the new newtarr-config PVC',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes SRE performing a careful SQLite config migration on the Epaflix cluster',
      task:
        'After the merge, ArgoCD has created the `' + args.newName + '` Deployment and an EMPTY `' + args.newName +
        '-config` PVC. Copy the huntarr config (SQLite + backups) into it so all integrations/state carry over, ' +
        'with the app quiesced for SQLite integrity.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH — prefix with `' + args.masterSsh + ' \'<kubectl ...>\'`. Use the on-disk PVC dirs + node from the discovery inventory in context.',
        'Wait for ArgoCD to reconcile the merge: confirm `' + args.newName + '` Deployment + `' + args.newName + '-config` PVC exist (`kubectl -n ' + args.ns + ' get deploy ' + args.newName + ' pvc ' + args.newName + '-config`). Identify the new PVC\'s bound volume + node + on-disk dir.',
        'QUIESCE for SQLite integrity: temporarily suspend the ' + args.servarrApp + ' App auto-sync so selfHeal does not fight you (argocd app set ' + args.servarrApp + ' --sync-policy none if the argocd CLI is available, else note the method used), then scale the ' + args.newName + ' Deployment to 0 replicas and confirm no pod is writing. The OLD huntarr stays running (do not touch it).',
        'COPY: from the worker node(s), copy huntarr.db, logs.db and the backups/ dir (and any other *.db state files found in discovery) from the huntarr-config on-disk dir into the newtarr-config on-disk dir; remove any stale files newtarr created first; `chown 568:568` everything copied (PUID/PGID 568). If source and dest PVCs are on different nodes, scp via the node hosts. NEVER modify the source huntarr files (copy only).',
        'RESTORE: scale `' + args.newName + '` back to 1 (or re-enable auto-sync `argocd app set ' + args.servarrApp + ' --sync-policy automated` and let selfHeal restore replicas:1). Confirm the newtarr pod comes up Running 1/1 and its logs show the carried config (tracked items count, sonarr/sonarr2 + prowlarr/qbittorrent connections loaded).',
        'Do NOT touch DNS or Authentik yet (old huntarr still serves the public host until cutover).',
        'Return ONLY the structured JSON result with copiedFiles, newtarr pod status, and evidence the config carried (e.g. tracked-items count matching the old instance).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['copiedFiles', 'newtarrRunning', 'configCarried', 'trackedItems', 'integrationsLoaded', 'anomalies', 'summary'],
      properties: {
        copiedFiles: { type: 'array', items: { type: 'string' } },
        newtarrRunning: { type: 'boolean' },
        configCarried: { type: 'boolean' },
        trackedItems: { type: 'string' },
        integrationsLoaded: { type: 'array', items: { type: 'string' } },
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

// PHASE 6 — migrate Authentik SSO app/provider huntarr -> newtarr (runtime, secrets-sensitive).
const authentikMigrateTask = defineTask('authentik-migrate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Migrate the Authentik Application/Proxy-Provider + bindings from huntarr to newtarr',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Authentik administrator migrating an SSO-protected app on the Epaflix cluster',
      task:
        'Recreate the huntarr Authentik objects as newtarr (new external host ' + args.newHost + ', upstream ' +
        'http://' + args.newName + '.' + args.ns + '.svc:' + args.port + '), reattach the outpost + the same group/' +
        'policy bindings, then delete the old huntarr Application + Provider — so SSO is preserved under the new name.',
      context: { ...args },
      instructions: [
        'Use the Authentik API with the admin/api token from .github/instructions/secrets.yml (NEVER print the token). Base the calls on the huntarr objects captured in the discovery inventory (provider mode, external host, group bindings, outpost assignment).',
        'Create a newtarr Proxy Provider mirroring huntarr\'s settings but external host -> https://' + args.newHost + ' and upstream/internal host -> http://' + args.newName + '.' + args.ns + '.svc:' + args.port + ' (forward-auth vs proxy mode: match what huntarr used).',
        'Create a newtarr Application (slug newtarr) bound to the new provider; copy the icon/launch-url; recreate the SAME group/policy bindings that gated huntarr login (e.g. the group from discovery) so the same users keep access.',
        'Attach the newtarr provider to the SAME outpost huntarr used (embedded outpost most likely) so routing works; trigger an outpost refresh.',
        'Verify newtarr is reachable through Authentik internally (the provider resolves the upstream service). Then DELETE the old huntarr Application and Proxy Provider (and detach from the outpost) so no huntarr SSO object remains.',
        'Do NOT change DNS yet. Return ONLY the structured JSON result with created PKs/slugs, deleted huntarr PKs, bindings reattached, and outpost refresh status.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['newtarrApp', 'newtarrProvider', 'bindingsReattached', 'outpostRefreshed', 'huntarrObjectsDeleted', 'anomalies', 'summary'],
      properties: {
        newtarrApp: { type: 'string' },
        newtarrProvider: { type: 'string' },
        bindingsReattached: { type: 'array', items: { type: 'string' } },
        outpostRefreshed: { type: 'boolean' },
        huntarrObjectsDeleted: { type: 'array', items: { type: 'string' } },
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

// PHASE 7 — DNS cutover huntarr.epaflix.com -> newtarr.epaflix.com (Pi-hole + Cloudflare).
const dnsCutoverTask = defineTask('dns-cutover', (args, taskCtx) => ({
  kind: 'agent',
  title: 'DNS cutover: add newtarr host, remove huntarr (Pi-hole dnsmasq + Cloudflare shadow)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'DNS administrator for the Epaflix *.epaflix.com zone',
      task:
        'Make ' + args.newHost + ' resolve to the Traefik LB (' + args.traefikLbIp + ') and remove ' + args.oldHost +
        ', across both Pi-hole (authoritative LAN) and Cloudflare (shadow DNS-only to defeat the proxied wildcard).',
      context: { ...args },
      instructions: [
        'PI-HOLE (192.168.10.30) — golden rule: edit /etc/dnsmasq.d/ files ONLY, never the web UI / custom.list. Using the discovery DNS findings: if huntarr had an explicit entry in /etc/dnsmasq.d/10-epaflix.conf, add the equivalent for ' + args.newHost + ' -> ' + args.traefikLbIp + ' and remove the huntarr line; if it was only covered by the `*.epaflix.com -> ' + args.traefikLbIp + '` wildcard, no per-host add is needed (note this). Reload: `ssh root@192.168.10.30 \'pihole reloaddns\'` (or restart dnsmasq/pihole-FTL as appropriate). Confirm `dig +short ' + args.newHost + ' @192.168.10.30` returns ' + args.traefikLbIp + '.',
        'CLOUDFLARE — the proxied `*.epaflix.com` wildcard hijacks any undefined subdomain, so a LAN-only host needs a DNS-only shadow A record. Using the CF token (from secrets.yml/memory; the live token may live in the ddns-updater TrueNAS app per memory): create `' + args.newName + '` A -> ' + args.traefikLbIp + ', PROXY DISABLED (DNS-only/grey-cloud). Delete the old `huntarr` shadow record if one exists.',
        'Verify external + internal resolution as practical. Do NOT delete any live k8s huntarr resources yet.',
        'Return ONLY the structured JSON result with pihole changes, cloudflare changes, and resolution checks.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['piholeChanged', 'piholeDetail', 'cloudflareChanged', 'cloudflareDetail', 'resolves', 'anomalies', 'summary'],
      properties: {
        piholeChanged: { type: 'boolean' },
        piholeDetail: { type: 'string' },
        cloudflareChanged: { type: 'boolean' },
        cloudflareDetail: { type: 'string' },
        resolves: { type: 'boolean' },
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

// PHASE 8 — delete orphaned live huntarr resources + old PVC (DESTRUCTIVE; prune is OFF).
const cleanupOldTask = defineTask('cleanup-old-huntarr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Delete orphaned live huntarr resources + old config PVC (servarr prune is OFF)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes SRE removing migrated-away orphan resources on the Epaflix cluster',
      task:
        'Now that newtarr is serving with the carried config under the new DNS + Authentik, delete the old, ' +
        'no-longer-in-git huntarr live resources (the servarr App has prune:false, so these are orphans that ' +
        'must be removed manually). Only proceed after confirming newtarr is healthy.',
      context: { ...args },
      instructions: [
        'kubectl over SSH (prefix `' + args.masterSsh + ' \'<kubectl ...>\'`). FIRST re-confirm newtarr is Running 1/1 and the config carried (from context). Abort and report if not.',
        'Delete the orphaned huntarr resources in namespace ' + args.ns + ': Deployment, Service, PDB, and any huntarr IngressRoute. `kubectl -n ' + args.ns + ' delete deploy/' + args.oldName + ' svc/' + args.oldName + ' pdb/' + args.oldName + '-pdb` (use the exact names from discovery; ignore not-found).',
        'Delete the old huntarr-config PVC ONLY after the data copy + newtarr verification are confirmed (the SQLite was COPIED, source still intact): `kubectl -n ' + args.ns + ' delete pvc ' + args.oldName + '-config`. Note local-path volumes: confirm the PV/volume is released; optionally leave the on-disk dir as a short-term backup but state that clearly.',
        'Confirm NO huntarr resources remain: `kubectl -n ' + args.ns + ' get all,pvc,pdb,ingressroute -l app=' + args.oldName + '` returns nothing, and a name grep for huntarr in the namespace is empty.',
        'Return ONLY the structured JSON result with deletedResources, pvcDeleted, and a confirmation that zero huntarr resources remain live.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['deletedResources', 'pvcDeleted', 'zeroHuntarrLive', 'anomalies', 'summary'],
      properties: {
        deletedResources: { type: 'array', items: { type: 'string' } },
        pvcDeleted: { type: 'boolean' },
        zeroHuntarrLive: { type: 'boolean' },
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

// PHASE 9 — full verification incl. ZERO-leftover sweep across git + live + Authentik + DNS.
const verifyWorkTask = defineTask('verify-work', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify newtarr healthy, SSO + config carried, and ZERO huntarr leftovers anywhere',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE doing end-to-end acceptance + zero-leftover verification of the migration',
      task:
        'Prove the migration is complete: newtarr healthy & Synced, SSO login works at ' + args.newHost + ', the ' +
        'prior config/integrations carried over, and NO functional huntarr leftover exists in git, live k8s, ' +
        'Authentik, or DNS.',
      context: { ...args },
      instructions: [
        'kubectl over SSH. Confirm: `' + args.newName + '` Deployment Available, pod Running 1/1; ArgoCD `' + args.servarrApp + '` App Synced + Healthy with auto-sync restored (selfHeal:true).',
        'CONFIG CARRIED: from newtarr logs / its API, confirm the tracked-items state and that the prowlarr, qbittorrent and servarr (Sonarr + Sonarr2) integrations are present and connected (matching pre-migration). Note the [[project_sonarr2_huntarr_race]] interaction (15-min seasons_packs hunt vs Sonarr2 add-search) — confirm behaviour is unchanged.',
        'SSO + DNS: confirm ' + args.newHost + ' resolves to ' + args.traefikLbIp + ' and loads through Authentik (login gated by the same group). Confirm ' + args.oldHost + ' no longer routes to a huntarr backend.',
        'ZERO-LEFTOVER SWEEP — all four must be clean (functional): (1) GIT `grep -rn -i huntarr` from repoRoot — every remaining hit must be a verbatim third-party research snapshot (research/user_config.yaml) or otherwise non-functional; functional/manifest/doc hits MUST be zero. (2) LIVE k8s: no huntarr deploy/svc/pvc/pdb/ingressroute. (3) AUTHENTIK: no huntarr Application/Provider. (4) DNS: no huntarr A record (Pi-hole dnsmasq + Cloudflare). Report each remaining huntarr string with its classification.',
        'Set verified=true ONLY if newtarr healthy + Synced AND config carried AND SSO works AND functional leftovers are zero across all four surfaces. List anomalies precisely.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'newtarrHealthy', 'appSynced', 'configCarried', 'ssoWorks', 'leftoversGit', 'leftoversLive', 'leftoversAuthentik', 'leftoversDns', 'remainingHuntarrStrings', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        newtarrHealthy: { type: 'boolean' },
        appSynced: { type: 'string' },
        configCarried: { type: 'boolean' },
        ssoWorks: { type: 'boolean' },
        leftoversGit: { type: 'number' },
        leftoversLive: { type: 'number' },
        leftoversAuthentik: { type: 'number' },
        leftoversDns: { type: 'number' },
        remainingHuntarrStrings: { type: 'array', items: { type: 'string' } },
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

// PHASE 10 — closeout: close #131, tick PR test plan, open follow-ups.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #131 with outcome, update PR test plan, open follow-up issues',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified migration in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close #' + args.issue + ', tick the PR test-plan checkboxes by EDITING the ' +
        'PR body (never a new comment), and open follow-up gh issues for any deferred items per CLAUDE.md.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on #' + args.issue + ' summarizing the verified result (full rename to newtarr, config carried, Authentik + DNS cut over, old huntarr removed, zero functional leftovers) and CLOSE it.',
        'Edit the PR body (`gh pr edit --body`) to check off the Test Plan items that passed, with observed evidence inline. Do NOT add a separate comment for the test plan.',
        'Follow-ups (CLAUDE.md — open a gh issue for each deferred item, enhancement-issue shape ## Finding/## Current state/## Desired outcome/## Notes, cross-link #' + args.issue + '): (a) re-confirm the [[project_sonarr2_huntarr_race]] behaviour over the next hunt cycles; (b) Newtarr is a fresh fork with low support promise — revisit if a better-maintained successor appears; (c) any anomaly surfaced by verify (e.g. leftover research snapshot, on-disk old PVC dir left as backup). Skip noise issues where nothing is deferred.',
        'Return ONLY the structured JSON result with issueState, prUpdated, followUpIssues (array of URLs).',
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
    repo: 'SpyrosPsarras/epaflix',
    issue: '131',
    branch: 'huntarr-to-newtarr-migration',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    ns: 'servarr',
    servarrApp: 'servarr',
    oldName: 'huntarr',
    newName: 'newtarr',
    newImage: 'ghcr.io/elfhosted/newtarr:rolling',
    oldDir: '2-k3s/08.servarr/huntarr',
    newDir: '2-k3s/08.servarr/newtarr',
    kustomization: '2-k3s/08.servarr/kustomization.yaml',
    appServarrManifest: '2-k3s/11.argocd/apps/app-servarr.yaml',
    port: '30262',
    metricsPort: '9705',
    oldHost: 'huntarr.epaflix.com',
    newHost: 'newtarr.epaflix.com',
    traefikLbIp: '192.168.10.101',
    authentikNs: 'authentik',
    ...inputs,
  };

  ctx.log('info', 'huntarr -> newtarr full rename migration (#131): discover -> plan[BP] -> author -> merge[BP] -> data-copy[BP] -> authentik[BP] -> dns[BP] -> cleanup[BP] -> verify -> closeout');

  // PHASE 1 — discover full footprint.
  const inv = await ctx.task(discoverRuntimeTask, {
    repoRoot: cfg.repoRoot, masterSsh: cfg.masterSsh, ns: cfg.ns, oldName: cfg.oldName, newName: cfg.newName,
    oldHost: cfg.oldHost, traefikLbIp: cfg.traefikLbIp,
  });
  ctx.log('info', `Discovery: gitHits=${(inv.gitHits || []).length}; sqlite=${JSON.stringify(inv.sqliteFiles)}; authentik=${JSON.stringify(inv.authentik)}`);

  // PHASE 2 — plan, with a refine/review loop.
  let plan, lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    plan = await ctx.task(planMigrationTask, {
      ...cfg, inventory: inv, feedback: lastFeedback || undefined, attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const gate = await ctx.breakpoint({
      question:
        'Review the huntarr -> newtarr migration PLAN (#' + cfg.issue + ') before ANY change.\n\n' +
        'Git change set: ' + (plan.gitChangeSet || []).length + ' items\n' +
        'Runtime steps: ' + JSON.stringify(plan.runtimeSteps) + '\n' +
        'Config copy: ' + JSON.stringify(plan.configCopyCommands) + '\n' +
        'Authentik: ' + JSON.stringify(plan.authentikSteps) + '\n' +
        'DNS: ' + JSON.stringify(plan.dnsSteps) + '\n' +
        'Cleanup: ' + JSON.stringify(plan.cleanupSteps) + '\n' +
        'Leftover checks: ' + JSON.stringify(plan.leftoverChecks) + '\n' +
        'Risks: ' + JSON.stringify(plan.risks) + '\n\n' +
        'Summary: ' + plan.summary + '\n\nApprove this plan?',
      options: ['Approve plan', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['plan-gate', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (gate.approved && !(gate.response || '').toLowerCase().includes('change')) break;
    if (!gate.approved && (gate.response || '').toLowerCase().includes('abort')) {
      ctx.log('warn', 'Plan aborted by owner.');
      return { success: false, reason: 'plan-aborted', feedback: gate.response || gate.feedback || '', inventory: inv, plan };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
  }

  // PHASE 3 — author the git rename + local commit.
  const change = await ctx.task(authorManifestsTask, { ...cfg, approvedPlan: plan, inventory: inv });
  ctx.log('info', `Authored: branch=${change.branch} commit=${change.commitSha} renderOk=${change.renderOk}`);

  // GATE — deploy (push + PR + merge). Merging creates live newtarr.
  const deployGate = await ctx.breakpoint({
    question:
      'Approve DEPLOY of the git rename (#' + cfg.issue + ')? This pushes branch `' + change.branch + '`, opens a PR, ' +
      'rebases, waits for `validate`, and MERGES (merge-commit). ArgoCD then creates the live newtarr.\n\n' +
      'Render OK: ' + change.renderOk + ' (' + change.renderNotes + ')\n' +
      'Renamed paths: ' + JSON.stringify(change.renamedPaths) + '\n' +
      'Changed files: ' + JSON.stringify(change.changedFiles) + '\n\n' +
      'Diff (truncated):\n' + (change.diff || '').slice(0, 4000) + '\n\nProceed to merge?',
    options: ['Approve deploy + merge', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'approval-gate'],
  });
  if (!deployGate.approved) {
    ctx.log('warn', 'Deploy not approved — branch+commit exist locally, nothing pushed.');
    return { success: false, merged: false, reason: 'deploy-not-approved', branch: change.branch, feedback: deployGate.response || '' };
  }

  // PHASE 4 — push + PR + merge.
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; validate=${pub.validatePassed}; PR=${pub.prUrl}`);
  if (!pub.merged) {
    return { success: false, merged: false, reason: 'merge-failed', prUrl: pub.prUrl, validatePassed: pub.validatePassed };
  }

  // GATE — destructive config copy.
  const copyGate = await ctx.breakpoint({
    question:
      'Approve the CONFIG DATA COPY (DESTRUCTIVE on the new empty PVC)? newtarr will be quiesced (auto-sync ' +
      'suspended + scaled to 0), the SQLite state copied from huntarr-config into newtarr-config, then restored. ' +
      'The OLD huntarr keeps serving. Source files are never modified. Proceed?',
    options: ['Approve config copy', 'Skip (start newtarr fresh)', 'Abort'],
    expert: 'owner',
    tags: ['destructive', 'data-migration', 'approval-gate'],
  });
  let data = { configCarried: false, copiedFiles: [], newtarrRunning: null, trackedItems: 'n/a', integrationsLoaded: [], anomalies: ['copy-skipped'], summary: 'skipped' };
  if (copyGate.approved && !(copyGate.response || '').toLowerCase().includes('abort')) {
    if ((copyGate.response || '').toLowerCase().includes('skip')) {
      ctx.log('warn', 'Owner chose to start newtarr fresh — no config carried.');
    } else {
      data = await ctx.task(configDataMigrationTask, { ...cfg, inventory: inv });
      ctx.log('info', `Config copy: carried=${data.configCarried} running=${data.newtarrRunning} items=${data.trackedItems}`);
    }
  } else {
    return { success: false, merged: true, prUrl: pub.prUrl, reason: 'config-copy-aborted' };
  }

  // GATE — Authentik (secrets/SSO).
  const authGate = await ctx.breakpoint({
    question:
      'Approve the AUTHENTIK SSO migration? Recreate the huntarr Application + Proxy Provider as newtarr (host ' +
      cfg.newHost + ', upstream newtarr.' + cfg.ns + '.svc:' + cfg.port + '), reattach the same group bindings + ' +
      'outpost, then delete the old huntarr Authentik objects. Uses the Authentik admin token (secrets). Proceed?',
    options: ['Approve Authentik migration', 'Skip', 'Abort'],
    expert: 'owner',
    tags: ['secrets', 'sso', 'approval-gate'],
  });
  let auth = { huntarrObjectsDeleted: [], bindingsReattached: [], outpostRefreshed: false, anomalies: ['authentik-skipped'], summary: 'skipped', newtarrApp: 'n/a', newtarrProvider: 'n/a' };
  if (authGate.approved && !(authGate.response || '').toLowerCase().includes('abort')) {
    if (!(authGate.response || '').toLowerCase().includes('skip')) {
      auth = await ctx.task(authentikMigrateTask, { ...cfg, inventory: inv });
      ctx.log('info', `Authentik: app=${auth.newtarrApp} deletedHuntarr=${JSON.stringify(auth.huntarrObjectsDeleted)}`);
    } else { ctx.log('warn', 'Authentik migration skipped by owner.'); }
  } else {
    return { success: false, merged: true, prUrl: pub.prUrl, reason: 'authentik-aborted', data };
  }

  // GATE — DNS cutover.
  const dnsGate = await ctx.breakpoint({
    question:
      'Approve the DNS CUTOVER? Add ' + cfg.newHost + ' -> ' + cfg.traefikLbIp + ' (Pi-hole dnsmasq, golden-rule ' +
      'files-only) + Cloudflare DNS-only shadow record, and remove the huntarr equivalents. Proceed?',
    options: ['Approve DNS cutover', 'Skip', 'Abort'],
    expert: 'owner',
    tags: ['dns', 'approval-gate'],
  });
  let dns = { piholeChanged: false, cloudflareChanged: false, resolves: false, anomalies: ['dns-skipped'], summary: 'skipped', piholeDetail: '', cloudflareDetail: '' };
  if (dnsGate.approved && !(dnsGate.response || '').toLowerCase().includes('abort')) {
    if (!(dnsGate.response || '').toLowerCase().includes('skip')) {
      dns = await ctx.task(dnsCutoverTask, { ...cfg, inventory: inv });
      ctx.log('info', `DNS: pihole=${dns.piholeChanged} cf=${dns.cloudflareChanged} resolves=${dns.resolves}`);
    } else { ctx.log('warn', 'DNS cutover skipped by owner.'); }
  } else {
    return { success: false, merged: true, prUrl: pub.prUrl, reason: 'dns-aborted', data, auth };
  }

  // GATE — delete old huntarr (destructive). Only with newtarr confirmed up.
  const cleanupGate = await ctx.breakpoint({
    question:
      'Approve DELETING the orphaned live huntarr resources + old config PVC (DESTRUCTIVE)? servarr prune is OFF ' +
      'so these must be removed manually. newtarr running=' + data.newtarrRunning + ', configCarried=' +
      data.configCarried + '. The old SQLite was COPIED (source intact). Proceed only if newtarr is healthy.',
    options: ['Approve delete old huntarr', 'Skip (leave orphans)', 'Abort'],
    expert: 'owner',
    tags: ['destructive', 'approval-gate'],
  });
  let cleanup = { deletedResources: [], pvcDeleted: false, zeroHuntarrLive: false, anomalies: ['cleanup-skipped'], summary: 'skipped' };
  if (cleanupGate.approved && !(cleanupGate.response || '').toLowerCase().includes('abort')) {
    if (!(cleanupGate.response || '').toLowerCase().includes('skip')) {
      cleanup = await ctx.task(cleanupOldTask, { ...cfg, configCarried: data.configCarried, newtarrRunning: data.newtarrRunning, inventory: inv });
      ctx.log('info', `Cleanup: deleted=${JSON.stringify(cleanup.deletedResources)} pvcDeleted=${cleanup.pvcDeleted}`);
    } else { ctx.log('warn', 'Old huntarr left in place by owner choice.'); }
  }

  // PHASE 9 — verify everything + zero-leftover sweep, with an anomaly gate.
  let verify = await ctx.task(verifyWorkTask, { ...cfg });
  if (!verify.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Final verification found issues.\n' +
        'newtarr healthy: ' + verify.newtarrHealthy + '; Synced: ' + verify.appSynced + '\n' +
        'config carried: ' + verify.configCarried + '; SSO: ' + verify.ssoWorks + '\n' +
        'leftovers git/live/authentik/dns: ' + verify.leftoversGit + '/' + verify.leftoversLive + '/' + verify.leftoversAuthentik + '/' + verify.leftoversDns + '\n' +
        'remaining huntarr strings: ' + JSON.stringify(verify.remainingHuntarrStrings) + '\n' +
        'anomalies: ' + JSON.stringify(verify.anomalies) + '\n' +
        'summary: ' + verify.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      verify = await ctx.task(verifyWorkTask, { ...cfg, attempt: 2 });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, merged: true, prUrl: pub.prUrl, reason: 'verification-stop', verify, data, auth, dns, cleanup };
    }
  }

  // PHASE 10 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, prUrl: pub.prUrl,
    verify, data, auth, dns, cleanup,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    merged: pub.merged,
    prUrl: pub.prUrl,
    configCarried: data.configCarried,
    authentikMigrated: (auth.huntarrObjectsDeleted || []).length > 0,
    dnsCutover: dns.piholeChanged || dns.cloudflareChanged,
    oldResourcesRemoved: cleanup.zeroHuntarrLive,
    leftoversZero: verify.verified && verify.leftoversLive === 0 && verify.leftoversAuthentik === 0 && verify.leftoversDns === 0,
    issueState: close.issueState,
    followUpIssues: close.followUpIssues,
  };
}

/**
 * @process specializations/devops-sre-platform/cleanuparr-nfs-unification
 * @description Issue #195 (Option A, deferred from #142). Collapse the FOUR separate NFS exports
 *   of the single ZFS dataset `pool1/dataset01` (animes / downloads / movies / tvshows) into ONE
 *   unified NFS export of `/mnt/pool1/dataset01`, so intra-export hardlinks work (today every export
 *   has its own fsid -> cross-export link() returns EXDEV -> every imported seeder is nlink=1,
 *   indistinguishable from a true orphan -> the Cleanuparr unlinked/orphan reaper must stay OFF).
 *
 *   FULL remap (owner-chosen) executed LIVE end-to-end:
 *     1. TrueNAS: single export of /mnt/pool1/dataset01 (added ALONGSIDE the 4 old exports first —
 *        recovery path before cutover; old exports removed only at the very end).
 *     2. k3s workers: unified node mount (added alongside the 4 old mounts, one node at a time,
 *        health-gated).
 *     3. GitOps: unified media PV/PVC + remap qbt / sonarr / sonarr2 / radarr volumeMounts to the
 *        unified export (subPaths), mount the library roots READ-ONLY into Cleanuparr; via branch+PR
 *        (rebase -> validate -> --merge per repo merge policy) so ArgoCD selfHeal applies it.
 *     4. In-app: remap qbt save path + Sonarr/Sonarr2/Radarr root folders, set copyUsingHardlinks=true;
 *        prove a real hardlink now spans downloads<->library (nlink=2, same fsid).
 *     5. Cleanuparr unlinked rule: enable in DRY-RUN first, verify ZERO false positives against the
 *        ~139 live seeders, then ARM.
 *     6. Remove the 4 old exports + old node mounts; update docs; open follow-ups; close #195.
 *
 *   Live-change risk: mutates the storage path EVERY media app depends on (TrueNAS exports, k3s-worker
 *   fstab, media PV/PVC, qbt+*arr in-app config) and finally enables a delete-with-data reaper. Every
 *   destructive / deploy step is gated by an owner breakpoint. Recovery path is built before cutover
 *   and validated; old paths are torn down only after the new path is proven.
 *
 * @inputs { repoRoot, repo, namespace, truenasHost, dataset, exportRoot, workerNodes, branch }
 * @outputs { success, unifiedExport, hardlinkProven, reaperArmed, falsePositives, issueClosed, prUrls, summary }
 *
 * @agent general-purpose (ssh TrueNAS/nodes, kubectl/exec, qbt+*arr API, git/gh, classification + adversarial verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 0 — capture live baseline + author the EXACT remap + rollback plan (read-only)
// ---------------------------------------------------------------------------
const captureDesignTask = defineTask('capture-design', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Capture live storage/servarr baseline + author exact remap design + rollback plan (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage/Kubernetes SRE on the Epaflix k3s cluster planning a live NFS-export unification (issue #195)',
      task:
        'Capture the COMPLETE current live state of the media storage path and author the exact, ordered, reversible ' +
        'remap plan + rollback plan for collapsing the 4 NFS exports of ' + args.dataset + ' into ONE export of ' +
        args.exportRoot + '. DO NOT change anything — strictly read-only.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: gather hard evidence, never assume.',
        'TrueNAS (' + args.truenasHost + ', ssh truenas_admin@' + args.truenasHost + '): enumerate the EXACT current NFS exports of ' +
          args.dataset + ' and its children (animes/downloads/movies/tvshows) — use `midclt call sharing.nfs.query` and `exportfs -v` / ' +
          '`cat /etc/exports*`. Record each export id, path, networks/hosts allowed, maproot/mapall, security, and fsid if pinned. ' +
          'Confirm all 4 children are the SAME underlying ZFS dataset ' + args.dataset + ' (so a parent export of ' + args.exportRoot + ' covers them with one fsid).',
        'k3s workers (' + JSON.stringify(args.workerNodes) + ', ssh ubuntu@<ip>): on EACH, capture the current media NFS mounts — ' +
          '`findmnt -t nfs,nfs4 -o TARGET,SOURCE,FSTYPE,OPTIONS` and the matching `/etc/fstab` lines for /mnt/k3s-animes, /mnt/k3s-tvshows, ' +
          '/mnt/k3s-movies, /mnt/k3s-downloads. Record server:path, mount opts (uid/gid 568), and which nodes actually mount media (note any that do not).',
        'k8s media storage: read ' + args.repoRoot + '/2-k3s/08.servarr/_shared/storage/media-pvcs.yaml (4 hostPath PV/PVC: animes 500Gi /mnt/k3s-animes, ' +
          'tvshows 500Gi /mnt/k3s-tvshows, movies 2Ti /mnt/k3s-movies, downloads 1Ti /mnt/k3s-downloads) and the consumer manifests ' +
          'qbittorrent/sonarr/sonarr2/radarr/cleanuparr (volumeMounts + claimName). Record current in-container mount paths (/downloads, /tv, /animes, /movies, cleanuparr /data).',
        'In-app config (read-only via each app API; X-Api-Key via `kubectl -n ' + args.namespace + ' exec deploy/<app> -- cat /config/config.xml`): ' +
          'record qBittorrent default save path + per-category save paths (qbt WebUI API), and Sonarr/Sonarr2/Radarr root folders (`/api/v3/rootfolder`) ' +
          'and current `copyUsingHardlinks` value (`/api/v3/config/mediamanagement`). Count current torrents by state and record the live SEEDER count (expected ~139).',
        'BASELINE EXDEV PROOF: exec into sonarr (or a node) and attempt a hardlink from a downloads file to its library path across the current mounts; ' +
          'confirm it fails with EXDEV today (capture the error). This is the before-state we will invert.',
        'DESIGN the unified layout: ONE TrueNAS export of ' + args.exportRoot + '; ONE node mount (propose mountpoint e.g. /mnt/k3s-media) on each media worker; ' +
          'unified media PV/PVC backed by that mount; consumers mount it with subPath so downloads/tvshows/movies/animes all live UNDER ONE export (same fsid). ' +
          'Decide and state the EXACT new in-container path mapping and the EXACT new qbt save path + each *arr root folder string (FULL remap is owner-chosen). ' +
          'If the new in-container paths differ from the old ones, the *arr DB stores absolute per-file paths — specify exactly how root folders get repointed ' +
          '(Sonarr/Radarr root-folder edit + series/movie bulk root-folder move that REWRITES DB paths without moving files on disk, since the bytes are identical ' +
          'under the new export) and how qbt existing torrents get their save path corrected (set location, no data move). Flag this DB-path-rewrite as the top risk.',
        'ROLLBACK plan: because the unified export is ADDED alongside the 4 old exports and the unified node mount is ADDED alongside the old mounts, every step ' +
          'before the final teardown is reversible by reverting the manifest PR + ArgoCD re-sync and pointing back at the old mounts. State the precise rollback for ' +
          'EACH phase (TrueNAS, node mount, manifest cutover, in-app remap, reaper arm).',
        'Produce the exact ordered runbook (commands per phase) + the rollback plan + the risk list. Save raw captures under tasks/' + taskCtx.effectId + '/.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['truenasExports', 'nodeMounts', 'k8sStorage', 'inAppConfig', 'seederCount', 'exdevProven', 'pathMapping', 'runbook', 'rollbackPlan', 'risks', 'summary'],
      properties: {
        truenasExports: { type: 'array', items: { type: 'object' } },
        nodeMounts: { type: 'array', items: { type: 'object' } },
        k8sStorage: { type: 'object' },
        inAppConfig: { type: 'object' },
        seederCount: { type: 'number' },
        exdevProven: { type: 'boolean' },
        pathMapping: { type: 'object' },
        runbook: { type: 'array', items: { type: 'object' } },
        rollbackPlan: { type: 'array', items: { type: 'object' } },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 0b — refine the design after owner feedback (read-only)
const refineDesignTask = defineTask('refine-design', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Refine the remap/rollback design per owner feedback (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage SRE refining the #195 unification plan after owner review',
      task: 'Revise the prior design to address the owner feedback. Read-only; no changes applied.',
      context: { ...args },
      instructions: [
        'Owner feedback to address: ' + (args.feedback || '(none)'),
        'Re-verify any live facts the feedback questions; keep the additive/recovery-first ordering.',
        'Return the SAME schema as capture-design with the revised pathMapping/runbook/rollbackPlan/risks.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['truenasExports', 'nodeMounts', 'k8sStorage', 'inAppConfig', 'seederCount', 'exdevProven', 'pathMapping', 'runbook', 'rollbackPlan', 'risks', 'summary'],
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 1 — build the RECOVERY PATH first: unified export + unified node mounts (live, ADDITIVE)
// ---------------------------------------------------------------------------
const buildNewPathTask = defineTask('build-new-path', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Add unified NFS export + unified node mounts ALONGSIDE existing (live, additive, health-gated)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage SRE building the new unified storage path without disturbing the live one',
      task:
        'Create the unified NFS export of ' + args.exportRoot + ' on TrueNAS and add the unified node mount on each media k3s worker, ' +
        'ALONGSIDE the existing 4 exports/mounts. Do NOT remove anything old. Additive + reversible only.',
      context: { ...args },
      instructions: [
        'Follow the APPROVED runbook exactly: ' + JSON.stringify(args.runbook),
        'TrueNAS (ssh truenas_admin@' + args.truenasHost + '): add ONE NFS export of ' + args.exportRoot + ' with the SAME networks/hosts/maproot ' +
          '(uid/gid 568) as the existing exports (use `midclt call sharing.nfs.create ...`). Keep the 4 existing exports in place. If TrueNAS refuses a ' +
          'nested/parent export while children exist, capture the exact error and STOP (report — do not force-remove children yet).',
        'On EACH media worker ' + JSON.stringify(args.workerNodes) + ', ONE AT A TIME (health-gated): add the unified mount at the approved mountpoint ' +
          '(e.g. /mnt/k3s-media) for ' + args.truenasHost + ':' + args.exportRoot + ' with the same opts (uid/gid 568), add the fstab line, `mount` it, and verify ' +
          '`findmnt` + read/write probe. Confirm the node + its k3s pods stay Ready (`kubectl get nodes`, `kubectl -n ' + args.namespace + ' get pods -o wide`) ' +
          'before moving to the next node. Do NOT touch the existing /mnt/k3s-* mounts.',
        'PROVE the new path supports hardlinks: on the unified mount, create a temp file under .../downloads and `ln` it to .../tvshows (or animes) — confirm nlink=2 ' +
          'and SAME device (no EXDEV). Remove the temp files. This is the core proof the unification fixes the EXDEV barrier.',
        'If any step fails, STOP, leave old paths fully intact, and report applied-vs-not + how to revert the partial new path.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['exportCreated', 'exportId', 'nodesMounted', 'hardlinkProvenOnNewPath', 'nodesHealthy', 'appliedSteps', 'summary'],
      properties: {
        exportCreated: { type: 'boolean' },
        exportId: { type: ['string', 'number', 'null'] },
        nodesMounted: { type: 'array', items: { type: 'string' } },
        hardlinkProvenOnNewPath: { type: 'boolean' },
        nodesHealthy: { type: 'boolean' },
        appliedSteps: { type: 'array', items: { type: 'object' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — author GitOps manifest remap on a branch + open PR (git, NOT merged yet)
// ---------------------------------------------------------------------------
const manifestRemapTask = defineTask('manifest-remap', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author unified media PV/PVC + remap consumer volumeMounts + Cleanuparr RO library mounts; branch + kustomize build + open PR (no merge)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps SRE authoring the manifest cutover for the unified media export',
      task:
        'On a fresh branch off origin/main, change the servarr manifests so all media consumers bind the UNIFIED export, mount the library ' +
        'roots READ-ONLY into Cleanuparr, validate with kustomize build, and open a PR. DO NOT MERGE — the merge is a separate gated step.',
      context: { ...args },
      instructions: [
        'Branch: `git fetch origin && git switch -c ' + args.branch + ' origin/main` in ' + args.repoRoot + '.',
        'Per the approved pathMapping (' + JSON.stringify(args.pathMapping) + '): rewrite 2-k3s/08.servarr/_shared/storage/media-pvcs.yaml to a UNIFIED media ' +
          'PV/PVC backed by the unified node mount (e.g. hostPath /mnt/k3s-media), preserving capacity/StorageClass/Retain semantics. Remap volumeMounts in ' +
          'qbittorrent/sonarr/sonarr2/radarr to the unified PVC using subPath (downloads/tvshows/movies/animes) so in-container paths match the approved mapping.',
        'CLEANUPARR: mount the library roots (tvshows, movies, animes) READ-ONLY into the Cleanuparr container in ADDITION to /data (downloads), via subPath of the ' +
          'unified PVC. This is what lets the unlinked detector see real hardlinks. Do NOT enable the unlinked rule here (that is in-app DB config, done later in dry-run).',
        'Update 2-k3s/08.servarr/kustomization.yaml resource list if PV/PVC resource names changed. Keep ServerSideApply behaviour intact.',
        'VALIDATE: run the repo CI-equivalent `kustomize build 2-k3s/08.servarr` (use the pinned kustomize per the repo) and confirm it renders clean. Also confirm no ' +
          'plaintext kind: Secret was introduced (pre-commit guard). Do NOT add .a5c/.history/secrets.',
        'Commit (conventional message, body explaining #195 unification) ending with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. ' +
          'Push and `gh pr create` against SpyrosPsarras/epaflix with a Test plan checklist; PR body ends with the "Generated with Claude Code" line. DO NOT MERGE.',
        'Return ONLY the structured JSON result (include prUrl + branch + kustomizeBuildPassed + the list of files changed).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'branch', 'filesChanged', 'kustomizeBuildPassed', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        kustomizeBuildPassed: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — merge PR -> ArgoCD sync -> in-app remap -> PROVE real hardlink (live cutover)
// ---------------------------------------------------------------------------
const cutoverTask = defineTask('cutover', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Merge PR (rebase->validate->merge), let ArgoCD sync onto unified export, remap qbt+*arr, set copyUsingHardlinks, prove nlink=2',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps SRE executing the live cutover to the unified media export',
      task:
        'Merge the manifest PR per the repo merge policy, confirm ArgoCD reconciles the servarr Application Healthy with pods bound to the unified export, ' +
        'remap the in-app save path + root folders, enable copyUsingHardlinks, and PROVE a real import now hardlinks across downloads<->library.',
      context: { ...args },
      instructions: [
        'MERGE per repo policy: on branch ' + args.branch + ' `git fetch origin && git rebase origin/main && git push --force-with-lease`; wait for the required ' +
          '`validate` check to pass (`gh pr checks ' + args.prUrl + ' --watch`); then `gh pr merge ' + args.prUrl + ' --merge`.',
        'Wait for ArgoCD: the `servarr` Application (selfHeal=true) must go Synced + Healthy. Watch `kubectl -n servarr get pods -o wide` until qbittorrent/sonarr/' +
          'sonarr2/radarr/cleanuparr roll to new pods that mount the unified PVC. Verify inside a pod that the in-container media paths resolve to the unified export ' +
          '(same fsid via `stat -f` across downloads + a library dir).',
        'IN-APP REMAP (FULL remap, per approved pathMapping): set qBittorrent default + per-category save paths to the new path; in Sonarr/Sonarr2/Radarr repoint the ' +
          'root folders and, if in-container paths changed, perform the root-folder move that REWRITES the DB paths WITHOUT moving bytes (files are identical under the ' +
          'new export). Set `copyUsingHardlinks=true` in each *arr media management config. For existing qbt torrents, correct their save location (no data move).',
        'VERIFY library integrity after the DB path rewrite: spot-check several series/movies still resolve to real files (no "missing files" storm); confirm *arr health ' +
          'checks are clean and no mass file-not-found.',
        'PROVE THE FIX: trigger (or simulate) an import so a torrent in downloads gets hardlinked into a library root, and confirm the resulting library file has nlink>=2 ' +
          'and SAME device as the download (the EXDEV barrier is gone). Capture the evidence.',
        'If merge cannot complete cleanly, or ArgoCD does not reach Healthy, or library integrity fails: STOP, do not proceed to reaper, and report exactly where it ' +
          'stopped + the rollback step (revert PR / re-point root folders).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'argoHealthy', 'podsOnUnifiedExport', 'inAppRemapped', 'copyUsingHardlinks', 'libraryIntegrityOk', 'realHardlinkProven', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        argoHealthy: { type: 'boolean' },
        podsOnUnifiedExport: { type: 'boolean' },
        inAppRemapped: { type: 'boolean' },
        copyUsingHardlinks: { type: 'boolean' },
        libraryIntegrityOk: { type: 'boolean' },
        realHardlinkProven: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3-REDESIGN — subPath could not hardlink in-pod (kubelet materializes each
// subPath as a SEPARATE NFS submount -> link(2) EXDEV across submounts). Forward-fix to
// the owner's literal "full remap": mount the unified PVC ONCE per pod at a single root
// (/media, NO subPath) so downloads+library are subdirs under ONE mount, then rewrite the
// *arr root folders + qbt save path (DB-path rewrite, no byte move). This is the top-risk step.
// ---------------------------------------------------------------------------
const redesignManifestTask = defineTask('redesign-manifest', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author single-root /media mount PR (no subPath) for all media consumers; kustomize build; open PR (no merge)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps SRE fixing the subPath EXDEV defect by mounting the unified export at a single root',
      task:
        'On a fresh branch off origin/main, change the 5 media consumers to mount the unified PVC servarr-media at ONE mountPath /media with NO subPath, so ' +
        'downloads/tvshows/animes/movies are subdirs UNDER one mount (hardlinks then work in-pod). Validate with kustomize build and open a PR. DO NOT MERGE.',
      context: { ...args },
      instructions: [
        'Branch: `git fetch origin && git switch -c ' + args.redesignBranch + ' origin/main` in ' + args.repoRoot + '.',
        'For qbittorrent/sonarr/sonarr2/radarr/cleanuparr: replace the per-role subPath volumeMounts with a SINGLE volumeMount of claim servarr-media at mountPath ' +
          '/media (no subPath). Remove the old /downloads,/tv,/animes,/movies,/data,/tv,/animes,/movies mounts for these 5. Keep the config PVC + other mounts (wireguard etc) intact.',
        'The in-container layout becomes: /media/downloads, /media/tvshows, /media/animes, /media/movies (these are the real subdir names under the export root). ' +
          'Cleanuparr mounts /media (whole tree) — it needs to read library to verify nlink and acts on downloads via qbt API, so a single RW /media mount is acceptable; ' +
          'note in the PR that cleanuparr does not delete library files on disk.',
        'Keep the unified servarr-media PV/PVC and the old 4 PV/PVC (rollback) exactly as they are in media-pvcs.yaml.',
        'VALIDATE: `kustomize build 2-k3s/08.servarr` renders clean (use the same render-workaround as PR #239 if ksops blocks it: strip only the generators: line on a copy). ' +
          'Confirm the 5 consumers now reference claim servarr-media at /media with no subPath, and no plaintext kind: Secret was introduced.',
        'Commit (conventional, e.g. `fix(servarr): mount unified media export at single /media root to enable in-pod hardlinks (#195, #240)`) with a body explaining the ' +
          'kubelet subPath EXDEV defect and that the *arr root folders + qbt save path will be repointed to /media/* (DB-path rewrite, no byte move) in the cutover. End body with ' +
          '`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Stage only intended manifests (no .a5c/.history/secrets/artifacts).',
        'Push and `gh pr create` (base main) with a ## Test plan checklist and the body ending `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. DO NOT MERGE.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'branch', 'filesChanged', 'kustomizeBuildPassed', 'newPaths', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        kustomizeBuildPassed: { type: 'boolean' },
        newPaths: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

const redesignCutoverTask = defineTask('redesign-cutover', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Merge single-root PR, ArgoCD sync, rewrite *arr root folders + qbt save path to /media/* (DB-only), prove in-pod hardlink nlink=2',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps/Servarr SRE executing the single-root cutover + DB-path rewrite (the top-risk step)',
      task:
        'Merge the single-root PR, let ArgoCD reconcile pods onto the /media mount, then repoint the *arr root folders and qbt save path to /media/* WITHOUT moving bytes ' +
        '(files already exist under /media), verify library integrity, and PROVE a real import now hardlinks IN-POD (nlink>=2).',
      context: { ...args },
      instructions: [
        'MERGE per repo policy: branch ' + args.redesignBranch + ' -> `git fetch origin && git rebase origin/main && git push --force-with-lease`; wait required `validate` ' +
          '(`gh pr checks ' + args.redesignPrUrl + ' --watch`); then `gh pr merge ' + args.redesignPrUrl + ' --merge`.',
        'Wait for ArgoCD servarr Synced+Healthy; all 5 pods roll to NEW pods with a single /media mount. Confirm `kubectl -n servarr exec deploy/sonarr -- ls /media` shows ' +
          'downloads tvshows animes movies, and `stat -c %D /media/downloads /media/tvshows` MATCH (one mount, one device).',
        'SNAPSHOT SAFETY: confirm the ZFS snapshot pool1/dataset01@pre-unify-issue195 still exists (rollback). Back up each *arr DB is not required (DB-only path edit is reversible), ' +
          'but capture current root folders + a sample of series/movie paths before editing.',
        'DB-PATH REWRITE (no byte move; bytes already at the same files under /media): for SONARR add root folder /media/tvshows, then bulk-edit ALL series Root Folder -> /media/tvshows ' +
          'with MOVE FILES UNCHECKED (DB-only repoint; Sonarr accepts because files already exist there), then remove the old /tv root folder. RADARR: same with /media/movies. ' +
          'SONARR2: same with /media/animes. Verify via /api/v3 that series/movies now resolve hasFile=true at the new paths (no missing-files storm). copyUsingHardlinks stays true on all three.',
        'QBITTORRENT: set default save path to /media/downloads and per-category paths to /media/downloads/<cat>; for existing torrents use Set Location to /media/downloads/<cat> ' +
          '(metadata-only, files already present) so seeding continues. AutoTMM: keep behaviour consistent so it does not try to move data; verify torrents stay in seeding state ' +
          '(seeder count ~' + args.seederCount + ', errored=0).',
        'PROVE THE FIX IN-POD: in the sonarr pod, `ln /media/downloads/<real file> /media/tvshows/.probe195` -> expect SUCCESS, nlink>=2, same %D; rm the probe. Then trigger (or simulate) ' +
          'a real Sonarr import and confirm the resulting library file has nlink>=2. Capture evidence.',
        'If merge fails, ArgoCD not Healthy, library integrity breaks, or torrents go errored: STOP, report exactly where, and give the rollback (revert this PR + revert #239 -> pods ' +
          'return to old claims; re-point root folders back to /tv,/animes,/movies; qbt set-location back to /downloads). Do not improvise.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'argoHealthy', 'podsOnUnifiedExport', 'inAppRemapped', 'copyUsingHardlinks', 'libraryIntegrityOk', 'realHardlinkProven', 'seederCountAfter', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        argoHealthy: { type: 'boolean' },
        podsOnUnifiedExport: { type: 'boolean' },
        inAppRemapped: { type: 'boolean' },
        copyUsingHardlinks: { type: 'boolean' },
        libraryIntegrityOk: { type: 'boolean' },
        realHardlinkProven: { type: 'boolean' },
        seederCountAfter: { type: 'number' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 4 — Cleanuparr unlinked rule in DRY-RUN, verify ZERO false positives (live, non-destructive)
// ---------------------------------------------------------------------------
const dryRunTask = defineTask('dry-run', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Enable Cleanuparr unlinked rule in DRY-RUN; verify zero false positives vs the live seeders',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE validating the unlinked reaper before arming it',
      task:
        'With the library roots now mounted read-only into Cleanuparr, enable the unlinked/orphan rule in DRY-RUN mode (no deletions), run it, and prove it would ' +
        'delete ZERO legitimate seeders. The reaper must remain non-destructive until this passes.',
      context: { ...args },
      instructions: [
        'Back up the Cleanuparr DBs in-pod first (cp cleanuparr.db/events.db to timestamped .bak).',
        'Enable the DownloadCleaner unlinked/orphan rule in DRY-RUN (delete disabled / report-only) per the Cleanuparr config (edit cleanuparr.db unlinked_configs ' +
          'or the UI). Ensure it sees the read-only library mounts and uses nlink correctly. Trigger a run.',
        'Cross-check the dry-run candidate list against the live qbt seeders (~' + args.seederCount + ' expected): EVERY currently-seeding/wanted torrent must NOT appear ' +
          'as an unlinked candidate (because real hardlinks now make them nlink>=2). Any seeder appearing as a candidate is a FALSE POSITIVE and BLOCKS arming.',
        'Report the candidate count, the falsePositive count (must be 0 to arm), and a sample of what it WOULD remove (true orphans are acceptable candidates).',
        'Do NOT arm/delete anything in this phase. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['dryRunEnabled', 'candidateCount', 'falsePositives', 'falsePositiveSamples', 'safeToArm', 'summary'],
      properties: {
        dryRunEnabled: { type: 'boolean' },
        candidateCount: { type: 'number' },
        falsePositives: { type: 'number' },
        falsePositiveSamples: { type: 'array', items: { type: 'object' } },
        safeToArm: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 5 — ARM the reaper (live, destructive: enables delete-with-data)
// ---------------------------------------------------------------------------
const armTask = defineTask('arm-reaper', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Arm the Cleanuparr unlinked/orphan reaper (live) + verify healthy',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE arming the approved unlinked reaper',
      task: 'Flip the unlinked/orphan rule from dry-run to ARMED (deletions enabled) exactly as approved. Preserve all seeding/ratio guards.',
      context: { ...args },
      instructions: [
        'Set the DownloadCleaner unlinked/orphan rule to live (delete enabled). Keep the existing seeding-ratio / seed-time guards intact so it never removes a wanted ' +
          'seeder. Restart Cleanuparr if a reload is needed (`kubectl -n ' + args.namespace + ' rollout restart deploy/cleanuparr`) and wait Ready.',
        'Confirm the rule is enabled+armed (re-read cleanuparr.db) and Cleanuparr is healthy (pod Ready, logs clean, arrs+qbt Healthy).',
        'Do a final immediate post-arm check that no legitimate seeder was removed in the first cycle (compare seeder count before/after; expect ~' + args.seederCount + ').',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['armed', 'cleanuparrHealthy', 'seederCountAfter', 'summary'],
      properties: {
        armed: { type: 'boolean' },
        cleanuparrHealthy: { type: 'boolean' },
        seederCountAfter: { type: 'number' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 6 — tear down the OLD exports + old node mounts (live, destructive)
// ---------------------------------------------------------------------------
const cleanupTask = defineTask('cleanup-old', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Remove the 4 old NFS exports + old node mounts (live, destructive) after the unified path is proven',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage SRE retiring the now-unused legacy media exports/mounts',
      task:
        'Now that all consumers are on the unified export and the reaper is armed+healthy, remove the 4 old NFS exports (TrueNAS) and the 4 old node mounts ' +
        '(/mnt/k3s-animes|tvshows|movies|downloads) on each worker. Destructive but reversible from the captured baseline.',
      context: { ...args },
      instructions: [
        'PRE-FLIGHT: confirm NOTHING still uses the old mounts — no pod binds the old hostPaths, no process holds the old mountpoints (`lsof`/`fuser` on each node). ' +
          'If anything still references them, STOP and report.',
        'On EACH worker ' + JSON.stringify(args.workerNodes) + ', ONE AT A TIME: `umount` the 4 old /mnt/k3s-* mounts and remove their fstab lines (keep the unified mount). ' +
          'Re-verify node + pods Ready before the next node.',
        'TrueNAS (ssh truenas_admin@' + args.truenasHost + '): delete the 4 old NFS exports (`midclt call sharing.nfs.delete <id>`), keeping ONLY the unified export of ' +
          args.exportRoot + '. Re-verify the unified export still serves and pods stay Healthy.',
        'If any check fails, STOP and report; the captured baseline (' + JSON.stringify(args.rollbackPlan ? 'rollbackPlan provided' : 'baseline') + ') is the restore source.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['oldExportsRemoved', 'oldMountsRemoved', 'unifiedStillHealthy', 'summary'],
      properties: {
        oldExportsRemoved: { type: 'boolean' },
        oldMountsRemoved: { type: 'array', items: { type: 'string' } },
        unifiedStillHealthy: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 7 — docs + follow-ups + close #195 (git/outward)
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Update docs, open follow-up issues, execute #195 PR test plan, close #195 (per repo policy)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing out #195 per the Epaflix repo Critical Rules',
      task: 'Persist the outcome: docs, follow-up issues, run the PR test plans, and (if approved) close #195. Only the approved actions. Be ACCURATE — reaper is ' + (args.reaperArmed ? 'ARMED' : 'NOT armed (deferred)') + '.',
      context: { ...args },
      instructions: [
        'STATE OF PLAY (be accurate in all text): unified export id 32 of ' + args.exportRoot + ' live; all 5 media pods on a SINGLE /media mount (no subPath); ' +
          'root folders repointed to /media/{tvshows,animes,movies}; qbt save /media/downloads; copyUsingHardlinks=true on all arrs; real in-pod import proven nlink>=2. ' +
          'Reaper: ' + (args.reaperArmed ? 'ARMED.' : 'NOT armed — dry-run found ' + args.falsePositives + ' of ' + args.candidateCount + ' candidates are pre-fix copy-seeders (nlink=1 because they predate the fix); arming deferred until they age out.') +
          ' Teardown of the 4 OLD exports/mounts is DEFERRED (bazarr/lingarr still use old movies/tvshows claims; old paths are the soak rollback).',
        'DOCS (branch off origin/main, single PR): update 0-truenas/README.md (single unified export of ' + args.exportRoot + ' id 32, note the 4 old child exports remain TEMPORARILY for bazarr/lingarr + rollback, to be torn down after soak) and 0-truenas/MIGRATION-TO-SSD.md if it lists the 4 exports; ' +
          'update 2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md (EXDEV barrier REMOVED via single /media mount — explain the kubelet subPath-submount EXDEV gotcha so it is not repeated; reaper still OFF/DEFERRED with the pre-fix-copy reason; new path mapping) and ' +
          '2-k3s/08.servarr/_shared/storage/media-pvcs.yaml comments. Note the Cleanuparr unlinked-rule DB config + the qbt LocalHostAuth change are PVC/live-only (see #244).',
        'Commit (conventional, body referencing #195) ending with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`; PR body ends with the ' +
          '"🤖 Generated with [Claude Code](https://claude.com/claude-code)" line and includes a Test plan. Rebase onto origin/main, push --force-with-lease, wait `validate`, then `gh pr merge <n> --merge`.',
        'EXECUTE THE PR TEST PLANS of the merged PRs ' + (args.manifestPrUrl || '#239') + ' and ' + (args.redesignPrUrl || '#242') + ' and the docs PR: tick each box that is now TRUE by EDITING the PR description ' +
          '(never a new comment); strike-through any step that is deferred (e.g. reaper-armed, teardown) with a short reason. Be truthful — do not tick boxes that are not done.',
        'FOLLOW-UPS (gh issues on SpyrosPsarras/epaflix, enhancement shape ## Finding/## Current state/## Desired outcome/## Notes, cross-link #142/#195/#240). Open ONLY ones that do not already exist (check first): ' +
          '(a) ARM the unlinked reaper after pre-fix copy-seeders age out — re-run the dry-run measurement, arm only when false-positives=0 (this is the deferred #195 tail); ' +
          '(b) TEAR DOWN the 4 old NFS exports + old node mounts + old 4 PV/PVC after soak AND after bazarr/lingarr are migrated to the unified claim; ' +
          '(c) migrate bazarr + lingarr to the unified servarr-media /media mount; ' +
          '(d) codify the Cleanuparr unlinked-rule DB config (PVC-only) into the SOPS seed; ' +
          '(e) HIGH-PRIORITY OWNER-REQUESTED REVISIT — "revisit #195 and redo correctly". The owner is right that we took a SAFER route but the MAIN PROBLEM may not be solved: ' +
          'the unlinked/orphan reaper is still UNARMED, so #142/#195 orphan auto-reaping is only UNBLOCKED, not actually happening in production. This follow-up must: ' +
          're-confirm new imports consistently hardlink (nlink>=2) over a real soak; quantify how the ~96 pre-fix copy-seeders age out (and whether they ever will, or need a deliberate re-import/relocate so they become hardlinked); ' +
          'then DECIDE between (i) arming the reaper once false-positives=0, or (ii) a different approach if single-/media-mount turns out insufficient; and verify end-to-end that orphans are genuinely reaped. ' +
          'Frame it as the definitive "did #195 actually solve the problem, and finish it" task. Title it clearly as a revisit/redo of #195. ' +
          'Note #243 (ArgoCD SSA volumeMounts list durability) and #244 (qbt LocalHostAuth/config drift) were already opened by earlier steps — cross-link them, do not duplicate.',
        (args.closeIssue
          ? 'CLOSE #195 with a comment summarizing: unified export id 32, all media pods on single /media mount, root folders repointed, copyUsingHardlinks=true, in-pod hardlink nlink=2 PROVEN (EXDEV barrier removed — the structural blocker from #142 is gone), dry-run = ' + args.falsePositives + ' false positives so reaper ARMING is deferred to follow-up (a), teardown deferred to follow-up (b), and list all follow-up issue numbers. Then `gh issue close 195`. The issue title goal (UNBLOCK safe reaping) is achieved even though arming is deferred.'
          : 'DO NOT close #195. Post a status comment on #195 summarizing the same facts and listing the follow-up issue numbers, and leave it OPEN.'),
        'No secrets, no .history, no .a5c, no artifacts committed. If validate/merge cannot complete cleanly, leave the PR open and report it (do not close #195 in that case). No media release/show titles in any commit, PR, issue, or doc.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['docsPrUrl', 'followUpIssues', 'testPlanExecuted', 'issueClosed', 'summary'],
      properties: {
        docsPrUrl: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        testPlanExecuted: { type: 'boolean' },
        issueClosed: { type: 'boolean' },
        summary: { type: 'string' },
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
    namespace: 'servarr',
    truenasHost: '192.168.10.200',
    dataset: '/mnt/pool1/dataset01',
    exportRoot: '/mnt/pool1/dataset01',
    workerNodes: ['192.168.10.61', '192.168.10.62', '192.168.10.63', '192.168.10.65'],
    branch: 'issue-195-unified-nfs-export-cleanuparr',
    ...inputs,
  };

  ctx.log('info', `#195 NFS unification: dataset=${cfg.dataset} -> single export ${cfg.exportRoot}`);

  // ---- PHASE 0: baseline + design (read-only), with owner refine loop ----
  let design = await ctx.task(captureDesignTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, truenasHost: cfg.truenasHost,
    dataset: cfg.dataset, exportRoot: cfg.exportRoot, workerNodes: cfg.workerNodes,
  });
  ctx.log('info', `Baseline: exdevProven=${design.exdevProven}; seeders=${design.seederCount}; risks=${(design.risks || []).length}`);

  // GATE A — master plan + rollback approval (before ANY live change)
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const gateA = await ctx.breakpoint({
      question:
        'Approve the FULL #195 live remap plan before any change?\n\n' +
        'Baseline: 4 separate NFS exports of ' + cfg.dataset + '; EXDEV-proven=' + design.exdevProven + '; live seeders=' + design.seederCount + '.\n\n' +
        'Proposed path mapping:\n' + JSON.stringify(design.pathMapping, null, 2) + '\n\n' +
        'Ordered runbook:\n' + JSON.stringify(design.runbook, null, 2) + '\n\n' +
        'Rollback plan:\n' + JSON.stringify(design.rollbackPlan, null, 2) + '\n\n' +
        'Top risks: ' + JSON.stringify(design.risks) + '\n\n' +
        'Recovery-first: the unified export + node mounts are ADDED alongside the old ones; old paths are removed only at the very end. ' +
        'Approving authorizes the additive live build (TrueNAS export + node mounts). Each later destructive/deploy step has its own gate. Proceed?',
      options: ['Approve plan', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['deploy', 'approval-gate', 'storage'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (gateA.approved && !(gateA.response || '').toLowerCase().includes('abort')) break;
    if ((gateA.response || '').toLowerCase().includes('abort') || (!gateA.approved && attempt === 2)) {
      ctx.log('warn', 'Plan not approved — stopping after read-only design.');
      return { success: false, reason: 'plan-not-approved', unifiedExport: false, hardlinkProven: false, reaperArmed: false, design, feedback: gateA.response || gateA.feedback || '' };
    }
    lastFeedback = gateA.response || gateA.feedback || 'Changes requested';
    design = await ctx.task(refineDesignTask, { ...cfg, prior: design, feedback: lastFeedback });
  }

  // ---- PHASE 1: build recovery path (live, additive) ----
  const built = await ctx.task(buildNewPathTask, {
    namespace: cfg.namespace, truenasHost: cfg.truenasHost, exportRoot: cfg.exportRoot,
    workerNodes: cfg.workerNodes, runbook: design.runbook, pathMapping: design.pathMapping,
  });
  ctx.log('info', `New path: exportCreated=${built.exportCreated}; nodesMounted=${(built.nodesMounted || []).length}; hardlinkOnNew=${built.hardlinkProvenOnNewPath}`);
  if (!built.exportCreated || !built.hardlinkProvenOnNewPath || !built.nodesHealthy) {
    return { success: false, reason: 'build-new-path-failed', unifiedExport: built.exportCreated, hardlinkProven: false, reaperArmed: false, built, summary: built.summary };
  }

  // ---- PHASE 2: author manifest PR (git, not merged) ----
  const pr = await ctx.task(manifestRemapTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, namespace: cfg.namespace, branch: cfg.branch, pathMapping: design.pathMapping,
  });
  ctx.log('info', `Manifest PR: ${pr.prUrl}; kustomizeBuild=${pr.kustomizeBuildPassed}`);
  if (!pr.kustomizeBuildPassed || !pr.prUrl) {
    return { success: false, reason: 'manifest-pr-failed', unifiedExport: true, hardlinkProven: false, reaperArmed: false, pr, summary: pr.summary };
  }

  // GATE B — cutover (destructive-git + deploy): merge PR -> ArgoCD applies -> in-app remap
  const gateB = await ctx.breakpoint({
    question:
      'Recovery path is live and hardlink-capable. Manifest PR is open and kustomize-build clean:\n' + pr.prUrl + '\n' +
      'Files: ' + JSON.stringify(pr.filesChanged) + '\n\n' +
      'Approve the CUTOVER? This merges the PR (rebase->validate->--merge), lets ArgoCD reconcile all media pods onto the unified export, then ' +
      'remaps qbt save path + Sonarr/Sonarr2/Radarr root folders (REWRITES *arr DB paths, no byte move), sets copyUsingHardlinks=true, and proves a real ' +
      'import hardlinks (nlink>=2). Rollback = revert PR + re-point root folders. Proceed?',
    options: ['Approve cutover', 'Hold', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'destructive-git', 'approval-gate', 'storage'],
  });
  if (!gateB.approved || (gateB.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Cutover not approved — recovery path is live but unused; manifest PR left open.');
    return { success: false, reason: 'cutover-not-approved', unifiedExport: true, hardlinkProven: built.hardlinkProvenOnNewPath, reaperArmed: false, prUrl: pr.prUrl, feedback: gateB.response || '' };
  }

  // ---- PHASE 3: cutover (live) ----
  const cut = await ctx.task(cutoverTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, branch: pr.branch, prUrl: pr.prUrl, pathMapping: design.pathMapping,
  });
  ctx.log('info', `Cutover: merged=${cut.merged}; argoHealthy=${cut.argoHealthy}; realHardlink=${cut.realHardlinkProven}; libOk=${cut.libraryIntegrityOk}`);
  // Hard failure (something broke) -> stop with old path intact.
  if (!cut.merged || !cut.argoHealthy || !cut.libraryIntegrityOk) {
    return { success: false, reason: 'cutover-incomplete', unifiedExport: true, hardlinkProven: cut.realHardlinkProven, reaperArmed: false, cut, summary: cut.summary };
  }

  // ---- PHASE 3-REDESIGN: subPath mounts can't hardlink in-pod (kubelet submount EXDEV).
  //      Forward-fix to the owner's literal full remap: single /media mount + *arr/qbt path rewrite. ----
  let activeCut = cut;
  if (!cut.realHardlinkProven) {
    ctx.log('warn', 'subPath cutover healthy but in-pod hardlink FAILED (kubelet submount EXDEV). Entering single-root /media redesign.');
    const redesignBranch = 'issue-195-single-root-media-mount';

    const rpr = await ctx.task(redesignManifestTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, namespace: cfg.namespace, redesignBranch,
    });
    ctx.log('info', `Redesign PR: ${rpr.prUrl}; kustomizeBuild=${rpr.kustomizeBuildPassed}`);
    if (!rpr.kustomizeBuildPassed || !rpr.prUrl) {
      return { success: false, reason: 'redesign-pr-failed', unifiedExport: true, hardlinkProven: false, reaperArmed: false, cut, rpr, summary: rpr.summary };
    }

    // GATE B2 — approve the single-root cutover + DB-path rewrite (the top-risk step).
    const gateB2 = await ctx.breakpoint({
      question:
        'The subPath cutover is healthy and the library is intact, BUT in-pod hardlinks FAILED: Kubernetes materializes each subPath mount as a separate NFS submount, ' +
        'so link(2) returns EXDEV across them (the node-level /mnt/k3s-media hardlinks fine). The ONLY fix is the literal full remap you chose: mount the unified export ' +
        'ONCE per pod at /media (no subPath) and repoint paths to /media/*.\n\n' +
        'Redesign PR (single /media mount, kustomize-clean, not merged): ' + rpr.prUrl + '\n' +
        'New in-container paths: ' + JSON.stringify(rpr.newPaths) + '\n\n' +
        'Approving will: merge the PR -> ArgoCD rolls pods onto a single /media mount -> rewrite Sonarr/Sonarr2/Radarr root folders to /media/{tvshows,animes,movies} via ' +
        'bulk root-folder move with MOVE FILES UNCHECKED (DB-only, no byte move) -> qbt save path + set-location to /media/downloads (metadata-only) -> prove a real import ' +
        'hardlinks in-pod (nlink>=2). This DB-path rewrite is the top risk flagged at Gate A. Rollback = revert this PR + revert #239 + re-point root folders/qbt back. Proceed?',
      options: ['Approve redesign cutover', 'Hold', 'Abort (revert #239)'],
      expert: 'owner',
      tags: ['deploy', 'destructive', 'destructive-git', 'approval-gate', 'storage'],
    });
    if (!gateB2.approved || (gateB2.response || '').toLowerCase().includes('abort')) {
      ctx.log('warn', 'Redesign cutover not approved.');
      return { success: false, reason: 'redesign-not-approved', unifiedExport: true, hardlinkProven: false, reaperArmed: false, redesignPrUrl: rpr.prUrl, feedback: gateB2.response || '' };
    }

    activeCut = await ctx.task(redesignCutoverTask, {
      repoRoot: cfg.repoRoot, namespace: cfg.namespace, redesignBranch: rpr.branch, redesignPrUrl: rpr.prUrl, seederCount: design.seederCount, newPaths: rpr.newPaths,
    });
    ctx.log('info', `Redesign cutover: merged=${activeCut.merged}; argoHealthy=${activeCut.argoHealthy}; realHardlink=${activeCut.realHardlinkProven}; libOk=${activeCut.libraryIntegrityOk}`);
    if (!activeCut.merged || !activeCut.argoHealthy || !activeCut.libraryIntegrityOk || !activeCut.realHardlinkProven) {
      return { success: false, reason: 'redesign-cutover-incomplete', unifiedExport: true, hardlinkProven: activeCut.realHardlinkProven, reaperArmed: false, cut: activeCut, summary: activeCut.summary };
    }
  }

  // ---- PHASE 4: Cleanuparr dry-run (live, non-destructive) ----
  let dry = await ctx.task(dryRunTask, { namespace: cfg.namespace, seederCount: design.seederCount });
  ctx.log('info', `Dry-run: candidates=${dry.candidateCount}; falsePositives=${dry.falsePositives}; safeToArm=${dry.safeToArm}`);

  // Decide arming. The unification goal is already achieved; arming is operational.
  let armed = { armed: false, cleanuparrHealthy: true, seederCountAfter: design.seederCount, summary: 'reaper left OFF' };
  let reaperDeferred = false;

  if (dry.safeToArm && dry.falsePositives === 0) {
    // GATE C — arm reaper (deploy/destructive): dry-run is clean.
    const gateC = await ctx.breakpoint({
      question:
        'Cutover complete: pods on unified export, copyUsingHardlinks=true, real import proven nlink>=2.\n' +
        'Dry-run unlinked rule: ' + dry.candidateCount + ' candidates, ' + dry.falsePositives + ' false positives (none of the live seeders).\n\n' +
        'Approve ARMING the Cleanuparr unlinked/orphan reaper (enables delete-with-data, seeding/ratio guards preserved)?',
      options: ['Arm reaper', 'Leave OFF', 'Abort'],
      expert: 'owner',
      tags: ['deploy', 'destructive', 'approval-gate', 'servarr'],
    });
    if (gateC.approved && (gateC.response || '').toLowerCase().includes('arm')) {
      armed = await ctx.task(armTask, { namespace: cfg.namespace, seederCount: design.seederCount });
      ctx.log('info', `Armed=${armed.armed}; cleanuparrHealthy=${armed.cleanuparrHealthy}; seedersAfter=${armed.seederCountAfter}`);
    } else {
      ctx.log('warn', 'Reaper left OFF by owner despite clean dry-run.');
    }
  } else {
    // Dry-run NOT clean: the false positives are pre-fix copy-seeders (nlink=1 because they predate the
    // hardlink fix). Re-running now will not change that — arming must wait until they age out under the
    // existing seeding-ratio guards. Defer arming to a follow-up; do NOT loop, do NOT dead-end.
    reaperDeferred = true;
    ctx.log('warn', `Reaper NOT armed: ${dry.falsePositives}/${dry.candidateCount} candidates are pre-fix copy-seeders (false positives). Deferring arming to a follow-up issue.`);
  }

  // GATE D (final) — wrap-up + close decision. Teardown of the 4 old exports is DEFERRED regardless:
  // bazarr/lingarr still bind the old movies/tvshows claims, and the old paths are the soak-window rollback.
  const gateD = await ctx.breakpoint({
    question:
      '#195 core goal DELIVERED: 4 NFS exports unified to one, all media pods on a single /media mount, ' +
      'copyUsingHardlinks=true, real in-pod import proven nlink>=2 (EXDEV barrier removed). ' +
      'Reaper status: ' + (armed.armed ? 'ARMED' : (reaperDeferred ? 'DEFERRED — ' + dry.falsePositives + ' pre-fix copy-seeders would be false positives; arm after they age out' : 'left OFF')) + '.\n\n' +
      'Final wrap-up (no destructive teardown — old exports/mounts kept for soak + bazarr/lingarr still use them; teardown is a tracked follow-up):\n' +
      '  - Update docs (0-truenas + servarr RECOVERY + media-pvcs comments).\n' +
      '  - Open follow-up issues: arm-reaper-after-aging, teardown-old-exports-after-soak, bazarr/lingarr migration, SSA list durability (#243), qbt auth/config drift (#244).\n' +
      '  - Run the PR test plans (#239/#242).\n' +
      '  - Close #195 (structural goal met; arming tracked as follow-up) OR keep it open.\n\n' +
      'How to proceed?',
    options: ['Approve wrap-up + close #195', 'Approve wrap-up, keep #195 open', 'Stop here'],
    expert: 'owner',
    tags: ['destructive-git', 'approval-gate', 'storage'],
  });

  if (!gateD.approved || (gateD.response || '').toLowerCase().includes('stop here')) {
    ctx.log('warn', 'Stopped before wrap-up by owner.');
    return { success: true, partial: true, reason: 'stopped-before-wrapup', unifiedExport: true, hardlinkProven: activeCut.realHardlinkProven, reaperArmed: armed.armed, reaperDeferred, falsePositives: dry.falsePositives };
  }
  const closeIssue = (gateD.response || '').toLowerCase().includes('close');

  // ---- PHASE 7: docs + follow-ups + (optionally) close #195 ----
  const wrap = await ctx.task(wrapupTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, namespace: cfg.namespace, exportRoot: cfg.exportRoot,
    manifestPrUrl: pr.prUrl, redesignPrUrl: 'https://github.com/SpyrosPsarras/epaflix/pull/242',
    reaperArmed: armed.armed, reaperDeferred, falsePositives: dry.falsePositives, candidateCount: dry.candidateCount,
    closeIssue, teardownDeferred: true,
  });
  ctx.log('info', `Wrap-up: docsPR=${wrap.docsPrUrl}; followUps=${JSON.stringify(wrap.followUpIssues)}; closed=${wrap.issueClosed}`);

  return {
    success: true,
    partial: reaperDeferred,
    unifiedExport: true,
    hardlinkProven: activeCut.realHardlinkProven,
    reaperArmed: armed.armed,
    reaperDeferred,
    falsePositives: dry.falsePositives,
    teardownDeferred: true,
    issueClosed: wrap.issueClosed,
    prUrls: [pr.prUrl, 'https://github.com/SpyrosPsarras/epaflix/pull/242', wrap.docsPrUrl].filter(Boolean),
    followUpIssues: wrap.followUpIssues || [],
    summary: wrap.summary,
  };
}

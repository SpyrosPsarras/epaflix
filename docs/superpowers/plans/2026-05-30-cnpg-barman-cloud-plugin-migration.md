# CNPG Barman Cloud Plugin Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `postgres-cluster` backups from CNPG in-tree `spec.backup.barmanObjectStore` to the standalone Barman Cloud Plugin, applied + restore-verified on the live cluster, closing issue #10.

**Architecture:** Install the plugin operator imperatively in `cnpg-system` (mirrors the CNPG operator install, stays out of ArgoCD per #93). Add a namespaced `ObjectStore` CR holding the MinIO config + retention. Switch the `Cluster` from `spec.backup.barmanObjectStore` to `spec.plugins`, and the `ScheduledBackup` to `method: plugin`. Ordering is enforced so the `ObjectStore` exists before the `Cluster` references it (the `postgres` ArgoCD Application is `selfHeal: true`, so a merge auto-applies).

**Tech Stack:** CloudNativePG 1.28.0, Barman Cloud Plugin v0.12.0 (fallback v0.11.0), kustomize, ArgoCD, cert-manager, SOPS pre-commit hook, MinIO/`mc`.

**Branch:** `migrate-cnpg-barman-cloud-plugin` (already created; design doc committed).

**Spec:** `docs/superpowers/specs/2026-05-30-cnpg-barman-cloud-plugin-migration-design.md`

---

## File Structure

- Create: `2-k3s/06.postgres/barman-cloud-plugin/manifest.yaml` — vendored upstream plugin operator (Deployment + CRDs + cert-manager Issuer/Certificate + empty placeholder Secret), namespace `cnpg-system`.
- Create: `2-k3s/06.postgres/barman-cloud-plugin/README.md` — what this is, version, how to re-vendor.
- Create: `2-k3s/06.postgres/03.install-barman-plugin.sh` — imperative install script (mirrors `01.install-operator.sh`).
- Create: `2-k3s/06.postgres/cluster/postgres-object-store.yaml` — `ObjectStore` CR.
- Modify: `2-k3s/06.postgres/cluster/postgres-cluster.yaml` — drop `spec.backup`, add `spec.plugins`.
- Modify: `2-k3s/06.postgres/backup/backup-schedule.yaml` — `method: plugin`.
- Modify: `2-k3s/06.postgres/kustomization.yaml` — add `cluster/postgres-object-store.yaml` to `resources`.
- Modify: `2-k3s/11.argocd/apps/app-postgres.yaml` — align `syncPolicy` manual → selfHeal (drift fix).
- Modify: `.github/hooks/check-sops-encrypted.sh` — allowlist the plugin manifest's placeholder Secret.

---

## Task 1: Vendor + install the Barman Cloud Plugin operator (imperative)

**Files:**
- Create: `2-k3s/06.postgres/barman-cloud-plugin/manifest.yaml`
- Create: `2-k3s/06.postgres/barman-cloud-plugin/README.md`
- Create: `2-k3s/06.postgres/03.install-barman-plugin.sh`
- Modify: `.github/hooks/check-sops-encrypted.sh`

- [ ] **Step 1: Vendor the upstream manifest**

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox
mkdir -p 2-k3s/06.postgres/barman-cloud-plugin
curl -fsSL https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.12.0/manifest.yaml \
  -o 2-k3s/06.postgres/barman-cloud-plugin/manifest.yaml
```

- [ ] **Step 2: Confirm what was vendored**

Run:
```bash
grep -E '^kind:|cloudnative-pg/plugin-barman-cloud:' 2-k3s/06.postgres/barman-cloud-plugin/manifest.yaml | sort -u
```
Expected: `kind:` set includes `CustomResourceDefinition`, `Deployment`, `Issuer`, `Certificate`, `Secret`, plus RBAC; a pinned `:v0.12.0` image tag. CRD: `objectstores.barmancloud.cnpg.io` (v0.12.0 ships only this CRD — our flow needs no `BackupConfiguration`).

- [ ] **Step 3: Allowlist the plugin's placeholder Secret in the pre-commit hook**

The manifest contains one `kind: Secret` (`plugin-barman-cloud-<hash>`, namespace `cnpg-system`, `type: Opaque`, **no `data:` block** — a cert-manager/operator-managed placeholder). The hook refuses plaintext Secrets; allowlist this vendored file.

Modify `.github/hooks/check-sops-encrypted.sh`, the `ALLOWLIST` array:

```bash
ALLOWLIST=(
  "2-k3s/11.argocd/oidc-secret.yaml"
  "2-k3s/06.postgres/barman-cloud-plugin/manifest.yaml"
)
```

- [ ] **Step 4: Write the install script**

Create `2-k3s/06.postgres/03.install-barman-plugin.sh`:

```bash
#!/bin/bash
set -e

echo "======================================"
echo "Installing Barman Cloud Plugin (v0.12.0)"
echo "======================================"

# Requires: CNPG operator >= 1.26 (we run 1.28.0) and cert-manager
# (plugin uses a cert-manager Issuer/Certificate for its CNPG-i gRPC TLS).
echo "Applying vendored plugin manifest into cnpg-system..."
kubectl apply --server-side --force-conflicts \
  -f barman-cloud-plugin/manifest.yaml

echo "Waiting for plugin deployment to be ready..."
kubectl wait --for=condition=available --timeout=300s \
  deployment/barman-cloud -n cnpg-system

echo ""
echo "Verify:"
echo "  kubectl get pods -n cnpg-system | grep barman"
echo "  kubectl get crd | grep barmancloud"
```

Make it executable:
```bash
chmod +x 2-k3s/06.postgres/03.install-barman-plugin.sh
```

- [ ] **Step 5: Write the README**

Create `2-k3s/06.postgres/barman-cloud-plugin/README.md`:

```markdown
# Barman Cloud Plugin (imperative)

CNPG Barman Cloud Plugin operator + `ObjectStore` CRD, installed into
`cnpg-system`. Pinned to **v0.12.0**. Added to migrate postgres-cluster off
in-tree `barmanObjectStore` (issue #10).

Installed imperatively via `../03.install-barman-plugin.sh` — NOT under ArgoCD,
mirroring the CNPG operator (`../operator/cnpg-operator.yaml`). ArgoCD adoption of
operators/CRDs is tracked in issue #93.

Requires CNPG operator >= 1.26 and cert-manager (gRPC TLS).

Re-vendor a new version:

    curl -fsSL https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/<vX.Y.Z>/manifest.yaml \
      -o manifest.yaml

The single `kind: Secret` in this manifest is an empty cert-manager-managed
placeholder (no credentials) and is allowlisted in
`.github/hooks/check-sops-encrypted.sh`.
```

- [ ] **Step 6: Install to the live cluster**

Run:
```bash
cd 2-k3s/06.postgres && ./03.install-barman-plugin.sh; cd -
```
Expected: deployment becomes available; script prints verify hints.

- [ ] **Step 7: Verify operator + CRDs**

Run:
```bash
kubectl get pods -n cnpg-system | grep barman
kubectl get crd | grep barmancloud
```
Expected: a `barman-cloud-...` pod `Running` 1/1 (or 2/2 with sidecar); CRD `objectstores.barmancloud.cnpg.io` listed.

If the pod crashloops citing an incompatible CNPG/API version on 1.28, re-vendor v0.11.0 (Step 1 with `v0.11.0`), re-run Steps 6–7, and update the README/script version strings.

- [ ] **Step 8: Confirm the ObjectStore retention field path**

Run:
```bash
kubectl explain objectstore.spec --recursive | grep -iE 'retention|configuration' | head
```
Expected: confirms whether retention is `objectstore.spec.retentionPolicy` (string) — note the exact path for Task 2. (Docs state retention moves to the `ObjectStore`; this verifies the field name for this release.)

- [ ] **Step 9: Commit**

```bash
git add 2-k3s/06.postgres/barman-cloud-plugin/ 2-k3s/06.postgres/03.install-barman-plugin.sh .github/hooks/check-sops-encrypted.sh
git commit -m "feat(postgres): vendor + install Barman Cloud Plugin operator v0.12.0 (#10)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
Expected: pre-commit hook passes (manifest Secret allowlisted), commit succeeds.

---

## Task 2: Create + apply the `ObjectStore` CR

**Files:**
- Create: `2-k3s/06.postgres/cluster/postgres-object-store.yaml`
- Modify: `2-k3s/06.postgres/kustomization.yaml`

- [ ] **Step 1: Write the ObjectStore CR**

Create `2-k3s/06.postgres/cluster/postgres-object-store.yaml`. Use the retention field path confirmed in Task 1 Step 8 (shown here as `spec.retentionPolicy`; correct if `kubectl explain` showed otherwise). The `sync-wave: "-1"` annotation makes ArgoCD apply this before the `Cluster` (default wave 0) in Task 4's merge — belt-and-suspenders on ordering.

```yaml
---
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata:
  name: postgres-minio-store
  namespace: postgres-system
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
spec:
  # .spec.configuration mirrors the in-tree spec.backup.barmanObjectStore block.
  configuration:
    destinationPath: s3://postgres-backups/
    endpointURL: https://minio.epaflix.com
    s3Credentials:
      accessKeyId:
        name: minio-backup-credentials
        key: ACCESS_KEY_ID
      secretAccessKey:
        name: minio-backup-credentials
        key: ACCESS_SECRET_KEY
    wal:
      compression: gzip
      maxParallel: 4
    data:
      compression: gzip
      jobs: 2
  retentionPolicy: "10d"
```

- [ ] **Step 2: Add it to the kustomization**

Modify `2-k3s/06.postgres/kustomization.yaml` — add to `resources`, right before `cluster/postgres-cluster.yaml`:

```yaml
  - cluster/postgres-object-store.yaml
  - cluster/postgres-cluster.yaml
```

- [ ] **Step 3: Server-side dry-run the CR**

Run:
```bash
kubectl apply --server-side --dry-run=server -f 2-k3s/06.postgres/cluster/postgres-object-store.yaml
```
Expected: `objectstore.barmancloud.cnpg.io/postgres-minio-store serverside-applied (dry run)`, no schema errors. If a field (e.g. `retentionPolicy`) is rejected, correct against `kubectl explain objectstore.spec` and re-run.

- [ ] **Step 4: Apply the CR to the live cluster (ahead of the Cluster edit)**

Run:
```bash
kubectl apply --server-side --force-conflicts -f 2-k3s/06.postgres/cluster/postgres-object-store.yaml
```
Expected: `serverside-applied`. (selfHeal `prune:false` means this branch-only resource is safe to pre-apply; ArgoCD will not delete it.)

- [ ] **Step 5: Verify the ObjectStore is accepted**

Run:
```bash
kubectl get objectstore postgres-minio-store -n postgres-system -o yaml | grep -A20 'status:'
```
Expected: a `status` with no error conditions (the CR validates credentials lazily; an empty/ready-ish status is acceptable). The `minio-backup-credentials` Secret already exists in `postgres-system` (verify: `kubectl get secret minio-backup-credentials -n postgres-system`).

- [ ] **Step 6: Commit**

```bash
git add 2-k3s/06.postgres/cluster/postgres-object-store.yaml 2-k3s/06.postgres/kustomization.yaml
git commit -m "feat(postgres): add ObjectStore CR for Barman Cloud Plugin (#10)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Switch the Cluster + ScheduledBackup to the plugin; fix app-postgres drift

**Files:**
- Modify: `2-k3s/06.postgres/cluster/postgres-cluster.yaml`
- Modify: `2-k3s/06.postgres/backup/backup-schedule.yaml`
- Modify: `2-k3s/11.argocd/apps/app-postgres.yaml`

- [ ] **Step 1: Edit the Cluster — remove in-tree backup, add plugin**

In `2-k3s/06.postgres/cluster/postgres-cluster.yaml`, delete the entire `backup:` block (the `barmanObjectStore:` … `target: prefer-standby` stanza) and add a top-level `plugins:` list under `spec:` (e.g. directly after `storage:`):

```yaml
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: postgres-minio-store
```

Update the trailing comment block in that file (the one referencing the `backup:` stanza) to instead describe the plugin + `ObjectStore` (`postgres-object-store.yaml`), so the file's own docs stay accurate.

- [ ] **Step 2: Edit the ScheduledBackup**

In `2-k3s/06.postgres/backup/backup-schedule.yaml`, replace `method: barmanObjectStore` with:

```yaml
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
```
Keep `schedule: "0 0 2 * * *"`, `immediate: false`, `suspend: false`.

- [ ] **Step 3: Fix the app-postgres syncPolicy drift (manual → selfHeal)**

In `2-k3s/11.argocd/apps/app-postgres.yaml`, replace:

```yaml
  # PHASE 1: manual only.
  syncPolicy: {}
```
with (matches live state set by PR #92 / #34):

```yaml
  syncPolicy:
    automated:
      selfHeal: true
      prune: false
    syncOptions:
      - ServerSideApply=true
```
Update the leading `# Sync:` comment to note the flip is done (no longer "manual on first install").

- [ ] **Step 4: Server-side dry-run the whole kustomization**

Run:
```bash
kubectl kustomize 2-k3s/06.postgres | kubectl apply --server-side --dry-run=server -f -
```
Expected: all resources `serverside-applied (dry run)`; **no** deprecation warning about `spec.backup.barmanObjectStore` (it's gone); no schema errors on `spec.plugins` or the ScheduledBackup.

- [ ] **Step 5: Commit**

```bash
git add 2-k3s/06.postgres/cluster/postgres-cluster.yaml 2-k3s/06.postgres/backup/backup-schedule.yaml 2-k3s/11.argocd/apps/app-postgres.yaml
git commit -m "feat(postgres): switch Cluster + ScheduledBackup to Barman Cloud Plugin; align app-postgres selfHeal (#10)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Open PR, merge, and watch the live cutover

**Files:** none (operational).

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin migrate-cnpg-barman-cloud-plugin
gh pr create --repo SpyrosPsarras/epaflix --base main \
  --title "Migrate CNPG postgres-cluster to Barman Cloud Plugin (#10)" \
  --body "$(cat <<'EOF'
## Finding
Closes #10. Migrates postgres-cluster off in-tree barmanObjectStore to the Barman Cloud Plugin (prerequisite for the CNPG 1.30 bump, #102).

## Changes
- Vendored + imperatively installed plugin operator v0.12.0 (cnpg-system).
- ObjectStore CR `postgres-minio-store` (MinIO config + 10d retention).
- Cluster: removed spec.backup.barmanObjectStore, added spec.plugins.
- ScheduledBackup: method: plugin.
- Fixed app-postgres.yaml syncPolicy drift (manual -> selfHeal, matches live).
- Allowlisted plugin manifest placeholder Secret in pre-commit hook.

## Test plan
- [ ] Plugin operator + CRDs healthy in cnpg-system
- [ ] ObjectStore accepted
- [ ] ArgoCD syncs; Cluster healthy 3/3 after rolling restart
- [ ] ContinuousArchiving=True with plugin
- [ ] Barman sidecar present in each pod
- [ ] On-demand plugin Backup completes; new objects in bucket
- [ ] Restore test from plugin backup passes
- [ ] No barmanObjectStore deprecation warning on apply

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Merge (repo policy: merge-commit, admin bypass)**

```bash
gh pr merge --repo SpyrosPsarras/epaflix --merge --admin
```
Expected: merged to `main`.

- [ ] **Step 3: Trigger / watch ArgoCD sync**

Run:
```bash
kubectl -n argocd annotate application postgres argocd.argoproj.io/refresh=hard --overwrite
kubectl -n argocd get application postgres -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
```
Expected: progresses to `Synced Healthy`. (`ObjectStore` wave -1 applies before the `Cluster`.)

- [ ] **Step 4: Watch the rolling restart settle**

Run:
```bash
kubectl get cluster postgres-cluster -n postgres-system -w
```
Expected: transitions through `Upgrading cluster`/switchover back to `Cluster in healthy state`, `3/3` ready. Ctrl-C when healthy.

- [ ] **Step 5: Verify plugin attached + archiving green + sidecar present**

Run:
```bash
kubectl get cluster postgres-cluster -n postgres-system -o jsonpath='{.spec.plugins}{"\n"}'
kubectl get cluster postgres-cluster -n postgres-system -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")].status}{"\n"}'
kubectl get pod -n postgres-system postgres-cluster-1 -o jsonpath='{.spec.containers[*].name}{"\n"}'
```
Expected: plugins list shows `barman-cloud.cloudnative-pg.io`; `ContinuousArchiving` = `True`; container list includes a barman/plugin sidecar alongside `postgres`.

---

## Task 5: Take a plugin backup and verify objects land

**Files:**
- Create (transient): `/tmp/postgres-plugin-backup-test.yaml`

- [ ] **Step 1: Trigger an on-demand plugin backup**

```bash
cat > /tmp/postgres-plugin-backup-test.yaml <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: plugin-migration-verify
  namespace: postgres-system
spec:
  cluster:
    name: postgres-cluster
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
EOF
kubectl apply -f /tmp/postgres-plugin-backup-test.yaml
```

- [ ] **Step 2: Verify the backup completes**

Run:
```bash
kubectl get backup plugin-migration-verify -n postgres-system -o jsonpath='{.status.phase}{"\n"}'
```
Expected: reaches `completed` (poll until it does; failures show `failed` with a `.status.error`).

- [ ] **Step 3: Verify new objects in the bucket**

Run (uses the existing `mc` alias `tn` for TrueNAS MinIO):
```bash
mc ls --recursive tn/postgres-backups | tail -10
```
Expected: fresh base-backup + WAL objects with a current timestamp (path layout may differ slightly from the in-tree layout — expected).

---

## Task 6: Restore test (the gate for closing #10)

**Files:**
- Create (transient): `/tmp/postgres-restore-test.yaml`

- [ ] **Step 1: Bootstrap a throwaway cluster from the plugin backup**

```bash
cat > /tmp/postgres-restore-test.yaml <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: postgres-restore-test
  namespace: postgres-system
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:16
  storage:
    storageClass: local-path
    size: 20Gi
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: false
      parameters:
        barmanObjectName: postgres-minio-store
  bootstrap:
    recovery:
      source: origin
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: postgres-minio-store
          serverName: postgres-cluster
EOF
kubectl apply -f /tmp/postgres-restore-test.yaml
```
Note: `serverName: postgres-cluster` points the recovery at the source cluster's path in the bucket. Confirm the source server name from `mc ls tn/postgres-backups/` if the top-level prefix differs.

- [ ] **Step 2: Watch the restore cluster come up**

Run:
```bash
kubectl get cluster postgres-restore-test -n postgres-system -w
```
Expected: reaches `Cluster in healthy state`, `1/1`. Ctrl-C when healthy. (If it errors, read `kubectl describe cluster postgres-restore-test -n postgres-system` and the restore job logs.)

- [ ] **Step 3: Verify data is present**

Run:
```bash
kubectl exec -n postgres-system postgres-restore-test-1 -- \
  psql -U postgres -c "\l" -c "SELECT count(*) FROM pg_database;"
```
Expected: the migrated databases (`authentik`, `jellyseerr`, the `*arr` DBs, `observability`) are listed — proving the plugin backup is restorable.

- [ ] **Step 4: Tear down the throwaway cluster**

```bash
kubectl delete -f /tmp/postgres-restore-test.yaml
rm -f /tmp/postgres-restore-test.yaml /tmp/postgres-plugin-backup-test.yaml
```
Expected: cluster + its PVCs removed. Verify: `kubectl get cluster -n postgres-system` shows only `postgres-cluster`.

---

## Task 7: Close out

**Files:**
- Create: `.history/2026-05-30-cnpg-barman-cloud-plugin-migration.md` (command log; git-ignored content, do NOT `git add -f`)

- [ ] **Step 1: Log the migration to .history**

Create `.history/2026-05-30-cnpg-barman-cloud-plugin-migration.md` summarising commands run + key outputs (operator install, ObjectStore apply, cutover sync, backup + restore verification). Do not force-add it (`.gitignore` covers `.history/*.md`).

- [ ] **Step 2: Edit the PR test plan with outcomes**

Run `gh pr edit` (or the API) to check every box in the PR body's `## Test plan` and append the actual evidence (e.g. `ContinuousArchiving=True`, completed backup name, restore DB count). Record by **editing the PR body**, not a new comment.

- [ ] **Step 3: Confirm #10 closed**

The PR body says `Closes #10`; verify:
```bash
gh issue view 10 --repo SpyrosPsarras/epaflix --json state -q .state
```
Expected: `CLOSED` after merge. If still open, close it with a comment summarising the verified outcome and cross-linking #102.

- [ ] **Step 4: Final sanity sweep**

Run:
```bash
kubectl get cluster postgres-cluster -n postgres-system
kubectl get scheduledbackup -n postgres-system
kubectl -n argocd get application postgres -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
```
Expected: cluster healthy 3/3; ScheduledBackup present with `method: plugin`; ArgoCD `Synced Healthy`. No new GitHub issues created (migration self-contained).

---

## Self-Review Notes

- **Spec coverage:** plugin install (T1), ObjectStore + retention (T2), Cluster/Schedule cutover (T3), ordering via pre-apply + sync-wave (T2/T3/T4), app-postgres drift fix (T3), verification incl. restore (T4–T6), rollback documented in spec, close-out + .history + PR test plan (T7). All spec sections mapped.
- **Ordering safety:** ObjectStore applied live (T2 S4) and given sync-wave -1 (T2 S1) before the Cluster references it (T3/T4) — covers both the pre-merge soak and the auto-sync.
- **Secrets:** no new Secret authored; `minio-backup-credentials` reused; the vendored manifest's empty placeholder Secret is allowlisted, not committed in plaintext violation.
- **Version risk:** v0.12.0 with explicit v0.11.0 fallback path (T1 S7).
- **Field uncertainty:** ObjectStore retention path verified live (T1 S8) before authoring (T2 S1) — no guessed field shipped without a dry-run (T2 S3).

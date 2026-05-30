# Migrate CNPG `postgres-cluster` to the Barman Cloud Plugin (#10)

- **Date:** 2026-05-30
- **Issue:** [#10](https://github.com/SpyrosPsarras/epaflix/issues/10)
- **Related:** #102 (CNPG 1.30 operator bump — gated on this), #93 (operator/CRDs under ArgoCD)
- **Status:** design approved, ready for implementation plan

## Goal

Move `postgres-cluster` backups off CNPG's in-tree `spec.backup.barmanObjectStore`
onto the standalone **Barman Cloud Plugin** (operator + `ObjectStore`/`BackupConfiguration`
CRDs + per-pod sidecar). This removes the deprecation warning and is the prerequisite
that unblocks the eventual CNPG 1.30 operator upgrade (#102), where native
`barmanObjectStore` is removed entirely.

Done = plugin path applied to the live cluster **and** a plugin-taken backup
restore-verified, so the in-tree path can be retired with confidence. Closing #10
requires the restore test, not just a successful apply.

## Context / current state (verified 2026-05-30)

- CNPG operator `ghcr.io/cloudnative-pg/cloudnative-pg:1.28.0` in `cnpg-system`.
- `postgres-cluster` (`postgres-system`), 3 instances on local-path PVCs, healthy,
  `ContinuousArchiving=True`, primary `postgres-cluster-9`. Backs Authentik SSO +
  every *arr database — a backup-path break is high-impact.
- In-tree backup → MinIO at `https://minio.epaflix.com`, bucket `postgres-backups`,
  secret `minio-backup-credentials` (`ACCESS_KEY_ID`/`ACCESS_SECRET_KEY`),
  retention `10d`. `ScheduledBackup` `0 0 2 * * *` daily, `method: barmanObjectStore`.
- **Plugin compatibility confirmed:** Barman Cloud Plugin requires CNPG **≥ 1.26**;
  we run 1.28.0, so migration is feasible **without** bumping the operator. Latest
  plugin release **v0.12.0** (2026-04-14); fallback **v0.11.0** if v0.12.0 will not
  register against the 1.28 operator.
- cert-manager is running (`cert-manager` ns, 3/3) — the plugin operator install
  manifest needs it for its CNPG-i gRPC TLS certificate.
- **ArgoCD drift found:** live `postgres` Application is
  `automated:{selfHeal:true, prune:false}, syncOptions:[ServerSideApply=true]`
  (flipped via PR #92 / #34), but repo `2-k3s/11.argocd/apps/app-postgres.yaml`
  still declares `syncPolicy: {}`. Consequence: changes merged to `main` in the
  `app-postgres` source path (`2-k3s/06.postgres`) **auto-apply** — there is no
  manual-sync gate. This PR aligns the repo flag to live (manual → selfHeal).

## Design

### Components & repo layout

| Item | Path | Managed by | Notes |
|------|------|-----------|-------|
| Plugin operator + CRDs (`ObjectStore`, `BackupConfiguration`) | `2-k3s/06.postgres/barman-cloud-plugin/` + install script | **Imperative** (mirrors `cnpg-operator.yaml`) | Lands in `cnpg-system`. NOT under ArgoCD — operator/CRD ArgoCD adoption stays #93's scope. |
| `ObjectStore` CR `postgres-minio-store` | `2-k3s/06.postgres/cluster/postgres-object-store.yaml` | ArgoCD (`app-postgres`) | `spec.configuration` = current `barmanObjectStore` block verbatim; reuses `minio-backup-credentials`; `retentionPolicy: 10d` moves here. |
| Cluster manifest | `2-k3s/06.postgres/cluster/postgres-cluster.yaml` (edit) | ArgoCD | Remove `spec.backup.barmanObjectStore` + `spec.backup.retentionPolicy`; add `spec.plugins`. |
| ScheduledBackup | `2-k3s/06.postgres/backup/backup-schedule.yaml` (edit) | ArgoCD | `method: plugin` + `pluginConfiguration.name`; keep `0 0 2 * * *`. |
| ArgoCD Application flag | `2-k3s/11.argocd/apps/app-postgres.yaml` (edit) | — | Align `syncPolicy: {}` → `automated:{selfHeal:true, prune:false}` + `ServerSideApply=true` to match live (drift fix; chosen option (b)). |

### Manifest shapes (field names confirmed against upstream docs)

`ObjectStore`:
```yaml
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata:
  name: postgres-minio-store
  namespace: postgres-system
spec:
  configuration:
    destinationPath: s3://postgres-backups/
    endpointURL: https://minio.epaflix.com
    s3Credentials:
      accessKeyId:    { name: minio-backup-credentials, key: ACCESS_KEY_ID }
      secretAccessKey:{ name: minio-backup-credentials, key: ACCESS_SECRET_KEY }
    wal:  { compression: gzip, maxParallel: 4 }
    data: { compression: gzip, jobs: 2 }
  retentionPolicy: "10d"   # exact field path confirmed at implement time
```

Cluster `spec.plugins` (replaces `spec.backup`):
```yaml
plugins:
- name: barman-cloud.cloudnative-pg.io
  isWALArchiver: true
  parameters:
    barmanObjectName: postgres-minio-store
```

ScheduledBackup:
```yaml
method: plugin
pluginConfiguration:
  name: barman-cloud.cloudnative-pg.io
```

### Cutover ordering (mandatory — selfHeal auto-applies)

1. Install plugin operator imperatively → confirm CRDs registered + operator pod
   healthy in `cnpg-system`.
2. Apply `ObjectStore` CR → confirm accepted/ready. **Must exist before** the Cluster
   references it, or WAL archiving breaks on the next sync.
3. **Then** merge the Cluster + ScheduledBackup edits (and the `app-postgres.yaml`
   flag) to `main` → ArgoCD auto-syncs → rolling pod recreate injects the sidecar →
   primary switchover (brief connection blip for Authentik/*arr).
4. Old bucket objects remain untouched.

### Verification

- `Cluster` healthy, 3/3, `ContinuousArchiving=True`.
- `spec.plugins` present; barman sidecar container in each pod.
- On-demand `Backup` (plugin method) completes; new objects land in `s3://postgres-backups/`.
- **Restore test:** bootstrap a throwaway `Cluster` from the plugin backup, confirm it
  starts and data is present, then delete it. Gate for trusting the plugin path.

### Rollback

- Before step 3: nothing applied to the Cluster — simply don't merge.
- After step 3: revert the commit → ArgoCD restores in-tree `barmanObjectStore`
  (old backups still valid) → rolling restart back. Plugin operator may stay installed
  (additive, harmless).

## Out of scope

- CNPG 1.30 operator bump (#102) — do **after** this is verified.
- Bringing the CNPG / plugin operator + CRDs under ArgoCD (#93).
- Rotating MinIO credentials; deleting old bucket objects (kept until restore-verified).
- `prune: true` (#21) — stays off on `app-postgres`.

## Outcome

Closes #10. No new issues created. The `app-postgres.yaml` drift fix is folded into
the same PR (option (b), per user).

# Process: huntarr → newtarr full migration (Epaflix #131)

## Goal
Full rename `huntarr` → `newtarr` (ElfHosted maintained fork,
`ghcr.io/elfhosted/newtarr:rolling`) because upstream Huntarr is discontinued
(image gone → ImagePullBackOff). **No leftovers**, and **all prior functionality
preserved** (prowlarr / qbittorrent / servarr Sonarr+Sonarr2 integrations +
tracked-items state carried via SQLite copy).

## Why it's not trivial
The integrations live inside huntarr's own config (SQLite in the config PVC), and
huntarr's external access is fronted by **runtime** config not in git: an
Authentik Application/Proxy-Provider, plus Pi-hole + Cloudflare DNS. So the
migration spans GitOps **and** runtime, and the `servarr` ArgoCD App is
`selfHeal:true` with `prune:false` → the rename goes through git and the old live
huntarr resources become orphans that must be deleted manually.

## Phases
1. **discover-runtime** — exhaustive inventory: git hits (classified), live k8s
   resources, the config PVC's on-disk dir + SQLite files, ingress/routing,
   Authentik objects (app/provider/bindings/outpost), Pi-hole + Cloudflare DNS.
2. **plan-migration** — concrete ordered change set + runtime steps + exact copy
   commands + leftover checks. **[BP: approve plan]** (refine loop ×3).
3. **author-manifests** — git mv dir, rename Deployment/Service/PVC/PDB/labels,
   set image, update `app-servarr.yaml` image-updater list + annotations
   (`allow-tags: ^rolling$`), update docs, scrub dead backup stanza. Validate
   with `kustomize build`. Branch + one local commit.
4. **[BP: deploy + merge]** → **publish-merge** — push, PR (with Test Plan),
   rebase, await `validate`, `gh pr merge --merge` (Epaflix policy). ArgoCD
   creates live newtarr + empty `newtarr-config` PVC.
5. **[BP: config copy — DESTRUCTIVE]** → **config-data-migration** — suspend
   auto-sync, scale newtarr to 0, copy `huntarr.db`/`logs.db`/`backups/` into the
   new PVC, `chown 568:568`, restore. Old huntarr untouched (still serving).
6. **[BP: Authentik — secrets/SSO]** → **authentik-migrate** — recreate
   app/provider as newtarr (`newtarr.epaflix.com` → `newtarr.servarr.svc:30262`),
   reattach group bindings + outpost, delete old huntarr objects.
7. **[BP: DNS cutover]** → **dns-cutover** — Pi-hole dnsmasq (files-only golden
   rule) + Cloudflare DNS-only shadow record; add newtarr, remove huntarr.
8. **[BP: delete old — DESTRUCTIVE]** → **cleanup-old-huntarr** — delete orphan
   deploy/svc/pdb/ingressroute + old config PVC (only after newtarr verified).
9. **verify-work** — newtarr 1/1 + Synced/Healthy, SSO login, config carried,
   Sonarr2-race behaviour unchanged, **zero functional leftovers** across
   git/live/Authentik/DNS. **[BP: anomaly]** re-verify / accept / stop.
10. **closeout** — close #131, tick PR Test Plan (edit body), open follow-up
    `gh issue`s (Sonarr2-race recheck, fork support-risk revisit, any anomaly).

## Guardrails
- Epaflix merge policy: branch + PR + rebase + `validate` + `--merge`.
- Never commit secrets; Authentik/Cloudflare tokens from secrets.yml, never printed.
- Old huntarr stays live until the end → reversible until cleanup.
- Follow-ups → `gh issue` per CLAUDE.md.

## Inputs
See `huntarr-to-newtarr-migration.inputs.json`.

## Outputs
`{ success, merged, prUrl, configCarried, authentikMigrated, dnsCutover,
oldResourcesRemoved, leftoversZero, issueState, followUpIssues }`

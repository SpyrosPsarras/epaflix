Runs CLIProxyAPI (`https://help.router-for.me`) as a second Deployment in the
`remote-pi` namespace. It signs in to Gemini, Codex and Claude accounts and
re-exposes them behind OpenAI-, Anthropic- and Gemini-compatible HTTP APIs, so a
local client such as `omp` points at one endpoint instead of juggling provider
SDKs.

Closes #858

## What changed

- New `2-k3s/17.remote-pi/cliproxy/` - Deployment, Service, IngressRoute, the two
  CNPG CRs, a ksops generator, and the SOPS Secret file.
- `2-k3s/17.remote-pi/kustomization.yaml` - the four new resources, the generator,
  and an `images:` pin.
- `2-k3s/17.remote-pi/README.md` - append-only, the relay sections are untouched.
- `.github/instructions/pihole.instructions.md` - `cliproxy.epaflix.com` record,
  plus a note that `remote-pi.epaflix.com` is still missing from that table.
- `CLAUDE.md` - the DNS section claimed Pi-hole maps all `*.epaflix.com` to
  `192.168.10.101`. It does not - per-host `address=` records, no wildcard, and
  the `internal`-entry-point ones point at `192.168.10.102`.

## Design calls worth reviewing

**One Application, one namespace.** No new app-of-apps entry. The `remote-pi`
child Application already has `syncPolicy: {}`, so every change waits for a human
to press sync - exactly the gate we want for something holding provider
credentials. Cost: a sync touches the relay too. One replica each, `Recreate` on
both, no shared storage, so that is acceptable.

**State in Postgres, not a PVC.** `PGSTORE_DSN` selects the Postgres backend -
the app picks its store by which env var is present, in the order
`PGSTORE_DSN` > `OBJECTSTORE_ENDPOINT` > `GITSTORE_GIT_URL` > local files. It
creates `config_store` and `auth_store` itself and syncs both ways with local
disk. The connection target is the `postgres-rw` service, never an instance
ordinal - CNPG renumbers on failover and the primary is `postgres-cluster-10`
right now.

**No `namespace:` transformer on the kustomization.** `cliproxy/database.yaml`
and one of the two Secrets belong to `postgres-system`. A top-level transformer
would drag them into `remote-pi` and the operator would never see them.

**Security deviation from the relay pod - deliberate.** The proxy runs as root
with `readOnlyRootFilesystem: false`. It rewrites `/CLIProxyAPI/config.yaml` in
place inside its own WORKDIR (that is how a plaintext
`remote-management.secret-key` becomes a bcrypt hash on first start), and
`/root/.cli-proxy-api` can only be relocated through `auth-dir` in that same
config file, which is not readable until after first boot. Bounded by: no service
account token, no host mounts, all capabilities dropped, no privilege escalation,
`seccompProfile: RuntimeDefault`, and a 2Gi `ephemeral-storage` limit capping the
writable layer.

**`tcpSocket` probes, not HTTP.** `/v1/*` needs a client API key and
`/management.html` can 404 until the SPA download from GitHub finishes. A
listening socket is the only honest signal.

**Reclaim policies are `retain`** on both CNPG CRs, so a prune or a git removal
cannot drop the database or the role.

## Image pin

`docker.io/eceasy/cli-proxy-api` at
`newTag: v7.2.123@sha256:a6234015cd9e9429311356b7fa89c417041a4bb97286066d57012acd8cbb9798`.
Highest of 803 tags; `v7.2.123` and `latest` resolve to the same manifest index.
`newTag@digest` and not a bare `digest:` - a tagless entry leaves Renovate's
kustomize manager with no `currentValue` (#530, #587).

## Test plan

- [ ] `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/17.remote-pi`
      renders clean with the age key present and the Secret file encrypted
- [ ] `./.github/hooks/check-sops-encrypted.sh --full-tree` passes with the
      encrypted `cliproxy/cliproxy-secrets.enc.yaml` staged
- [ ] Pi-hole record `cliproxy.epaflix.com` → `192.168.10.102` added to
      `/etc/dnsmasq.d/10-epaflix.conf`, `dig cliproxy.epaflix.com @192.168.10.30
      +short` returns it
- [ ] Cloudflare DNS-only shadow record for `cliproxy.epaflix.com` exists, so the
      proxied wildcard does not hijack it
- [ ] First manual ArgoCD sync of the `remote-pi` Application completes, waves
      run in order (Secrets → DatabaseRole → Database → Deployment)
- [ ] `kubectl -n remote-pi get deploy cliproxy` shows 1/1 ready
- [ ] Database `cliproxy` and role `cliproxy` exist on the CNPG primary
      (resolved from `cnpg.io/instanceRole=primary`, not a hardcoded ordinal)
- [ ] Tables `config_store` and `auth_store` present in database `cliproxy`
      after first boot
- [ ] `/management.html` reachable over `https://cliproxy.epaflix.com` after
      setting `remote-management.allow-remote: true` through the port-forward
- [ ] Config and provider auth survive `kubectl -n remote-pi delete pod` - the
      emptyDir is gone, the Postgres rows are not
- [ ] `cliproxy.epaflix.com` unreachable from a genuinely external network, both
      normal DNS and a forced SNI/Host request to the public origin
- [ ] `omp models cliproxy` discovers the provider registered in
      `~/.omp/agent/models.yml` against `https://cliproxy.epaflix.com/v1`, using a
      key from the `api-keys:` list

## Post-merge

- [ ] Add `remote-pi.epaflix.com` → `192.168.10.102` to the Pi-hole record table
      and confirm the live `10-epaflix.conf` entry - tracked in #860
- [ ] Decide whether the `api-keys:` list should be seeded from SOPS instead of
      typed into the management UI. It lives in `config.yaml` and therefore in
      Postgres, which makes it a live-only value today - tracked in #861
- [ ] Rebuild-from-scratch check: confirm the documented port-forward OAuth flow
      is the only manual step needed to bring a fresh pod back to working -
      tracked in #862

## Notes for the reviewer

- The relay is untouched. `git diff origin/main` over `deployment.yaml`,
  `service.yaml`, `ingress.yaml`, `storage.yaml` and `namespace.yaml` is empty,
  and the README change is append-only.
- Provider OAuth login only completes against `127.0.0.1` - the redirect handed
  to the provider is a loopback URL. So every future account addition needs
  `kubectl -n remote-pi port-forward deploy/cliproxy 8317:8317`, not just the
  first one. Documented in the README as a standing limitation, not a bootstrap
  quirk.
- The management SPA is downloaded from GitHub releases at every startup into
  `MANAGEMENT_STATIC_PATH`. The pod needs egress to GitHub, and a `404` on
  `/management.html` right after a restart is normal.
- Restoring the CNPG cluster restores `auth_store`, but providers rotate tokens
  on their own schedule, so a non-fresh restore will normally need accounts
  re-authorised.

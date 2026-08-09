# GitHub Actions — CI

GitHub Actions runs **on GitHub's hosted runners**, not in the cluster. It is
separate from ArgoCD:

| System | Where | Role |
|--------|-------|------|
| GitHub Actions (`ci.yml`) | GitHub-hosted `ubuntu-latest` VM | **validates** a PR before merge |
| ArgoCD | in-cluster (`2-k3s/11.argocd/`) | **deploys** merged manifests |

Flow: open PR → Actions validates → you merge → ArgoCD deploys.

## Workflows in this directory

| Workflow | Trigger | Role |
|----------|---------|------|
| `ci.yml` | PR + push to main | the required `validate` check (details below) |
| `build-vpn-picker.yml` | PR/push touching `images/vpn-picker/**` | build + test the vpn-picker image; push to GHCR only on main, PR runs never push |
| `build-airvpn-bluetit.yml` | PR/push touching `images/airvpn-bluetit/**` | build the AirVPN Bluetit sidecar image; same PR-safe pattern |
| `k3s-stable-drift.yml` | schedule (Mon 08:00 UTC) | compares pinned k3s version against upstream stable and opens/updates a drift issue |
| `seerr-oidc-watch.yml` | schedule (Mon 08:20 UTC) | watches upstream `seerr-team/seerr` for native OIDC and reopens #218 when it lands |
| `upstream-release-watch.yml` | schedule (Mon 08:40 UTC) | data-driven matrix watch: per row, reopens a closed `blocked-external` issue once upstream cuts a release past the pinned version. Adding an issue is one matrix row (#270 = lingarr) |

## `ci.yml` — the `validate` check

A **secret-free** gate that validates repository policy and Renovate's change
surface without ever needing the SOPS age key. Its validation groups are:

1. **Tracked-file policy** — rejects tracked paths matched by `.gitignore`.
2. **Plaintext Secret guard** — runs the committed throwaway-repository fixture
   suite, then parses every tracked YAML blob in full-tree mode. Placeholder
   Secret templates are content-classified; SOPS protection is checked per
   document. This is the server-side complement to the optional local hook.
3. **YAML parse check** — every non-chart `*.yaml`/`*.yml` must parse. Syntax
   only, no style rules (so it never false-fails on existing formatting).
4. **Helm chart pins resolve** — for every `helmCharts:` entry in any
   `2-k3s/**/kustomization.yaml`, `helm pull <chart>@<version>` must succeed.
   This is what catches a Renovate chart bump to a version that does not exist.
5. **kustomize build (sops-free dirs)** — full offline render of the overlays
   that use neither Helm nor sops/ksops:
   `01.kube-vip`, `03.kube-vip-cloud-provider`, `04.coredns`,
   `11.argocd/apps`, `12.renovate`, `maintenance`,
   `maintenance/system-upgrade/controller`.

### Why sops/Helm dirs are not fully rendered

Most chart dirs (`02.cert-manager`, `05.traefik-deployment`,
`07.authentik-deployment`, `10.observability`, `11.argocd`,
`11.argocd/image-updater`, plus `06.postgres` and `08.servarr`) decrypt secrets via a ksops/sops generator. A full
`kustomize build` of those needs the **age private key**, which is
deliberately **withheld from CI** — putting the key that decrypts every
cluster secret onto a GitHub runner is not acceptable. Those dirs are covered
indirectly by chart-pin validation, YAML parsing, and full-tree Secret
validation.

## Security model (public repo)

This repo is public, so anyone can fork and open a PR. The workflow is built
so a fork PR is harmless:

- **`pull_request` trigger, not `pull_request_target`.** Fork PRs get a
  read-only `GITHUB_TOKEN` and **no repo secrets** by GitHub default.
- **No secrets used at all** — nothing to leak even in same-repo runs.
- **`permissions: contents: read`** — cannot push, tag, or merge.
- **Fork-guard** — the job only runs for PRs whose head branch is in *this*
  repo (plus pushes to `main`):

  ```yaml
  if: >-
    github.event_name == 'push' ||
    github.event.pull_request.head.repo.full_name == github.repository
  ```

  Fork PRs therefore execute **nothing** — no fork-authored `ci.yml` ever runs
  on the runner. Combined with the repo Actions setting
  *fork-PR approval = all external contributors*, an outside PR can neither run
  workflows nor reach anything.
- Runs on **ephemeral GitHub VMs** — never touches Proxmox, the cluster, or any
  kubeconfig.

## Merge policy — no auto-merge

`validate` is **informational only**:

- It is **not** a required status check; `main` branch protection is untouched.
- **Auto-merge is off.** Although `renovate.json` marks `patch` updates
  `automerge: true`, GitHub native auto-merge does not fire because there is no
  required check — by design.
- PRs (including Renovate's) are **merged manually** after a glance at the
  green/red `validate` result.

If hands-off patch→deploy is ever wanted later: mark `validate` a required
check on `main` and enable *Allow auto-merge*. Note this repo has
`enforce_admins: true`, so a required check would gate the owner's own merges
too.

## Local equivalent

Reproduce the check before pushing:

```bash
# Secret fixtures plus all tracked YAML blobs (needs Python + PyYAML)
./.github/hooks/test-check-sops-encrypted.sh
./.github/hooks/check-sops-encrypted.sh --full-tree

# YAML parses
python3 -c 'import glob,yaml,sys; [list(yaml.safe_load_all(open(f))) for f in glob.glob("**/*.y*ml",recursive=True)]'

# chart pins resolve (needs helm)
# for each helmCharts entry: helm pull <name> --repo <repo> --version <ver>

# sops-free overlays build (kustomize, or `kubectl kustomize`)
for d in 2-k3s/01.kube-vip 2-k3s/03.kube-vip-cloud-provider 2-k3s/04.coredns \
         2-k3s/11.argocd/apps 2-k3s/12.renovate 2-k3s/maintenance \
         2-k3s/maintenance/system-upgrade/controller; do
  kubectl kustomize "$d" >/dev/null && echo "OK $d"
done
```

See also: `2-k3s/11.argocd/README.md` (sync policy), issue #88.

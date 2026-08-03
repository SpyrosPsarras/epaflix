# Process: deliver issue #148 — standardize k3s masters on `config.yaml`

## Goal
Make `/etc/rancher/k3s/config.yaml` the **single source of truth** for k3s server+kubelet
config on every master. master-52 currently carries its args inline in the k3sup-generated
systemd `ExecStart` *and* (since the #121 fix) in a partial `config.yaml` — two sources of
truth, with CLI flags overriding the file. Move the inline args into `config.yaml` and reduce
`ExecStart` to a bare `k3s server`, bringing 51/52/53 to a consistent, upgrade-durable mechanism.

This is a **refactor of where args live, not what args are in effect** — the running k3s
server/kubelet behaviour must be byte-for-byte equivalent before and after (the #121 metrics
binds on `0.0.0.0`, the kubelet `resolv-conf=/etc/k3s-resolv.conf` pin, node-ip / advertise /
tls-sans / etcd-args / disable / node-taint / write-kubeconfig-mode, and the join identity).

## Why a babysitter run
Touches the live control plane: a `systemctl restart k3s` on an etcd member. etcd quorum is
2/3, so a mistake risks quorum. The user profile is **low breakpoint tolerance** with
`alwaysBreakOn: [deploy, destructive-git]`, so the live restart and the PR merge are gated
behind one explicit owner approval, with automatic `.bak` rollback on any unhealthy master.

## Phases
1. **inspect** (read-only) — dump `config.yaml` + systemd `ExecStart` + env-file keys + k3s
   version + node Ready + etcd health on all three masters; classify which masters still carry
   inline `ExecStart` args; derive each one's consolidated `config.yaml` and target bare
   `ExecStart`, plus the lowest-risk join-identity plan. Adapts to ground truth (may find 51/53
   also need it, not just 52).
2. **prepare-docs** — update `.github/instructions/k3s.instructions.md` to document the
   standardized mechanism + canonical consolidated `config.yaml`, and remove the "52 has no
   config.yaml" divergence framing. Branch + local commit, **no push**.
3. **owner gate** (`deploy` + `destructive-git`) — one approval authorizing **both** the live
   per-master reconciliation and the subsequent PR merge; shows per-master before→after and the
   docs diff. Retry/refine loop on "request changes"; abort = no mutation.
4. **live-rollout** — per diverging master, **one at a time**: back up `config.yaml` +
   `k3s.service` to `.bak-148`, write the consolidated `config.yaml`, reduce `ExecStart`,
   `daemon-reload`, `restart k3s`, health-gate (node Ready, etcd 3/3, #121 metrics ports,
   resolv-conf), and prove no behaviour change. Hard-stop + automatic rollback on failure;
   recovery gate on partial failure (the docs PR is not merged until live succeeds).
5. **publish-merge** — rebase onto `origin/main`, `push --force-with-lease`, open PR, wait for
   the required `validate` check, `gh pr merge --merge` (merge-commit per Epaflix policy).
6. **post-verify** — all masters `config.yaml`-only (no inline `ExecStart` args), nodes Ready,
   etcd 3/3, #121 metrics still up, resolv-conf pin intact, CoreDNS healthy. Recovery gate on
   failure.
7. **closeout** — close #148, tick the PR test plan by editing the PR body (never a new
   comment), and open a `#44` follow-up to verify the drop-in survives a
   system-upgrade-controller-driven k3s upgrade.

## Deliverables
- **Live:** masters standardized on `config.yaml` (out-of-band; k3s host config is not ArgoCD-managed).
- **Git:** docs PR updating `k3s.instructions.md`.
- **Issues:** #148 closed; `#44`-linked upgrade-durability follow-up opened.

## Guardrails honored
- etcd quorum: one master at a time, pre-check 3/3 before each.
- Reversible: `.bak-148` of both files per master; auto-rollback on unhealthy.
- No secrets in git/logs (join token/URL kept in env-file or redacted).
- Epaflix merge policy: rebase + `--force-with-lease` + `validate` + `gh pr merge --merge`.
- Follow-up issue opened per CLAUDE.md before closing the thread.

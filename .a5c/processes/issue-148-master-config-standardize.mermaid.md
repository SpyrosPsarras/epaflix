# Issue #148 — Standardize k3s masters on `/etc/rancher/k3s/config.yaml`

```mermaid
flowchart TD
    A["1 · INSPECT (read-only)<br/>dump config.yaml + systemd ExecStart + k3s version<br/>+ node/etcd health on 51/52/53<br/>derive per-master consolidated config.yaml + target bare ExecStart"] --> B{"any master with<br/>inline ExecStart args?"}
    B -- "no (already clean)" --> C["2 · PREPARE DOCS<br/>k3s.instructions.md standardization note<br/>branch + local commit (no push)"]
    B -- "yes" --> C
    A --> Q{"etcd 3/3 healthy?"}
    Q -- "no" --> QG["owner gate:<br/>abort or proceed-anyway"]
    QG -- "abort" --> X1["STOP — no mutation"]

    C --> G{{"OWNER GATE (deploy + destructive-git)<br/>authorizes BOTH:<br/>live per-master reconcile + PR merge<br/>shows before→after + docs diff"}}
    G -- "Request changes" --> C
    G -- "Abort" --> X2["STOP — no mutation"]
    G -- "Approve" --> D

    D["3 · LIVE ROLLOUT (one master at a time)<br/>backup .bak → write consolidated config.yaml<br/>→ reduce ExecStart to bare server → daemon-reload<br/>→ restart k3s → health-gate (Ready + etcd 3/3<br/>+ #121 metrics + resolv-conf) → prove no behaviour change"] --> E{"all reconciled +<br/>healthy + unchanged?"}
    E -- "no" --> R["recovery gate:<br/>retry / stop (rolled back)"]
    R -- "stop" --> X3["STOP — docs NOT merged"]
    R -- "retry" --> D
    E -- "yes" --> F

    F["4 · PUBLISH + MERGE<br/>rebase onto origin/main → push --force-with-lease<br/>→ PR → wait validate → gh pr merge --merge"] --> H
    H["5 · POST-VERIFY<br/>parity: all masters config.yaml-only<br/>nodes Ready + etcd 3/3 + #121 metrics up<br/>+ resolv-conf intact + CoreDNS healthy"] --> I{"verified?"}
    I -- "no" --> V["recovery gate:<br/>re-verify / accept / stop"]
    V -- "stop" --> X4["STOP"]
    V -- "re-verify" --> H
    I -- "yes" --> J

    J["6 · CLOSEOUT<br/>close #148 + tick PR test plan (edit body)<br/>open #44 upgrade-durability follow-up"] --> K(["DONE"])
```

**Safety:** etcd quorum is 2/3 — masters are reconciled **one at a time**, never two restarts at once. Every change is a **pure refactor** (where args live, not what they are): config.yaml + systemd unit are backed up to `.bak-148` and **rolled back automatically** if a master does not return healthy. The single owner gate covers the live control-plane restart **and** the docs PR merge (low breakpoint tolerance; `alwaysBreakOn: deploy, destructive-git`).

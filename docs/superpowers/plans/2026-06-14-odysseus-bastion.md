# Odysseus Bastion Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a dedicated execution sandbox VM (`odysseus-bastion`, 1043/.43) that Odysseus drives over SSH, sharing a TrueNAS NFS workspace mounted into both the Odysseus pod and the VM, reachable at `bastion.epaflix.com`.

**Architecture:** Two channels — NFS (shared files: pod reads the project / writes output; bastion sees the same tree) and SSH (execution: builds, starting servers run on the bastion). The pod gets a dedicated, bastion-only SSH key; the bastion holds the broad homelab keys. Web output served by Odysseus-launched servers, reachable via a Pi-hole LAN record.

**Tech Stack:** Proxmox `qm` + cloud-init (Ubuntu 24.04), TrueNAS `midclt` (ZFS dataset + NFS export), k3s NFS PV/PVC, SOPS+age encrypted Secret, Pi-hole dnsmasq, ArgoCD (odysseus Application), Odysseus scoped `/api/codex` (memory/documents).

**Reference:** Design spec `docs/superpowers/specs/2026-06-14-odysseus-bastion-design.md`. User-VM flow `1-proxmox/user-vms/README.md`. SOPS recipe `.github/instructions/sops.instructions.md`.

**Conventions reminder (CLAUDE.md):** Merge-commit + mandatory-rebase PR policy; open a `gh issue` for every follow-up; never commit plaintext secrets (pre-commit guard refuses them + force-added gitignored files); log out-of-band commands to `.history/`.

---

## File Structure

**Committed (GitOps):**
- `1-proxmox/user-vms/odysseus-bastion-ssh-config` — CREATE — SSH config snippet for the bastion (workstation convenience).
- `1-proxmox/user-vms/README.md` — MODIFY — add `odysseus-bastion` to the inventory table + a short "bastion" note.
- `0-truenas/` — MODIFY/CREATE — document the `apps/odysseus-bastion` dataset + NFS export (match existing NFS doc style in this dir).
- `2-k3s/13.odysseus/odysseus-bastion-pv.yaml` — CREATE — NFS PersistentVolume + PVC for the workspace.
- `2-k3s/13.odysseus/odysseus-ssh-config.yaml` — CREATE — ConfigMap with the pod's `~/.ssh/config` (`bastion` alias).
- `2-k3s/13.odysseus/odysseus.yaml` — MODIFY — mount workspace PVC at `/workspace`; mount `id_bastion` key at `/app/.ssh/id_bastion`; mount ssh-config ConfigMap.
- `2-k3s/13.odysseus/odysseus-secrets.enc.yaml` — MODIFY — add `ODYSSEUS_BASTION_SSH_KEY` (the private key).
- `2-k3s/13.odysseus/odysseus-data-seed.enc.yaml` — MODIFY — add/patch `settings.json` with the durable bastion instruction.
- `2-k3s/13.odysseus/kustomization.yaml` — MODIFY — add the new PV/PVC + ssh-config ConfigMap resources.
- `pihole` docs — MODIFY — record `bastion.epaflix.com` in the documented dnsmasq config.

**Out-of-band (operational; logged to `.history/`, NOT committed):**
- VM creation on evanthoulaki, TrueNAS dataset/export creation, workspace seeding (git clone + `secrets.yml` + homelab keys), live Pi-hole record, bastion `authorized_keys`, Odysseus runtime memory/doc.

---

## Phase 1 — TrueNAS shared workspace (foundation)

### Task 1: Create the `apps/odysseus-bastion` dataset

**Files:** none committed (out-of-band; log to `.history/2026-06-14-odysseus-bastion.md`).

- [ ] **Step 1: Verify the `apps` pool and current NFS exports (baseline)**

Run:
```bash
ssh truenas_admin@192.168.10.200 "midclt call pool.dataset.query '[[\"name\",\"=\",\"apps\"]]' | python3 -m json.tool | head -5"
ssh truenas_admin@192.168.10.200 "midclt call sharing.nfs.query | python3 -c 'import sys,json;[print(s[\"path\"],s[\"id\"]) for s in json.load(sys.stdin)]'"
```
Expected: the `apps` dataset exists; a list of current NFS share paths/ids (note them so we add, not clobber).

- [ ] **Step 2: Create the dataset**

Run:
```bash
ssh truenas_admin@192.168.10.200 "midclt call pool.dataset.create '{\"name\":\"apps/odysseus-bastion\",\"share_type\":\"GENERIC\"}'"
```
Expected: JSON of the new dataset (no error). If it already exists, skip.

- [ ] **Step 3: Set ownership to 1000:1000 + create subdirs**

Run:
```bash
ssh truenas_admin@192.168.10.200 "sudo chown -R 1000:1000 /mnt/apps/odysseus-bastion && sudo -u $(id -un) true; sudo mkdir -p /mnt/apps/odysseus-bastion/repo /mnt/apps/odysseus-bastion/work && sudo chown -R 1000:1000 /mnt/apps/odysseus-bastion && ls -lan /mnt/apps/odysseus-bastion"
```
Expected: `repo/` and `work/` owned by uid/gid `1000`.

- [ ] **Step 4: Log to `.history/`**

Append the commands + outputs to `.history/2026-06-14-odysseus-bastion.md` (create it; it is git-tracked as `.md` but its content is operational — keep secrets out).

### Task 2: Create the NFS export (scoped to k3s workers + bastion)

**Files:** none committed (out-of-band).

- [ ] **Step 1: Create the NFS share**

`hosts` = the four k3s worker external IPs + the bastion. `maproot_user/group` = 1000 so root writes land as uid 1000.
```bash
ssh truenas_admin@192.168.10.200 "midclt call sharing.nfs.create '{
  \"path\":\"/mnt/apps/odysseus-bastion\",
  \"comment\":\"odysseus-bastion workspace\",
  \"hosts\":[\"192.168.10.61\",\"192.168.10.62\",\"192.168.10.63\",\"192.168.10.65\",\"192.168.10.43\"],
  \"maproot_user\":\"1000\",\"maproot_group\":\"1000\",
  \"security\":[]
}'"
```
Expected: JSON of the created share. (If the running TrueNAS rejects numeric maproot, use the matching username — verify with `midclt call user.query '[["uid","=",1000]]'`.)

- [ ] **Step 2: Verify the export is live**

Run from a k3s worker (has the right source IP):
```bash
ssh k3s-worker-61 "showmount -e 192.168.10.200 | grep odysseus-bastion"
```
Expected: `/mnt/apps/odysseus-bastion` appears in the export list.

- [ ] **Step 3: Log to `.history/`**

---

## Phase 2 — Bastion VM (`odysseus-bastion`, 1043 / .43)

> Follow `1-proxmox/user-vms/README.md` exactly, substituting `USER=odysseus-bastion`-style values. Below are the deltas/values; the README is the source of truth for the template-transfer mechanics.

### Task 3: Provision the VM

**Files:** none committed (out-of-band).

- [ ] **Step 1: Confirm the Ubuntu 24.04 template raw is present on evanthoulaki**

Run:
```bash
ssh root@192.168.10.11 "ls -lh /tmp/ubuntu-24.04-template.raw 2>/dev/null || echo MISSING"
```
Expected: a ~3.5 GiB file. If `MISSING`, re-run README §2 (template transfer from takaros) before continuing.

- [ ] **Step 2: Generate the bastion-login keypair for cloud-init's `ciuser`**

This is the key the `ubuntu` user logs in with (separate from the pod→bastion key in Task 8).
```bash
ssh-keygen -t ed25519 -C "odysseus-bastion@epaflix.com" -f /tmp/odysseus-bastion_ed25519 -N ""
```
Expected: keypair at `/tmp/odysseus-bastion_ed25519{,.pub}`.

- [ ] **Step 3: Create + configure the VM (1043 / .43)**

```bash
VMID=1043; IP=192.168.10.43
ssh root@192.168.10.11 "qm create ${VMID} --name odysseus-bastion --memory 8192 --cores 4 --sockets 1 --cpu host --ostype l26 --scsihw virtio-scsi-pci --net0 virtio,bridge=vmbr0 --agent enabled=1 --onboot 1 --numa 0"
ssh root@192.168.10.11 "qm importdisk ${VMID} /tmp/ubuntu-24.04-template.raw local-raid --format raw"
ssh root@192.168.10.11 "qm set ${VMID} --scsihw virtio-scsi-pci --scsi0 local-raid:vm-${VMID}-disk-0 --boot order=scsi0 --bootdisk scsi0 --ide2 local-raid:cloudinit"
scp /tmp/odysseus-bastion_ed25519.pub root@192.168.10.11:/tmp/odysseus-bastion.pub
ssh root@192.168.10.11 "qm set ${VMID} --ciuser ubuntu --sshkeys /tmp/odysseus-bastion.pub --ipconfig0 ip=${IP}/24,gw=192.168.10.1 --nameserver 192.168.10.30 --searchdomain epaflix.com"
ssh root@192.168.10.11 "qm resize ${VMID} scsi0 60G && qm start ${VMID}"
```
Expected: each command returns without error; `qm start` boots the VM.

- [ ] **Step 4: Verify reachability (cloud-init takes ~1-2 min)**

```bash
until ping -c1 -W1 192.168.10.43 >/dev/null 2>&1; do sleep 5; done
ssh -i /tmp/odysseus-bastion_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new ubuntu@192.168.10.43 "echo ok && hostname && lsb_release -ds"
```
Expected: `ok`, hostname `odysseus-bastion`, `Ubuntu 24.04...`.

- [ ] **Step 5: Log to `.history/`**

### Task 4: Base packages + NFS mount on the bastion

**Files:** none committed (out-of-band).

- [ ] **Step 1: Install tooling + NFS client + fail2ban**

```bash
ssh -i /tmp/odysseus-bastion_ed25519 -o IdentitiesOnly=yes ubuntu@192.168.10.43 "sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-common git python3 python3-venv nodejs npm fail2ban"
```
Expected: installs cleanly.

- [ ] **Step 2: Mount the workspace via fstab**

```bash
ssh -i /tmp/odysseus-bastion_ed25519 -o IdentitiesOnly=yes ubuntu@192.168.10.43 "sudo mkdir -p /workspace && echo '192.168.10.200:/mnt/apps/odysseus-bastion /workspace nfs defaults,_netdev,vers=4 0 0' | sudo tee -a /etc/fstab && sudo mount -a && df -h /workspace && ls -lan /workspace"
```
Expected: `/workspace` mounted; `repo/` and `work/` visible, owned uid/gid 1000.

- [ ] **Step 3: Verify write-through as ubuntu (uid 1000)**

```bash
ssh -i /tmp/odysseus-bastion_ed25519 -o IdentitiesOnly=yes ubuntu@192.168.10.43 "echo hello > /workspace/work/_probe.txt && cat /workspace/work/_probe.txt && rm /workspace/work/_probe.txt"
```
Expected: `hello`, then file removed (no permission error).

- [ ] **Step 4: Log to `.history/`**

### Task 5: Hardening (inbound SSH + fail2ban) — keep workstation access

**Files:** none committed (out-of-band).

- [ ] **Step 1: Restrict inbound SSH to the LAN + enable fail2ban sshd jail**

```bash
ssh -i /tmp/odysseus-bastion_ed25519 -o IdentitiesOnly=yes ubuntu@192.168.10.43 "sudo bash -c '
cat > /etc/ssh/sshd_config.d/epaflix.conf <<CONF
PasswordAuthentication no
PermitRootLogin no
AllowUsers ubuntu
CONF
systemctl restart ssh
cat > /etc/fail2ban/jail.d/sshd.local <<JAIL
[sshd]
enabled = true
maxretry = 4
bantime = 1h
JAIL
systemctl enable --now fail2ban && fail2ban-client status sshd'"
```
Expected: `sshd` jail status prints; SSH still works (pubkey).

- [ ] **Step 2: Add spy's workstation key to `authorized_keys` (direct access)**

```bash
WS_PUB=$(cat ~/.ssh/id_ed25519.pub)
ssh -i /tmp/odysseus-bastion_ed25519 -o IdentitiesOnly=yes ubuntu@192.168.10.43 "echo '$WS_PUB' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && wc -l ~/.ssh/authorized_keys"
```
Expected: authorized_keys line count increments.

- [ ] **Step 3: Verify workstation can connect with its default key**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "echo workstation-ok"
```
Expected: `workstation-ok`.

- [ ] **Step 4: Log to `.history/`**

---

## Phase 3 — Seed the workspace + homelab keys

### Task 6: Clone the project + place git-ignored files into the workspace

**Files:** none committed (out-of-band; this writes secrets onto NFS only).

- [ ] **Step 1: Clone the repo into `/workspace/repo` from the bastion**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "git clone https://github.com/SpyrosPsarras/epaflix.git /workspace/repo 2>&1 | tail -2 && ls /workspace/repo | head"
```
Expected: repo cloned; top-level dirs (`0-truenas`, `1-proxmox`, `2-k3s`, ...) listed. (HTTPS clone needs no creds for read; if private, use the GitHub PAT from `secrets.yml` once it's placed in Step 2 — re-run with the token URL.)

- [ ] **Step 2: Copy `secrets.yml` from the workstation into its normal path**

```bash
scp -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 .github/instructions/secrets.yml ubuntu@192.168.10.43:/workspace/repo/.github/instructions/secrets.yml
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "chmod 600 /workspace/repo/.github/instructions/secrets.yml && head -1 /workspace/repo/.github/instructions/secrets.yml"
```
Expected: first line of `secrets.yml` prints (file present, mode 600).

- [ ] **Step 3: Verify `git status` ignores the secret (no accidental tracking)**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "cd /workspace/repo && git check-ignore -v .github/instructions/secrets.yml"
```
Expected: `.gitignore:... secrets.yml` (ignored — safe).

- [ ] **Step 4: Log to `.history/`** (record WHAT was copied, never the values).

### Task 7: Install homelab SSH keys + config on the bastion

**Files:** none committed (out-of-band).

- [ ] **Step 1: Build a homelab-only SSH config + copy the referenced keys**

Homelab hosts only (exclude work: `davidhorn`, `ft4`, `dh-demo`, `gc1`, `ezhellas`, `alex-tv`, `webos-tv`, `nick`). Create a trimmed config locally and push it with just the keys it references (`id_ed25519`, `id_rsa`, and any host-specific homelab keys).
```bash
# Build trimmed config: keep takaros, evanthoulaki, k3s-master/worker-*, epaflix-jumpbox, and add explicit truenas/pihole/proxmox entries.
awk '/^Host (takaros|evanthoulaki|k3s-|epaflix-jumpbox|192\.168\.10\.(2|3))/{p=1} /^Host /{if($0!~/takaros|evanthoulaki|k3s-|epaflix-jumpbox|192\.168\.10\.(2|3)/)p=0} p' ~/.ssh/config > /tmp/homelab-ssh-config
scp -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 /tmp/homelab-ssh-config ubuntu@192.168.10.43:/tmp/homelab-ssh-config
for k in id_ed25519 id_rsa; do scp -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ~/.ssh/$k ~/.ssh/$k.pub ubuntu@192.168.10.43:/tmp/; done
```
Expected: config + keys copied to `/tmp` on the bastion. (Review `/tmp/homelab-ssh-config` before installing — confirm no work hosts slipped in.)

- [ ] **Step 2: Install into `ubuntu`'s `~/.ssh` with correct perms**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  cat /tmp/homelab-ssh-config >> ~/.ssh/config && chmod 600 ~/.ssh/config
  mv /tmp/id_ed25519 /tmp/id_rsa ~/.ssh/ 2>/dev/null; mv /tmp/id_ed25519.pub /tmp/id_rsa.pub ~/.ssh/ 2>/dev/null
  chmod 600 ~/.ssh/id_* && chmod 644 ~/.ssh/*.pub
  grep -c '^Host' ~/.ssh/config"
```
Expected: a small Host count (homelab only).

- [ ] **Step 3: Verify the bastion can reach infra by alias**

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "ssh -o StrictHostKeyChecking=accept-new takaros hostname && ssh -o StrictHostKeyChecking=accept-new k3s-master-51 hostname"
```
Expected: `takaros` and a master hostname print.

- [ ] **Step 4: Log to `.history/`**

---

## Phase 4 — Pi-hole DNS

### Task 8: Add `bastion.epaflix.com → 192.168.10.43`

**Files:** MODIFY the pihole instruction doc to record the entry (the live edit is out-of-band on Pi-hole; golden rule = dnsmasq.d only).

- [ ] **Step 1: Add the dnsmasq record + full FTL restart**

```bash
ssh root@192.168.10.30 "echo 'address=/bastion.epaflix.com/192.168.10.43' >> /etc/dnsmasq.d/10-epaflix.conf && systemctl restart pihole-FTL"
```
Expected: command succeeds.

- [ ] **Step 2: Verify resolution**

```bash
dig bastion.epaflix.com @192.168.10.30 +short
```
Expected: `192.168.10.43`.

- [ ] **Step 3: Record in repo docs + commit**

Add the `bastion.epaflix.com` line to wherever k3s/service DNS records are documented (per `pihole.instructions.md`).
```bash
git add -A && git commit -m "docs(pihole): record bastion.epaflix.com -> 192.168.10.43"
```

- [ ] **Step 4: Log to `.history/`**

---

## Phase 5 — Odysseus pod wiring (GitOps)

### Task 9: Determine the container HOME (for `~/.ssh`)

**Files:** none committed (investigation).

- [ ] **Step 1: Inspect HOME in the running pod**

```bash
POD=$(ssh k3s-master-51 "kubectl get pods -n odysseus -l app=odysseus -o name | head -1")
ssh k3s-master-51 "kubectl exec -n odysseus ${POD#pod/} -c odysseus -- sh -c 'echo HOME=\$HOME; ls -ld \$HOME'"
```
Expected: prints `HOME=` (likely `/app` or `/root`). **Record this value** — it determines the mount path for `config` so `ssh bastion` resolves the alias. The private key mount is fixed at `/app/.ssh/id_bastion` regardless, and the canonical instruction uses explicit flags so it works even if the alias path is off.

### Task 10: Generate the pod→bastion key, encrypt it, authorize it

**Files:**
- Modify: `2-k3s/13.odysseus/odysseus-secrets.enc.yaml`

- [ ] **Step 1: Generate the dedicated keypair**

```bash
ssh-keygen -t ed25519 -C "odysseus-pod@bastion" -f /tmp/id_bastion -N ""
```
Expected: `/tmp/id_bastion{,.pub}`.

- [ ] **Step 2: Add the private key to the SOPS secret**

Decrypt, add the key, re-encrypt (per `.github/instructions/sops.instructions.md`). The Secret's `stringData` gains `ODYSSEUS_BASTION_SSH_KEY`.
```bash
cd 2-k3s/13.odysseus
sops -d odysseus-secrets.enc.yaml > /tmp/odysseus-secrets-plaintext.yaml
# Add under stringData: (literal block scalar)
python3 - <<'PY'
import sys
p="/tmp/odysseus-secrets-plaintext.yaml"
key=open("/tmp/id_bastion").read()
import re
txt=open(p).read()
block="  ODYSSEUS_BASTION_SSH_KEY: |\n" + "".join("    "+l for l in key.splitlines(keepends=True))
# insert after 'stringData:' line
txt=re.sub(r"(stringData:\n)", r"\1"+block+"\n", txt, count=1)
open(p,"w").write(txt)
print("patched")
PY
sops -e /tmp/odysseus-secrets-plaintext.yaml > odysseus-secrets.enc.yaml
rm -f /tmp/odysseus-secrets-plaintext.yaml
```
Expected: `odysseus-secrets.enc.yaml` re-encrypted (contains `sops:` block). Verify: `sops -d odysseus-secrets.enc.yaml | grep ODYSSEUS_BASTION_SSH_KEY`.

- [ ] **Step 3: Authorize the pubkey on the bastion**

```bash
BPUB=$(cat /tmp/id_bastion.pub)
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "echo '$BPUB' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
```
Expected: no error.

- [ ] **Step 4: Commit the encrypted secret**

```bash
git add 2-k3s/13.odysseus/odysseus-secrets.enc.yaml
git commit -m "feat(odysseus): add SOPS-encrypted pod->bastion ssh key"
```
(The pre-commit SOPS guard must pass — file is encrypted.)

### Task 11: NFS PV/PVC for the workspace

**Files:**
- Create: `2-k3s/13.odysseus/odysseus-bastion-pv.yaml`
- Modify: `2-k3s/13.odysseus/kustomization.yaml`

- [ ] **Step 1: Write the PV + PVC**

```yaml
# 2-k3s/13.odysseus/odysseus-bastion-pv.yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: odysseus-bastion-workspace
spec:
  capacity:
    storage: 60Gi
  accessModes: ["ReadWriteMany"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  mountOptions: ["vers=4", "_netdev"]
  nfs:
    server: 192.168.10.200
    path: /mnt/apps/odysseus-bastion
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: odysseus-bastion-workspace
  namespace: odysseus
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ""
  volumeName: odysseus-bastion-workspace
  resources:
    requests:
      storage: 60Gi
```

- [ ] **Step 2: Add to kustomization resources**

In `2-k3s/13.odysseus/kustomization.yaml`, add under `resources:`:
```yaml
  - odysseus-bastion-pv.yaml
```

- [ ] **Step 3: Validate the kustomize build**

```bash
cd 2-k3s/13.odysseus && kustomize build --enable-alpha-plugins --enable-exec . >/dev/null && echo BUILD-OK
```
Expected: `BUILD-OK` (no schema errors).

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/13.odysseus/odysseus-bastion-pv.yaml 2-k3s/13.odysseus/kustomization.yaml
git commit -m "feat(odysseus): NFS PV/PVC for bastion workspace"
```

### Task 12: Pod ssh-config ConfigMap

**Files:**
- Create: `2-k3s/13.odysseus/odysseus-ssh-config.yaml`
- Modify: `2-k3s/13.odysseus/kustomization.yaml`

- [ ] **Step 1: Write the ConfigMap**

```yaml
# 2-k3s/13.odysseus/odysseus-ssh-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: odysseus-ssh-config
  namespace: odysseus
data:
  config: |
    Host bastion
        HostName 192.168.10.43
        User ubuntu
        IdentityFile /app/.ssh/id_bastion
        IdentitiesOnly yes
        StrictHostKeyChecking accept-new
        UserKnownHostsFile /app/.ssh/known_hosts
```

- [ ] **Step 2: Add to kustomization resources**

```yaml
  - odysseus-ssh-config.yaml
```

- [ ] **Step 3: Commit**

```bash
git add 2-k3s/13.odysseus/odysseus-ssh-config.yaml 2-k3s/13.odysseus/kustomization.yaml
git commit -m "feat(odysseus): pod ssh config with bastion alias"
```

### Task 13: Wire the volumes into the Deployment

**Files:**
- Modify: `2-k3s/13.odysseus/odysseus.yaml`

- [ ] **Step 1: Add volumeMounts to the `odysseus` container**

Under the container's `volumeMounts:` (currently only `data` at `/app/data`), add:
```yaml
            - name: workspace
              mountPath: /workspace
            - name: bastion-ssh-key
              mountPath: /app/.ssh/id_bastion
              subPath: id_bastion
              readOnly: true
            - name: bastion-ssh-config
              mountPath: <HOME>/.ssh/config   # <HOME> from Task 9 (e.g. /app or /root)
              subPath: config
              readOnly: true
```
> If `<HOME>` differs from `/app`, also ensure the `id_bastion` IdentityFile path in the ConfigMap (Task 12) stays `/app/.ssh/id_bastion` (absolute, HOME-independent).

- [ ] **Step 2: Add the volumes to the pod spec**

Under `volumes:` (currently `data` PVC + `seed` secret), add:
```yaml
        - name: workspace
          persistentVolumeClaim:
            claimName: odysseus-bastion-workspace
        - name: bastion-ssh-key
          secret:
            secretName: odysseus-secrets
            items:
              - key: ODYSSEUS_BASTION_SSH_KEY
                path: id_bastion
                mode: 0400
        - name: bastion-ssh-config
          configMap:
            name: odysseus-ssh-config
            items:
              - key: config
                path: config
```

- [ ] **Step 3: Validate kustomize build**

```bash
cd 2-k3s/13.odysseus && kustomize build --enable-alpha-plugins --enable-exec . >/dev/null && echo BUILD-OK
```
Expected: `BUILD-OK`.

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/13.odysseus/odysseus.yaml
git commit -m "feat(odysseus): mount bastion workspace + ssh key into pod"
```

---

## Phase 6 — Durable bastion instruction + runtime nudge

### Task 14: Seed the "work on the bastion" instruction into Odysseus config

**Files:**
- Modify: `2-k3s/13.odysseus/odysseus-data-seed.enc.yaml`

- [ ] **Step 1: Inspect the current seed payload**

```bash
cd 2-k3s/13.odysseus && sops -d odysseus-data-seed.enc.yaml | grep -E 'settings.json|auth.json' 
```
Expected: see which JSON files are seeded. Identify where a custom instruction/system-prompt field lives in `settings.json` (read the decrypted `settings.json` value fully to find the right key — Odysseus stores agent instructions there).

- [ ] **Step 2: Patch `settings.json` with the bastion instruction**

Decrypt, set the instruction text, re-encrypt. Instruction text:
> "Execution environment: do NOT run build/serve tasks in your own container. For anything that runs code or starts a server, use the bastion over SSH: `ssh -i /app/.ssh/id_bastion -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new ubuntu@192.168.10.43 '<command>'` (or `ssh bastion '<command>'`). The shared workspace is `/workspace` (same path on pod and bastion). Put web output in `/workspace/work` and start servers there; they are reachable at http://bastion.epaflix.com:<port>. The full project, including secrets.yml, is at `/workspace/repo`."

```bash
cd 2-k3s/13.odysseus
sops -d odysseus-data-seed.enc.yaml > /tmp/seed-plaintext.yaml
# Edit the settings.json value to include the instruction (use the key discovered in Step 1).
# (Manual/scripted JSON edit — keep it valid JSON inside the YAML block scalar.)
$EDITOR /tmp/seed-plaintext.yaml
sops -e /tmp/seed-plaintext.yaml > odysseus-data-seed.enc.yaml
rm -f /tmp/seed-plaintext.yaml
```
Expected: re-encrypted file. Verify: `sops -d odysseus-data-seed.enc.yaml | grep -i bastion`.

> NOTE: the seed is **non-clobbering** (only seeds absent files). On the live PVC `settings.json` already exists, so this seed alone will NOT update it — see Task 16 (runtime API) for the live update. The seed guarantees the instruction on a fresh PVC.

- [ ] **Step 3: Commit**

```bash
git add 2-k3s/13.odysseus/odysseus-data-seed.enc.yaml
git commit -m "feat(odysseus): seed durable bastion-usage instruction"
```

---

## Phase 7 — Deploy + end-to-end verification

### Task 15: Open the PR, merge, sync ArgoCD

**Files:** none new.

- [ ] **Step 1: Rebase + push the branch**

```bash
git fetch origin -q && git rebase origin/main && git push -u origin odysseus-bastion --force-with-lease
```
Expected: clean rebase, pushed.

- [ ] **Step 2: Open the PR with a test plan**

```bash
gh pr create --base main --head odysseus-bastion --title "feat(odysseus): bastion execution sandbox" --body "<summary + test plan checklist mirroring Phase 7 verification>"
```
Expected: PR URL. Wait for `validate` to pass; rebase if BEHIND (per merge policy).

- [ ] **Step 3: Merge**

```bash
gh pr merge <n> --merge --delete-branch
```

- [ ] **Step 4: Sync the odysseus Application**

The odysseus Application is manual-sync. Sync it and watch the rollout:
```bash
ssh k3s-master-51 "kubectl -n argocd annotate app odysseus argocd.argoproj.io/refresh=hard --overwrite; argocd app sync odysseus 2>/dev/null || kubectl -n argocd patch app odysseus --type merge -p '{\"operation\":{\"sync\":{}}}'"
ssh k3s-master-51 "kubectl -n odysseus rollout status deploy/odysseus --timeout=180s"
```
Expected: new pod Ready (with workspace + ssh key mounted).

### Task 16: End-to-end verification (the HTML use case)

**Files:** none.

- [ ] **Step 1: Confirm the pod can SSH to the bastion**

```bash
POD=$(ssh k3s-master-51 "kubectl get pods -n odysseus -l app=odysseus -o name | head -1")
ssh k3s-master-51 "kubectl exec -n odysseus ${POD#pod/} -c odysseus -- ssh -i /app/.ssh/id_bastion -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new ubuntu@192.168.10.43 'hostname && whoami'"
```
Expected: `odysseus-bastion` / `ubuntu`.

- [ ] **Step 2: Confirm the shared workspace is the same on both sides**

```bash
ssh k3s-master-51 "kubectl exec -n odysseus ${POD#pod/} -c odysseus -- sh -c 'echo pod-write > /workspace/work/_probe.txt'"
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "cat /workspace/work/_probe.txt && rm /workspace/work/_probe.txt"
```
Expected: `pod-write` (pod write visible on the bastion).

- [ ] **Step 3: Full use-case dry run — write HTML, serve it, fetch it**

```bash
# pod writes HTML to the shared workspace
ssh k3s-master-51 "kubectl exec -n odysseus ${POD#pod/} -c odysseus -- sh -c 'printf \"<h1>bastion ok</h1>\" > /workspace/work/index.html'"
# pod starts a server ON THE BASTION (background), then we fetch via the DNS name
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "cd /workspace/work && nohup python3 -m http.server 8088 >/tmp/srv.log 2>&1 & sleep 2"
curl -s http://bastion.epaflix.com:8088/ ; echo
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ubuntu@192.168.10.43 "pkill -f 'http.server 8088'; rm -f /workspace/work/index.html"
```
Expected: `curl` prints `<h1>bastion ok</h1>`.

- [ ] **Step 4: Update Odysseus's live state via the scoped API (runtime nudge)**

Add a memory + a workspace doc so the live PVC (not just a fresh seed) carries the instruction:
```bash
python3 ~/.claude/skills/odysseus/scripts/odysseus_api.py POST /api/codex/memory '{"text":"For any build/run/serve task, do NOT execute locally — use the bastion: ssh bastion (ubuntu@192.168.10.43, key /app/.ssh/id_bastion). Shared workspace /workspace; web output in /workspace/work served at http://bastion.epaflix.com:<port>; full project incl secrets.yml at /workspace/repo.","category":"fact"}'
```
Expected: JSON success. (Document the same via `POST /api/codex/documents` if a how-to doc is wanted.)

- [ ] **Step 5: Record verification results in the PR description** (tick the boxes; never a new comment — per CLAUDE.md). Log to `.history/`.

---

## Phase 8 — Follow-ups + docs

### Task 17: Repo docs + follow-up issues

**Files:**
- Create: `1-proxmox/user-vms/odysseus-bastion-ssh-config`
- Modify: `1-proxmox/user-vms/README.md`

- [ ] **Step 1: Add the workstation SSH config snippet + inventory row**

```
# 1-proxmox/user-vms/odysseus-bastion-ssh-config
Host bastion odysseus-bastion
    HostName 192.168.10.43
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```
Add to the README inventory table: `| odysseus-bastion | 1043 | 192.168.10.43 | bastion.epaflix.com | Odysseus exec sandbox |`.

- [ ] **Step 2: Commit the docs**

```bash
git add 1-proxmox/user-vms/odysseus-bastion-ssh-config 1-proxmox/user-vms/README.md
git commit -m "docs(user-vms): add odysseus-bastion (1043/.43)"
```

- [ ] **Step 3: Open follow-up issues**

```bash
gh issue create --repo SpyrosPsarras/epaflix --title "odysseus-bastion: optional isolated-subnet hardening (VLAN + router)" --body "<Finding/Current/Desired/Notes, cross-link the bastion PR>"
gh issue create --repo SpyrosPsarras/epaflix --title "odysseus-bastion: reinforce/automate the 'use the bastion' instruction if it drifts at runtime" --body "<...>"
```
Expected: two issue URLs.

---

## Self-Review

- **Spec coverage:** VM (Task 3-5), NFS share (Task 1-2), one-share project copy incl. secrets (Task 6), homelab-only keys (Task 7), pod→bastion SSH isolation (Task 10,12,13), workstation access (Task 5), Pi-hole record (Task 8), no always-on server (verified by serving on-demand in Task 16), durable instruction seed + runtime nudge (Task 14,16), hardening (Task 5), follow-ups (Task 17). All spec sections mapped.
- **Open verification points flagged inline (not placeholders):** container HOME (Task 9), exact `midclt` maproot arg form (Task 2), template-raw presence (Task 3), settings.json instruction key (Task 14). Each has a discovery step + fallback.
- **Type/name consistency:** key name `ODYSSEUS_BASTION_SSH_KEY`, PVC `odysseus-bastion-workspace`, ConfigMap `odysseus-ssh-config`, mount `/app/.ssh/id_bastion`, alias `bastion` — used consistently across Tasks 10–16.

# User VMs

This directory contains SSH config files for individual user VMs and documents the procedure for provisioning a new user VM with jumpbox access.

## Architecture

```
[User's laptop]
      │
      │  SSH (port 10022, public internet)
      ▼
[jump-box LXC 1040 — 192.168.10.40]   ← takaros, Alpine Linux
      │
      │  SSH (port 22, internal network)
      ▼
[User VM — 192.168.10.4X]              ← evanthoulaki, Ubuntu 24.04
```

External DNS (Cloudflare) → `<user>.epaflix.com` → `81.167.233.67` (router) → port 10022 → jumpbox port 22
Internal DNS (Pi-hole) → `<user>.internal.epaflix.com` → `192.168.10.4X`

---

## Inventory

| User  | VMID | IP             | Hostname                     |
|-------|------|----------------|------------------------------|
| nick  | 1041 | 192.168.10.41  | nick.internal.epaflix.com    |
| vidar | 1042 | 192.168.10.42  | vidar.internal.epaflix.com   |

---

## SSH Config Files

Each user's config file lives in this directory as `<user>-ssh-config`.
Give the user:
1. The SSH config file
2. Their private key (`<user>_ed25519`)

The user places the key at `~/.ssh/<user>_ed25519` (chmod 600) and merges the config into `~/.ssh/config`.

---

## Provisioning a New User VM

Follow these steps in order. VMID and IP last two digits should match (e.g. VMID `1042` → IP `192.168.10.42`).

### 0. Prerequisites

- SSH access to `takaros` (192.168.10.10) and `evanthoulaki` (192.168.10.11)
- SSH access to the Pi-hole at 192.168.10.30
- A Cloudflare DNS A record `<user>.epaflix.com` → `81.167.233.67` (manual step — add in Cloudflare dashboard)

### 1. Generate SSH key pair

```bash
USER=vidar
ssh-keygen -t ed25519 -C "${USER}@epaflix.com" -f /tmp/${USER}_ed25519 -N ""
cat /tmp/${USER}_ed25519.pub   # verify
```

### 2. Copy the template to evanthoulaki (first time only)

The Ubuntu 24.04 cloud image template lives on `takaros` (local-raid, VMID 9001).
Cross-node clone to non-shared storage is not supported, so the disk must be manually transferred.

```bash
# Activate the LVM thin volume on takaros
ssh root@192.168.10.10 "lvchange -ay -K pve-raid/base-9001-disk-0"

# Fix SSH known_hosts on takaros for evanthoulaki (if needed after reinstall)
ssh root@192.168.10.10 "ssh-keygen -f /etc/ssh/ssh_known_hosts -R 192.168.10.11 && \
  ssh -o StrictHostKeyChecking=no root@192.168.10.11 hostname"

# Pipe disk from takaros to evanthoulaki (runs in background — takes ~5-10 min)
ssh root@192.168.10.10 \
  "dd if=/dev/mapper/pve--raid-base--9001--disk--0 bs=4M | gzip | \
   ssh -o StrictHostKeyChecking=no root@192.168.10.11 \
   'gunzip > /tmp/ubuntu-24.04-template.raw' && echo DONE"
```

> ⚠️ This command runs host-to-host over their 10GbE link. Check `/tmp/ubuntu-24.04-template.raw`
> exists and is ~3.5 GiB on evanthoulaki before continuing.

### 3. Create the VM on evanthoulaki

```bash
VMID=1042
USER=vidar
IP=192.168.10.42

ssh root@192.168.10.11 "qm create ${VMID} \
  --name ${USER} \
  --memory 8192 \
  --cores 16 \
  --sockets 1 \
  --cpu host \
  --ostype l26 \
  --scsihw virtio-scsi-pci \
  --net0 virtio,bridge=vmbr0 \
  --agent enabled=1 \
  --onboot 0 \
  --numa 0"
```

### 4. Import and attach the disk

```bash
ssh root@192.168.10.11 "qm importdisk ${VMID} /tmp/ubuntu-24.04-template.raw local-raid --format raw"

ssh root@192.168.10.11 "qm set ${VMID} \
  --scsihw virtio-scsi-pci \
  --scsi0 local-raid:vm-${VMID}-disk-0 \
  --boot order=scsi0 \
  --bootdisk scsi0 \
  --ide2 local-raid:cloudinit"
```

### 5. Configure cloud-init

```bash
# Copy public key to evanthoulaki
scp /tmp/${USER}_ed25519.pub root@192.168.10.11:/tmp/${USER}_ed25519.pub

ssh root@192.168.10.11 "qm set ${VMID} \
  --ciuser ${USER} \
  --sshkeys /tmp/${USER}_ed25519.pub \
  --ipconfig0 ip=${IP}/24,gw=192.168.10.1 \
  --nameserver 192.168.10.30 \
  --searchdomain epaflix.com"
```

### 6. Resize disk and start

```bash
ssh root@192.168.10.11 "qm resize ${VMID} scsi0 100G"
ssh root@192.168.10.11 "qm start ${VMID}"
```

Verify the VM is reachable (cloud-init takes ~1-2 min on first boot):

```bash
# Wait for the IP to respond
ping -c 3 ${IP}

# Test SSH directly (bypassing jumpbox)
ssh -i /tmp/${USER}_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no ${USER}@${IP} \
  "echo ok && hostname && uptime"
```

### 7. Add user to the jump-box

The jump-box is LXC 1040 on `takaros`. It runs Alpine Linux.

> ⚠️ **Alpine `adduser -D` locks the account.** OpenSSH refuses pubkey auth on locked accounts.
> The shadow field must be `*` (no password) not `!` (locked). See the `sed` fix below.

```bash
PUBKEY=$(cat /tmp/${USER}_ed25519.pub)

ssh root@192.168.10.10 "pct exec 1040 -- sh -c '
  adduser -D -G ssh-users -s /bin/sh ${USER} &&
  mkdir -p /home/${USER}/.ssh &&
  chmod 700 /home/${USER}/.ssh &&
  echo \"${PUBKEY}\" > /home/${USER}/.ssh/authorized_keys &&
  chmod 600 /home/${USER}/.ssh/authorized_keys &&
  chown -R \$(id -u ${USER}):\$(id -u ${USER}) /home/${USER}/.ssh &&
  sed -i \"s/^${USER}:!:/${USER}:*:/\" /etc/shadow
'"

# Verify the shadow entry shows * not !
ssh root@192.168.10.10 "pct exec 1040 -- sh -c 'grep ${USER} /etc/shadow'"
# Expected: vidar:*:...
```

> **Note on chown**: Alpine's `adduser -D -G ssh-users` assigns the next available UID/GID.
> Since GID 1001 is already taken by `ssh-users`, no separate primary group named `${USER}` is
> created. Use numeric UID/GID (`id -u ${USER}`) instead of `${USER}:${USER}` in chown.

### 8. Add internal DNS record

```bash
ssh root@192.168.10.30 "
  echo 'address=/${USER}.internal.epaflix.com/${IP}' >> /etc/dnsmasq.d/10-internal-epaflix.conf
  systemctl restart pihole-FTL
"

# Verify (pihole reloaddns alone is NOT enough for new entries — use full FTL restart)
dig ${USER}.internal.epaflix.com @192.168.10.30 +short
# Expected: <IP>
```

> ⚠️ **Use `systemctl restart pihole-FTL`**, not just `pihole reloaddns`, when adding new
> `*.internal.epaflix.com` entries. A reload (SIGHUP) does not always pick up new dnsmasq
> directives for this zone.

### 9. Add external DNS record (Cloudflare — manual)

In the Cloudflare dashboard, add:

| Type | Name           | Content       | Proxy |
|------|----------------|---------------|-------|
| A    | `<user>`       | `81.167.233.67` | DNS only (grey cloud) |

This makes `<user>.epaflix.com` resolve publicly so the user can reach the jumpbox.

### 10. Test the full path

```bash
ssh -i /tmp/${USER}_ed25519 \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no \
  -o ProxyCommand="ssh -q -W %h:%p -i /tmp/${USER}_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -p 22 ${USER}@192.168.10.40" \
  ${USER}@${USER}.internal.epaflix.com \
  "echo ok && hostname && uptime"
```

Expected output:
```
ok
vidar
 HH:MM:SS up X min, ...
```

### 11. Generate the config file for the user

Create `1-proxmox/user-vms/<user>-ssh-config`:

```
Host epaflix-jumpbox-<user>
    HostName <user>.epaflix.com
    Port 10022
    User <user>
    IdentityFile ~/.ssh/<user>_ed25519
    IdentitiesOnly yes

Host <user>-vm
    HostName <user>.internal.epaflix.com
    User <user>
    ProxyJump epaflix-jumpbox-<user>
    IdentityFile ~/.ssh/<user>_ed25519
    IdentitiesOnly yes
```

> **`ProxyJump` not `ProxyCommand`**: Using `ProxyCommand ssh -q -W %h:%p epaflix-jumpbox-<user>`
> fails when the config is passed via `-F`, because `ProxyCommand` spawns a child `ssh` process
> that does **not** inherit the `-F` flag and therefore cannot resolve the jumpbox host alias.
> `ProxyJump` is handled natively by the same SSH process and correctly uses the active config file.

### 12. Clean up temp files

```bash
ssh root@192.168.10.11 "rm -f /tmp/ubuntu-24.04-template.raw /tmp/${USER}_ed25519.pub"
ssh root@192.168.10.10 "rm -f /tmp/${USER}_ed25519.pub"
# Keep /tmp/<user>_ed25519 and /tmp/<user>_ed25519.pub locally until delivered to user
```

---

## Delivering Credentials to the User

Send the user:
1. `<user>-ssh-config` — SSH config snippet to merge into `~/.ssh/config`
2. `<user>_ed25519` — private key (send securely, e.g. encrypted email or Signal)

**Instructions for the user:**

```bash
# Save the private key
cp vidar_ed25519 ~/.ssh/vidar_ed25519
chmod 600 ~/.ssh/vidar_ed25519

# Merge the SSH config (or append manually to ~/.ssh/config)
cat vidar-ssh-config >> ~/.ssh/config

# Connect
ssh vidar-vm "echo ok && hostname"
```

---

## Troubleshooting

### `User X not allowed because account is locked`

The Alpine `adduser -D` command creates the user with `!` in `/etc/shadow` (locked). Fix:

```bash
ssh root@192.168.10.10 "pct exec 1040 -- sh -c 'sed -i \"s/^<user>:!:/<user>:*:/\" /etc/shadow'"
```

### `Too many authentication failures` / IP banned by fail2ban

The jumpbox has `MaxAuthTries 3`. If you try the wrong key multiple times, fail2ban bans your IP.
Unban:

```bash
ssh root@192.168.10.10 "pct exec 1040 -- sh -c 'fail2ban-client set sshd unbanip <YOUR_IP>'"
```

Always use `-o IdentitiesOnly=yes` with `-i <keyfile>` to prevent the SSH agent from trying
extra keys before the right one.

### `Connection closed by UNKNOWN port 65535`

This happens when the `ProxyCommand` or `ProxyJump` could not forward to the internal VM. Check:
1. The jumpbox auth works: `ssh -i <key> -o IdentitiesOnly=yes -p 10022 <user>@<user>.epaflix.com hostname`
2. The jumpbox can reach the VM: `pct exec 1040 -- nc -zv 192.168.10.4X 22`
3. Internal DNS resolves on the jumpbox: `pct exec 1040 -- nslookup <user>.internal.epaflix.com`
4. `AllowTcpForwarding yes` is set in `/etc/ssh/sshd_config.d/epaflix.conf` on the jumpbox
5. The user is in the `ssh-users` group: `pct exec 1040 -- getent group ssh-users`
6. The config uses `ProxyJump`, not `ProxyCommand` with a host alias — see note in step 11 above

### Internal DNS not resolving after adding record

`pihole reloaddns` is not always sufficient for new `*.internal.epaflix.com` entries.
Use a full restart:

```bash
ssh root@192.168.10.30 "systemctl restart pihole-FTL"
```

### `can't clone to non-shared storage 'local-raid'`

Proxmox does not support cross-node cloning to non-shared (local) storage via `qm clone`.
The manual disk transfer procedure in step 2 is the correct workaround.
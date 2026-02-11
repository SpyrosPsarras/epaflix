# Automated Command Logging

This guide explains how to automatically capture commands and their outputs when working with LLM-driven operations.

## Quick Start

### Method 1: Use the Command Logger Wrapper (Recommended for LLM)

When I (the LLM) need to run a command and log it:

```bash
./.history/log-cmd.sh "Check VM status" "qm status 8001"
```

This will:
- Execute the command
- Capture full output
- Log everything to `.history/YYYY-MM-DD-commands.log`
- Show where it was logged

**Custom log file**:
```bash
./.history/log-cmd.sh "Description" "command" ".history/2026-02-14-custom.log"
```

### Method 2: Logged Terminal Session

Start a session where everything is captured:

```bash
./.history/start-logged-session.sh proxmox-setup
```

All commands and outputs in that shell will be logged. Exit with `exit` or `Ctrl+D`.

### Method 3: Shell Functions (Interactive Use)

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
source ~/Documents/Epaflix/k3s-swarm-proxmox/.history/shell-functions.sh
```

Then use functions:

```bash
# Set today's log file
llm-log-to "2026-02-14-my-work"

# Log a command
llm-log "Check node status" "kubectl get nodes"

# Log the last command you just ran
kubectl get pods
llm-log-last "List all pods"

# Show current log file
llm-log-file
```

## Comparison

| Method | Best For | Pros | Cons |
|--------|----------|------|------|
| **log-cmd.sh** | LLM execution | Simple, single command focus | Need to wrap each command |
| **start-logged-session** | Interactive work | Captures everything automatically | Includes all typing/mistakes |
| **shell-functions** | Daily use | Flexible, integrated | Requires shell config |

## How LLM Should Use This

When I execute commands via `run_in_terminal`, I can now wrap them:

**Instead of**:
```bash
kubectl get nodes
```

**I'll use**:
```bash
./.history/log-cmd.sh "Verify K3s cluster nodes" "kubectl get nodes"
```

This ensures everything is logged for future reference.

## Advanced Usage

### Chaining Commands

Log complex operations:
```bash
./.history/log-cmd.sh "Deploy application" "kubectl apply -f app.yaml && kubectl rollout status deployment/myapp"
```

### Remote Commands

Log SSH commands:
```bash
./.history/log-cmd.sh "Check Proxmox storage" "ssh root@192.168.10.10 'pvesm status'"
```

### Scripted Sequences

Create scripts that use the logger:
```bash
#!/bin/bash
LOG_FILE=".history/$(date +%Y-%m-%d)-deployment.log"

./.history/log-cmd.sh "Pull latest images" "docker pull myimage:latest" "${LOG_FILE}"
./.history/log-cmd.sh "Deploy to k3s" "kubectl apply -f deploy.yaml" "${LOG_FILE}"
./.history/log-cmd.sh "Wait for rollout" "kubectl rollout status deployment/myapp" "${LOG_FILE}"
```

## Environment Variables

```bash
# Set default log file
export LLM_HISTORY_LOG=".history/2026-02-14-my-session.log"

# Commands will use this file when using shell functions
```

## Tips

1. **Descriptive names**: Use clear descriptions so logs are searchable
2. **One log per session**: Keep related work in one file
3. **Review logs**: Check `.history/` directory periodically
4. **Security**: Remember this directory is git-ignored - can include real IPs/hostnames
5. **Timestamps**: All methods include automatic timestamps

## Integration with Workflow

For a typical work session:

```bash
# Option A: Start logged session
./.history/start-logged-session.sh "k3s-deployment"
# ... work interactively, everything logged ...
exit

# Option B: Use wrapper for each command
./.history/log-cmd.sh "Step 1" "command1"
./.history/log-cmd.sh "Step 2" "command2"

# Option C: Use shell functions
llm-log-to "2026-02-14-my-work"
llm-log "Check status" "kubectl get all"
```

## Viewing Logs

```bash
# Latest logs
ls -lt .history/*.log | head

# View specific log
less .history/2026-02-14-commands.log

# Search across logs
grep -r "error" .history/

# Count commands in a log
grep -c "^## \[" .history/2026-02-14-commands.log
```

## Troubleshooting

**Commands not logging?**
- Check script is executable: `ls -l .history/*.sh`
- Check permissions: `chmod +x .history/*.sh`

**Log file in wrong location?**
- Use absolute paths: `./.history/log-cmd.sh` not `log-cmd.sh`
- Or add to PATH: `export PATH="$PATH:$PWD/.history"`

**Output truncated?**
- Check disk space: `df -h`
- Large outputs are captured - check the full log file

## Future Enhancements

Potential additions:
- [ ] Add filtering options (hide certain outputs)
- [ ] Colored output in logs
- [ ] Automatic log rotation
- [ ] Integration with VS Code tasks
- [ ] Web viewer for logs

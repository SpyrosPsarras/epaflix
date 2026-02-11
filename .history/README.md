# Command History Documentation

This directory contains historical records of commands executed during the setup, configuration, and maintenance of the infrastructure. These logs serve as:

1. **Troubleshooting reference** - Review what was done when issues occur
2. **LLM context** - Provide AI assistants with accurate history of operations
3. **Documentation** - Record of actual commands used vs theoretical documentation
4. **Learning resource** - See real-world command sequences and outputs

## 🚀 Quick Start: Automated Logging

**For LLM-driven command execution:**
```bash
./.history/log-cmd.sh "Description" "your-command-here"
```

**For interactive sessions:**
```bash
./.history/start-logged-session.sh session-name
```

👉 **See [USAGE.md](USAGE.md) for complete automation guide and examples**

## ⚠️ Security Notice

**This directory is git-ignored** - it may contain sensitive information like:
- Actual IP addresses and hostnames
- System configurations
- Error messages with system details
- VM IDs and infrastructure layout

While passwords and tokens should still use placeholders (reference `.github/instructions/secrets.yml`), other sensitive but non-credential information can be safely documented here.

## File Organization

### Recommended Structure

```
.history/
├── README.md                            # This file
├── YYYY-MM-DD-session-name.log          # Daily session logs
├── YYYY-MM-DD-troubleshooting-xyz.log   # Specific troubleshooting sessions
└── commands/                             # Optional: organized by component
    ├── proxmox.log                       # Ongoing Proxmox commands
    ├── k3s.log                           # Ongoing K3s commands
    ├── truenas.log                       # Ongoing TrueNAS commands
    └── networking.log                    # Network configuration commands
```

### Naming Conventions

- **Date prefix**: Always start with `YYYY-MM-DD` for chronological sorting
- **Descriptive name**: Brief description of the work session
- **Use hyphens**: Separate words with hyphens, not spaces

**Examples**:
- `2026-02-14-initial-proxmox-setup.log`
- `2026-02-15-k3s-cluster-deployment.log`
- `2026-02-16-troubleshoot-storage-issues.log`

## Log Entry Format

Each log entry should follow this template for consistency:

```markdown
## [YYYY-MM-DD HH:MM] - Brief Description

**Context**: Explain what you're trying to accomplish and why

**Command**:
```bash
actual command here
```

**Output**:
```
full command output
including errors if applicable
```

**Result**: Success | Failed | Partial

**Notes**:
- Any observations
- Issues encountered
- Solutions applied
- Things to remember for next time
- References to documentation

---
```

## Best Practices

### What to Document

✅ **DO document**:
- All infrastructure provisioning commands (Proxmox, TrueNAS)
- Kubernetes cluster operations (kubectl, helm, k3sup)
- Configuration changes to systems
- Troubleshooting command sequences
- Failed attempts (very valuable!)
- Workarounds and their reasons
- Performance tuning commands

❌ **DON'T document**:
- Routine checks with no interesting output
- Repeated identical commands (unless testing something)
- Simple file edits (git handles this)

### Documentation Tips

1. **Include context first** - Future you won't remember why you ran that command
2. **Full outputs for errors** - Error messages are crucial for troubleshooting
3. **Document the "why"** - Not just what you did, but why you did it
4. **Cross-reference** - Link to relevant documentation sections
5. **Note deviations** - If you deviate from documented procedures, explain why
6. **Time stamps** - Help correlate with system logs and events
7. **Redact carefully** - IPs and hostnames are OK, passwords/tokens are NOT

### Using History with LLMs

When working with AI assistants:

```
"Check .history/2026-02-14-k3s-setup.log for context on the cluster deployment"

"Review the troubleshooting session in .history/2026-02-15-storage-debug.log"

"I've documented all steps in .history/2026-02-16-proxmox-network.log"
```

LLMs can read these files to understand:
- What has already been done
- What worked and what didn't
- Current state of the infrastructure
- Previous solutions to similar problems

## Example Session

See `example-session.log` in this directory for a template of how to structure your command history logs.

## Maintenance

- **Review periodically** - Old logs can be archived or deleted if no longer relevant
- **Consolidate when needed** - Multiple small logs can be combined into summary docs
- **Update README** - If you develop better practices, update this file
- **Check .gitignore** - Ensure .history/ remains git-ignored

## Integration with Main Documentation

These history logs complement (not replace) the main documentation:

- **Main docs**: The "ideal" way to do things, clean and generalized
- **History logs**: The "actual" way things were done, with all the messy reality

Both are valuable! Main docs guide new deployments, history logs help troubleshoot and learn from experience.

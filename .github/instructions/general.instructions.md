---
applyTo: "**"
description: "General Instructions for all setups"
---

# CRITICAL General instructions

CRITICAL: Never save username, hostnames and passwords on any documentation, script, yaml or anywhere other than secrets.yml.
CRITICAL: If we need to reference it, use the `.github/instructions/secrets.yml` file.

## Command History Documentation

IMPORTANT: Document all significant commands and their outputs in the `.history/` directory for future LLM reference and troubleshooting.

### History Directory Structure

```
.history/
├── README.md                    # This explains the history logging system
├── YYYY-MM-DD-session-name.log  # Daily session logs
└── commands/                     # Optional: organized by component
    ├── proxmox.log
    ├── k3s.log
    └── truenas.log
```

### What to Document

1. **All infrastructure commands**: Proxmox VM operations, network configurations, storage setups
2. **K3s cluster commands**: Installations, deployments, kubectl operations
3. **Configuration changes**: Any modifications to system or cluster settings
4. **Troubleshooting sessions**: Commands used to diagnose and fix issues
5. **Command outputs**: Full terminal output, especially for error diagnosis

### History Log Format

Use this format for each log entry:

```markdown
## [YYYY-MM-DD HH:MM] - Brief Description

**Context**: What you're trying to accomplish

**Command**:
```bash
command here
```

**Output**:
```
output here
```

**Result**: Success/Failed/Partial - Brief explanation

**Notes**: Any observations, issues, or things to remember
---

### Best Practices

- Create a new log file for each major session or daily work
- Use descriptive session names: `2026-02-14-k3s-initial-setup.log`
- Redact sensitive information (IPs can stay, but tokens/passwords must use placeholders)
- Include context before commands so future readers understand the "why"
- Document failures and errors - they're valuable learning material
- Cross-reference related documentation sections when applicable

### Git Ignore

The `.history/` directory is git-ignored to prevent committing sensitive information. This means:
- You can safely include actual IPs, hostnames, and system details
- Still avoid including passwords or API tokens when possible
- The history is local to your machine and won't be shared via git

### LLM Context

When asking for help or working with LLMs:
- Reference specific history log files for context
- LLMs can read these files to understand what has been done
- Include the log file path in your questions: "Check `.history/2026-02-14-setup.log` for context"

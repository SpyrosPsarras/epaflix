# Docker Swarm Stacks

This directory contains Docker Compose stack definitions for services deployed on the Docker Swarm cluster.

## How to Deploy a Stack

```bash
# Deploy or update a stack from this laptop
docker -H ssh://ubuntu@192.168.10.71 stack deploy -c <stack-dir>/docker-compose.yml <stack-name>

# Or SSH into the manager and deploy
ssh ubuntu@192.168.10.71
docker stack deploy -c /path/to/docker-compose.yml <stack-name>
```

## Available Stacks

| Stack Name | Directory | Description |
|------------|-----------|-------------|
| `traefik` | [`traefik/`](./traefik/) | Traefik v3 reverse proxy — wildcard TLS (`*.epaflix.com`), HTTP→HTTPS redirect, dashboard at `traefik.epaflix.com` |

## Stack File Structure

Each stack lives in its own subdirectory:

```
stacks/
└── <stack-name>/
    ├── docker-compose.yml   # Main stack definition (Compose v3.8+)
    ├── .env.example         # Example environment variables (no real values)
    └── README.md            # Service-specific notes and instructions
```

## Conventions

- **Stack name** matches the directory name (kebab-case)
- **Compose version**: always use `"3.8"` or higher
- **Secrets**: use Docker Swarm secrets (`docker secret create`) — never hardcode in compose files
- **Configs**: use Docker Swarm configs for non-sensitive config files
- **Images**: prefer pinned tags (e.g. `nginx:1.27-alpine`) over `latest`
- **Replicas**: always define `deploy.replicas` explicitly
- **Restart policy**: always define `deploy.restart_policy`
- **Update config**: always define `deploy.update_config` for rolling updates

## Example Stack Template

```yaml
version: "3.8"

networks:
  default:
    driver: overlay
    attachable: true

services:
  app:
    image: <image>:<tag>
    networks:
      - default
    ports:
      - "8080:8080"
    environment:
      - ENV_VAR=value
    deploy:
      replicas: 2
      update_config:
        parallelism: 1
        delay: 10s
        failure_action: rollback
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 3
```

## Useful Commands

```bash
# List all deployed stacks
ssh ubuntu@192.168.10.71 "docker stack ls"

# List services in a stack
ssh ubuntu@192.168.10.71 "docker stack services <stack-name>"

# Watch task status
ssh ubuntu@192.168.10.71 "docker stack ps <stack-name>"

# View service logs
ssh ubuntu@192.168.10.71 "docker service logs --tail 100 -f <stack-name>_<service>"

# Remove a stack
ssh ubuntu@192.168.10.71 "docker stack rm <stack-name>"
```
```

Now let me continue creating the remaining files:
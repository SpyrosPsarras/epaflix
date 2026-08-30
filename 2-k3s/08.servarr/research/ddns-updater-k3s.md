# ddns-updater on k3s: facts from primary sources

Question: what does it take to run `qmcgaw/ddns-updater` (repo `qdm12/ddns-updater`) as a kustomize Deployment + PVC + Service on k3s, updating proxied Cloudflare records, replacing the TrueNAS SCALE community app (app_version v2.9.0, uid/gid 568)?

Answer up front: latest stable is v2.10.0 (image tags `latest`, `v2.10`, `v2.10.0`, published 2026-05-01). The config file is `/updater/data/config.json` (env `CONFIG_FILEPATH`), not `settings.json`, and its only top-level key is `settings`. The program reads config once at startup, so config changes need a pod restart. It runs as uid/gid 1000 by default; the documented way to run as another uid is to build the image with `--build-arg UID=<uid>`. There is an official kustomize example in the repo's `k8s/` directory, but it has no PVC and no probes. Public IP detection is HTTPS echo services plus DNS over TLS (TCP 853) to Cloudflare and OpenDNS, not DoH. Two instances updating the same records do DNS-only checks per cycle for non-proxied records; for proxied Cloudflare records each instance does at most one GET per cycle and skips the PUT when the stored record content already equals the detected public IP.

Read 2026-08-30. Master branch equals the v2.10.0 code base. Docs are versioned per release (README, "Versioned documentation" table); the versioned docs list in the README only goes back to v2.8, so all citations below are to master (= v2.10.0) unless noted.

## Sources read

- [README](https://github.com/qdm12/ddns-updater/blob/master/README.md)
- [docs/cloudflare.md](https://github.com/qdm12/ddns-updater/blob/master/docs/cloudflare.md)
- [k8s/README.md](https://github.com/qdm12/ddns-updater/blob/master/k8s/README.md), [k8s/base/deployment.yaml](https://github.com/qdm12/ddns-updater/blob/master/k8s/base/deployment.yaml), [k8s/base/service.yaml](https://github.com/qdm12/ddns-updater/blob/master/k8s/base/service.yaml), [k8s/base/secret-config.yaml](https://github.com/qdm12/ddns-updater/blob/master/k8s/base/secret-config.yaml)
- [Dockerfile](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile)
- [cmd/ddns-updater/main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go)
- [internal/update/service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go), [internal/update/update.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/update.go)
- [internal/provider/providers/cloudflare/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/providers/cloudflare/provider.go), [internal/provider/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/provider.go), [internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go)
- [internal/persistence/json/database.go](https://github.com/qdm12/ddns-updater/blob/master/internal/persistence/json/database.go)
- [internal/health/check.go](https://github.com/qdm12/ddns-updater/blob/master/internal/health/check.go), [internal/health/handler.go](https://github.com/qdm12/ddns-updater/blob/master/internal/health/handler.go), [internal/server/handler.go](https://github.com/qdm12/ddns-updater/blob/master/internal/server/handler.go)
- [pkg/publicip/dns/fetch.go](https://github.com/qdm12/ddns-updater/blob/master/pkg/publicip/dns/fetch.go), [pkg/publicip/dns/providers.go](https://github.com/qdm12/ddns-updater/blob/master/pkg/publicip/dns/providers.go)
- [GitHub releases/latest API](https://api.github.com/repos/qdm12/ddns-updater/releases/latest), [Docker Hub tags API](https://hub.docker.com/v2/repositories/qmcgaw/ddns-updater/tags), [Docker Hub page](https://hub.docker.com/r/qmcgaw/ddns-updater)

## 1. Image and tags

- Images: `ghcr.io/qdm12/ddns-updater` (GitHub Packages) and `qmcgaw/ddns-updater` (Docker Hub) ([README, Features](https://github.com/qdm12/ddns-updater/blob/master/README.md)).
- Latest stable release: `v2.10.0`, published 2026-05-01, `prerelease: false`, draft false ([releases/latest API](https://api.github.com/repos/qdm12/ddns-updater/releases/latest)).
- Docker Hub tags `latest`, `v2`, `2`, `v2.10`, `2.10`, `v2.10.0`, `2.10.0` all share manifest digest `sha256:3e2aa558946b...`, pushed 2026-05-01; amd64 compressed size 5,534,351 bytes ([Docker Hub tags API](https://hub.docker.com/v2/repositories/qmcgaw/ddns-updater/tags)). The image is scratch-based; README quotes it as ~12 MB.
- Multi-arch: `amd64`, `386`, `arm64`, `armv7`, `armv6`, `s390x`, `ppc64le`, `riscv64` ([README](https://github.com/qdm12/ddns-updater/blob/master/README.md), [Docker Hub](https://hub.docker.com/r/qmcgaw/ddns-updater)).
- TrueNAS `app_version v2.9.0` corresponds to Docker tag `v2.9.0` (alias `2.9.0`), pushed 2024-12-24, digest `sha256:ed73f1fb7ab5...` ([Docker Hub tags API](https://hub.docker.com/v2/repositories/qmcgaw/ddns-updater/tags)). Note the rolling `v2.9`/`2.9`/`v2.9.1` tags were re-pushed 2026-05-01 with a different digest (`sha256:3758e33ca466...`), so the pinned `v2.9.0` tag is not what the rolling v2.9 aliases point at now.

## 2. On-disk layout, user, persistence

Paths and env (defaults from the [README env table](https://github.com/qdm12/ddns-updater/blob/master/README.md) and the [Dockerfile ENV block](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile)):

| Env | Default | Meaning |
| --- | --- | --- |
| `DATADIR` | `/updater/data` | Data directory |
| `CONFIG_FILEPATH` | `/updater/data/config.json` | Config file path |
| `CONFIG` | empty | One-line JSON config, takes precedence over the file |
| `UMASK` | current umask | Umask for files the program writes |

- The settings file is named `config.json`, not `settings.json`. The only top-level key the reader accepts is `settings` ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go): `struct { CommonSettings []commonSettings \`json:"settings"\` }`). A TrueNAS export using a top-level `config` array is not read by any code path in the repo.
- If `CONFIG` is set, the program pretty-prints that JSON and writes it back to `CONFIG_FILEPATH` (perms 0666), so the file on disk mirrors the env config ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go), `getProvidersFromEnv`). If the file is missing, the program creates an empty `{}` config file ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go), `getProvidersFromFile`).
- The program also writes `$DATADIR/updates.json` (`filepath.Join(dataDir, "updates.json")`, [internal/persistence/json/database.go](https://github.com/qdm12/ddns-updater/blob/master/internal/persistence/json/database.go)). It stores per-record history events (IP + timestamp), appended on each successful update ([internal/update/update.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/update.go)); the README describes it as the persistence for "old IP addresses with change times for each record".
- What must be persisted: the content under `DATADIR`, i.e. `config.json` (unless config is supplied via `CONFIG` env, which the program then re-writes to disk anyway) and `updates.json`. The README states the program still works if `/updater/data` is not bind-mounted (no persistent `updates.json`), so persistence of `updates.json` is not a hard requirement for operation, it preserves history ([README, Container section](https://github.com/qdm12/ddns-updater/blob/master/README.md)).
- Everything else is ephemeral: the final image is `FROM scratch` containing only `/updater/ddns-updater` and an empty `/updater/data` owned `UID:GID` ([Dockerfile](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile)).
- User: the image runs as build args `UID`/`GID`, default `1000:1000` (`USER ${UID}:${GID}` in the [Dockerfile](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile)). The README instructs the data dir to be owned by uid 1000 with rwx, and says for another user ID "build the image yourself with `--build-arg UID=<your-uid>`"; running as root via `--user="0"` is mentioned but called out as "not advised security wise" ([README, Container section](https://github.com/qdm12/ddns-updater/blob/master/README.md)). Files created by the program: data dir `0777`, `updates.json` `0666`, config file `0666` ([persistence/json/database.go](https://github.com/qdm12/ddns-updater/blob/master/internal/persistence/json/database.go), [params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go)).
- To keep the TrueNAS uid/gid 568, the documented mechanism is building the image with `--build-arg UID=568 GID=568`. The official k8s manifests set no `securityContext` and no `fsGroup`, so they run with the image default uid 1000 ([k8s/base/deployment.yaml](https://github.com/qdm12/ddns-updater/blob/master/k8s/base/deployment.yaml)).

## 3. Web UI and health endpoints

- Web server on by default: `SERVER_ENABLED=yes`, `LISTENING_ADDRESS=:8000` (configurable), `ROOT_URL=/` for reverse-proxy path prefixes ([README env table](https://github.com/qdm12/ddns-updater/blob/master/README.md), [Dockerfile](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile), `EXPOSE 8000`).
- Routes ([internal/server/handler.go](https://github.com/qdm12/ddns-updater/blob/master/internal/server/handler.go)): `GET /` renders the UI, `GET /update` forces an update round through the update service, `GET /static/*` serves assets. There is no `/health` route on the web server.
- Health server: separate listener at `HEALTH_SERVER_ADDRESS`, default `127.0.0.1:9999`. Single route: `GET /` returns 200 if healthy, 500 with the error otherwise ([internal/health/handler.go](https://github.com/qdm12/ddns-updater/blob/master/internal/health/handler.go)). Healthy means: no record has status FAIL, and every non-proxied record's recorded IP still DNS-resolves to that IP; proxied records are excluded from the DNS-lookup check ([internal/health/check.go](https://github.com/qdm12/ddns-updater/blob/master/internal/health/check.go)). An empty address disables the server ([main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go), `createHealthServer`).
- The Docker healthcheck execs the same binary with the `healthcheck` subcommand (`HEALTHCHECK ... CMD ["/updater/ddns-updater", "healthcheck"]`, interval 60s, timeout 5s, start-period 10s, retries 2, [Dockerfile](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile)). That subcommand queries the health server address ([main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go)).
- The official k8s Deployment defines no liveness/readiness probes at all ([k8s/base/deployment.yaml](https://github.com/qdm12/ddns-updater/blob/master/k8s/base/deployment.yaml)). Since the health server binds `127.0.0.1`, a kubelet `httpGet` probe (which dials the pod IP) cannot reach it; the `healthcheck` subcommand is runnable via an `exec` probe inside the container.

## 4. Config schema for Cloudflare

Top level is a JSON object with a `settings` array ([docs/cloudflare.md](https://github.com/qdm12/ddns-updater/blob/master/docs/cloudflare.md), [internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go)). Common fields per entry ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go)):

- `provider`: provider name (`cloudflare`).
- `domain`: `example.com`, `sub.example.com`, or `*.example.com` for wildcard; a comma-separated list (`"example.com,sub.example.com"`) produces one record per entry. Owner is derived here: the root domain gets owner `@`, a subdomain gets its label prefix as owner ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go), `extractFromDomainField`).
- `owner`: optional explicit owner; legacy `host` maps onto it ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go)).
- `ip_version`: `ipv4`, `ipv6`, or `ipv4 or ipv6` (default) ([docs/cloudflare.md](https://github.com/qdm12/ddns-updater/blob/master/docs/cloudflare.md)).
- `ipv6_suffix`: optional IPv6 interface-identifier suffix for permanent IPv6 addresses ([docs/cloudflare.md](https://github.com/qdm12/ddns-updater/blob/master/docs/cloudflare.md)).
- `provider_ip`: deprecated, only produces a warning ([internal/params/json.go](https://github.com/qdm12/ddns-updater/blob/master/internal/params/json.go)).

Cloudflare-specific fields ([docs/cloudflare.md](https://github.com/qdm12/ddns-updater/blob/master/docs/cloudflare.md), enforcement in [cloudflare/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/providers/cloudflare/provider.go)):

Compulsory:

- `zone_identifier`: the Cloudflare Zone ID.
- `domain` (see above).
- `ttl`: integer seconds; "specify 1 for automatic". A value of 0 fails validation (`ErrTTLNotSet`), so the field cannot be omitted.
- Exactly one auth mode: `token` (API token with DNS edit permission for the zone), or `email` + `key` (Global API Key pair, key regex `^[a-zA-Z0-9]+$`), or `user_service_key` (regex `^v1\.0.+$`).

Optional:

- `proxied`: boolean, default false. Sets the `proxied` flag on created/updated records ([cloudflare/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/providers/cloudflare/provider.go)).
- `ip_version`, `ipv6_suffix` as above.

There is no `update_period` or per-provider period field. Timing is global only: `PERIOD` and `UPDATE_COOLDOWN_PERIOD` env vars ([README env table](https://github.com/qdm12/ddns-updater/blob/master/README.md)); neither the provider interface ([internal/provider/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/provider.go)) nor the common settings struct has a period field.

Full example for the current setup, one proxied A record with automatic TTL:

```json
{
  "settings": [
    {
      "provider": "cloudflare",
      "zone_identifier": "<zone id>",
      "domain": "example.com",
      "ttl": 1,
      "token": "<api token>",
      "proxied": true,
      "ip_version": "ipv4",
      "ipv6_suffix": ""
    }
  ]
}
```

## 5. Startup, reload, and update period

- Config is read once at startup ([main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go): `readConfig` then `jsonReader.JSONProviders`). There is no file watcher and no reload endpoint; the web server exposes only `/`, `/update`, `/static` ([server/handler.go](https://github.com/qdm12/ddns-updater/blob/master/internal/server/handler.go)). Config changes require a process restart.
- At startup the program starts the update service and immediately runs one forced update round (`go updaterService.ForceUpdate(ctx)` in [main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go)), then ticks every `PERIOD` ([service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go), `run()`).
- Defaults: `PERIOD=5m` (IP check cycle), `UPDATE_COOLDOWN_PERIOD=5m` (minimum spacing between actual updates per record), `HTTP_TIMEOUT=10s`, `PUBLICIP_DNS_TIMEOUT=3s` ([README env table](https://github.com/qdm12/ddns-updater/blob/master/README.md)).
- Per cycle, a record is skipped when ([service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go)):
  - it is inside the cooldown window (time since last successful update < `UPDATE_COOLDOWN_PERIOD`),
  - it is within a 1-hour ban period (`const banPeriod = time.Hour`) set after the provider returns `ErrBannedAbuse` ([service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go), [update.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/update.go)),
  - proxied record: the detected public IP equals the last IP that instance recorded in `updates.json` (`shouldUpdateRecordNoLookup`; no DNS lookup is done because Cloudflare proxied records resolve to Cloudflare edge IPs),
  - non-proxied record: a DNS resolution of the hostname contains the public IP (`shouldUpdateRecordWithLookup`, 5 tries; if resolution errors after retries, the update proceeds anyway).
- History (including the "last IP" used for proxied records) is loaded from `updates.json` at startup ([main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go), `readRecords`).
- Cloudflare API calls per possible update ([cloudflare/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/providers/cloudflare/provider.go), `Update`): one `GET /client/v4/zones/{zone}/dns_records?per_page=1`; if the record content already equals the target IP, it returns "up to date" and no `PUT` happens; if no record exists, it `POST`s one; otherwise it `PUT`s the record with type, name, content, `proxied`, and `ttl`.

## 6. Kubernetes specifics and public IP detection

Official Kubernetes support exists in-repo: the [k8s/ directory](https://github.com/qdm12/ddns-updater/blob/master/k8s/README.md) has a kustomize `base` (Deployment, Service, Secret, kustomization) plus overlays `overlay/with-ingress` and `overlay/with-ingress-tls-cert-manager`. The [k8s/README.md](https://github.com/qdm12/ddns-updater/blob/master/k8s/README.md) instructs: download the four base files, edit `secret-config.yaml`, `kubectl apply -k .`, then `kubectl port-forward svc/ddns-updater 8080:80`.

Manifest facts ([k8s/base](https://github.com/qdm12/ddns-updater/blob/master/k8s/base/deployment.yaml)):

- Deployment: image `ghcr.io/qdm12/ddns-updater:latest`, config injected as env `CONFIG` via `envFrom.secretRef` (`name: ddns-updater-config`), `containerPort: 8000`. No `replicas` field (defaults to 1), no probes, no `securityContext`, no PVC, no volume mounts.
- Secret `secret-config.yaml` holds `CONFIG` as a one-line JSON object with a `settings` array.
- Service: `ClusterIP`, port 80 to `targetPort: 8000`.
- Consequence for the planned layout: the official base has no persistence, so a PVC for `/updater/data` is an addition beyond the official example (the README notes the program works without persistent `updates.json`, losing history).

Public IP detection ([README Public IP section](https://github.com/qdm12/ddns-updater/blob/master/README.md)):

- Default `PUBLICIP_FETCHERS=all`: HTTPS echo services and DNS-based detection, cycled per request across all providers so no single service is hammered. HTTPS providers include ipify, icanhazip, ident.me, seeip, wtfismyip, nnev, changeip, spdyn, ipinfo, ipleak, ifconfig, plus arbitrary `url:` endpoints; separate IPv4-only and IPv6-only lists exist.
- DNS providers: `cloudflare` and `opendns`. Implementation is DNS over TLS on TCP port 853 (`net.JoinHostPort(serverHost, "853")` in [pkg/publicip/dns/fetch.go](https://github.com/qdm12/ddns-updater/blob/master/pkg/publicip/dns/fetch.go)); Cloudflare queries TXT `whoami.cloudflare` (CHAOS class) at 1.1.1.1 with TLSName `cloudflare-dns.com`, OpenDNS queries ANY `myip.opendns.com` at 208.67.222.222 with TLSName `dns.opendns.com` ([pkg/publicip/dns/providers.go](https://github.com/qdm12/ddns-updater/blob/master/pkg/publicip/dns/providers.go)). This is DoT, not DNS-over-HTTPS.
- Outbound firewall needs per README: TCP 443 (HTTPS) and UDP 53 (DNS resolution of the records). The DoT fetchers additionally need TCP 853 outbound (source above).
- `RESOLVER_ADDRESS` (optional, e.g. `1.1.1.1:53`) overrides the resolver used for DNS resolution of the configured domains only, useful for split DNS ([README env table](https://github.com/qdm12/ddns-updater/blob/master/README.md), issue #389). `RESOLVER_TIMEOUT=5s` is set in the [Dockerfile ENV](https://github.com/qdm12/ddns-updater/blob/master/Dockerfile) but is absent from the README table.

Gotchas documented in the repo:

- Proxied Cloudflare records ([README, "Special case: Cloudflare"](https://github.com/qdm12/ddns-updater/blob/master/README.md)): the update check relies on the last IP in `updates.json`; if the record is changed manually, the program will not detect it, because an API GET every period "would get you banned especially with a low period duration".
- Rate limiting: `UPDATE_COOLDOWN_PERIOD` is described as "useful to avoid being rate limited or banned" ([README env table](https://github.com/qdm12/ddns-updater/blob/master/README.md)); the code enforces a 1-hour no-update period per record after `ErrBannedAbuse` and notifies via Shoutrrr ([update.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/update.go), [service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go)).
- Restart behavior: every restart triggers one immediate update round (main.go `ForceUpdate`). With a persisted `updates.json` and an unchanged public IP, proxied Cloudflare records are skipped with zero API calls. With a fresh/empty `updates.json`, each proxied record costs one GET; the PUT is skipped because the provider compares the stored record content against the target IP ([cloudflare/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/providers/cloudflare/provider.go)). For non-proxied records a restart costs only DNS lookups while the record resolves to the public IP ([service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go)).

## 7. Two instances updating the same Cloudflare records

The repo documents nothing about multi-instance operation. The relevant facts from code:

- All update-decision state is per process: in-memory records seeded from that instance's own `updates.json` ([main.go](https://github.com/qdm12/ddns-updater/blob/master/cmd/ddns-updater/main.go)). There is no cross-instance lock, lease, or leader election anywhere in the codebase.
- Per cycle, for a proxied Cloudflare record, an instance contacts the Cloudflare API only if the detected public IP differs from its own last recorded IP ([service.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/service.go), `shouldUpdateRecordNoLookup`). Even then, the provider's GET short-circuits the PUT when the record content already equals the target IP ([cloudflare/provider.go](https://github.com/qdm12/ddns-updater/blob/master/internal/provider/providers/cloudflare/provider.go)).
- Therefore, during an overlap where both instances sit behind the same NAT (same public IP) and the DNS records already point at that IP: the instance with persisted history matching the IP makes zero API calls per cycle; the fresh instance makes one GET per record at its immediate startup round and then zero once it persists that IP in its `updates.json` (history is appended on success, [update.go](https://github.com/qdm12/ddns-updater/blob/master/internal/update/update.go)).
- On a real IP change, both instances can independently PUT the same new content (the cooldowns are per instance, not shared), so the record ends up with the same value either way; the doubled API traffic is GETs and one identical PUT per instance, subject to Cloudflare rate limits that the README warns about for low `PERIOD` values.
- Non-proxied records are unaffected by concurrency in the same way: the skip decision is a plain DNS resolution ([README Architecture section](https://github.com/qdm12/ddns-updater/blob/master/README.md)).

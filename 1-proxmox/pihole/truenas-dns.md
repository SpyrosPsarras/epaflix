# TrueNAS LAN DNS

Pi-hole at `192.168.10.30` stores these settings through its HTTPS API:

```json
{
  "config": {
    "dns": {
      "hosts": ["192.168.10.102 truenas.epaflix.com"]
    },
    "misc": {
      "dnsmasq_lines": ["local=/truenas.epaflix.com/"]
    }
  }
}
```

Append these entries to the existing arrays, preserving other records. The host
record takes precedence over the older dnsmasq `address=` mapping to
`192.168.10.101`. The `local=` rule prevents AAAA queries from falling through to
Cloudflare's public wildcard.

The internal Traefik listener at `192.168.10.102:443` serves TrueNAS without
Authentik. The public listener at `192.168.10.101:443` requires Authentik. TrueNAS
still requires its own login. LAN clients must use Pi-hole DNS for this split.

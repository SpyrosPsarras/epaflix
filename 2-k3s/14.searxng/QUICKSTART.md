# SearXNG — quickstart

## Deploy (GitOps)
1. Merge the `14.searxng` manifests + `app-searxng.yaml` to `main`.
2. app-of-apps reconciles → ArgoCD creates the `searxng` Application.
3. Add Pi-hole record `searxng.epaflix.com → 192.168.10.101` in
   `/etc/dnsmasq.d/10-epaflix.conf`, reload FTL.

## Check
    kubectl -n argocd get application searxng
    kubectl -n searxng get pods
    curl --resolve searxng.epaflix.com:443:192.168.10.101 \
      'https://searxng.epaflix.com/search?q=test&format=json' | jq '.results | length'

## Pi tool
Extension at `~/.pi/agent/extensions/searxng-web-search/` (auto-loaded).
Test: `pi -p "search the web for the latest k3s release"`.

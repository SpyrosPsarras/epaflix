#!/bin/bash
# Apply Cloudflare Origin Certificate to Traefik
# Usage: ./apply-cloudflare-cert.sh <base64-cert> <base64-key>

set -e

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: $0 <base64-encoded-cert> <base64-encoded-key>"
    echo ""
    echo "Example:"
    echo "  CERT=\$(cat cert.pem | base64 -w0)"
    echo "  KEY=\$(cat key.pem | base64 -w0)"
    echo "  ./apply-cloudflare-cert.sh \"\$CERT\" \"\$KEY\""
    exit 1
fi

CERT_B64="$1"
KEY_B64="$2"

echo "Creating Cloudflare Origin Certificate secret..."

kubectl create secret tls cloudflare-origin-cert \
    --cert=<(echo "$CERT_B64" | base64 -d) \
    --key=<(echo "$KEY_B64" | base64 -d) \
    --namespace=traefik-system \
    --dry-run=client -o yaml | kubectl apply -f -

echo "✓ Certificate secret created/updated"

echo ""
echo "Now updating Authentik IngressRoute to use the certificate..."

cat <<'EOF' | kubectl apply -f -
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: authentik-https
  namespace: app-authentik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`auth.epaflix.com`)
      kind: Rule
      services:
        - name: authentik-server
          port: 80
  tls:
    secretName: cloudflare-origin-cert
EOF

echo "✓ IngressRoute updated to use Cloudflare Origin Certificate"
echo ""
echo "Wait 5-10 seconds, then test: https://auth.epaflix.com"
echo "The certificate warning should be gone!"

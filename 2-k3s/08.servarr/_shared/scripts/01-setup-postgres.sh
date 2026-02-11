#!/bin/bash
# PostgreSQL Database Setup for Servarr Apps
# Run this script to create databases and users on PostgreSQL server

set -e

POSTGRES_HOST="192.168.10.105"
POSTGRES_PORT="5432"
POSTGRES_ADMIN_USER="postgres"
POSTGRES_ADMIN_PASSWORD="<POSTGRES_PASSWORD>"

echo "========================================"
echo "Servarr PostgreSQL Database Setup"
echo "========================================"
echo ""
echo "PostgreSQL Server: $POSTGRES_HOST:$POSTGRES_PORT"
echo ""

# Function to generate secure password
generate_password() {
    openssl rand -base64 24 | tr -d "=+/" | cut -c1-20
}

# Generate passwords
SONARR_PASSWORD=$(generate_password)
SONARR2_PASSWORD=$(generate_password)
RADARR_PASSWORD=$(generate_password)
PROWLARR_PASSWORD=$(generate_password)
JELLYSEERR_PASSWORD=$(generate_password)

echo "Generated secure passwords for database users"
echo ""

# Create databases and users
echo "Creating databases and users..."
echo ""

PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" psql -h "$POSTGRES_HOST" -U "$POSTGRES_ADMIN_USER" -p "$POSTGRES_PORT" <<EOF
-- Sonarr (TV Shows)
CREATE DATABASE "sonarr-main";
CREATE USER sonarr WITH PASSWORD '$SONARR_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE "sonarr-main" TO sonarr;
ALTER DATABASE "sonarr-main" OWNER TO sonarr;

-- Sonarr2 (Anime)
CREATE DATABASE "sonarr2-main";
CREATE USER sonarr2 WITH PASSWORD '$SONARR2_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE "sonarr2-main" TO sonarr2;
ALTER DATABASE "sonarr2-main" OWNER TO sonarr2;

-- Radarr (Movies)
CREATE DATABASE "radarr-main";
CREATE USER radarr WITH PASSWORD '$RADARR_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE "radarr-main" TO radarr;
ALTER DATABASE "radarr-main" OWNER TO radarr;

-- Prowlarr (Indexers)
CREATE DATABASE "prowlarr-main";
CREATE USER prowlarr WITH PASSWORD '$PROWLARR_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE "prowlarr-main" TO prowlarr;
ALTER DATABASE "prowlarr-main" OWNER TO prowlarr;

-- Jellyseerr (Requests)
CREATE DATABASE "jellyseerr";
CREATE USER jellyseerr WITH PASSWORD '$JELLYSEERR_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE "jellyseerr" TO jellyseerr;
ALTER DATABASE "jellyseerr" OWNER TO jellyseerr;

\l
EOF

echo ""
echo "✓ Databases and users created successfully!"
echo ""

# Create Kubernetes secret
echo "Creating Kubernetes secret..."
echo ""

kubectl create secret generic servarr-postgres \
  --from-literal=sonarr-host="$POSTGRES_HOST" \
  --from-literal=sonarr-port="$POSTGRES_PORT" \
  --from-literal=sonarr-database="sonarr-main" \
  --from-literal=sonarr-user="sonarr" \
  --from-literal=sonarr-password="$SONARR_PASSWORD" \
  --from-literal=sonarr2-host="$POSTGRES_HOST" \
  --from-literal=sonarr2-port="$POSTGRES_PORT" \
  --from-literal=sonarr2-database="sonarr2-main" \
  --from-literal=sonarr2-user="sonarr2" \
  --from-literal=sonarr2-password="$SONARR2_PASSWORD" \
  --from-literal=radarr-host="$POSTGRES_HOST" \
  --from-literal=radarr-port="$POSTGRES_PORT" \
  --from-literal=radarr-database="radarr-main" \
  --from-literal=radarr-user="radarr" \
  --from-literal=radarr-password="$RADARR_PASSWORD" \
  --from-literal=prowlarr-host="$POSTGRES_HOST" \
  --from-literal=prowlarr-port="$POSTGRES_PORT" \
  --from-literal=prowlarr-database="prowlarr-main" \
  --from-literal=prowlarr-user="prowlarr" \
  --from-literal=prowlarr-password="$PROWLARR_PASSWORD" \
  --from-literal=jellyseerr-host="$POSTGRES_HOST" \
  --from-literal=jellyseerr-port="$POSTGRES_PORT" \
  --from-literal=jellyseerr-database="jellyseerr" \
  --from-literal=jellyseerr-user="jellyseerr" \
  --from-literal=jellyseerr-password="$JELLYSEERR_PASSWORD" \
  -n servarr \
  --dry-run=client -o yaml > secrets/postgres-secret-generated.yaml

echo "✓ Kubernetes secret manifest created: secrets/postgres-secret-generated.yaml"
echo ""

echo "========================================"
echo "Database Credentials (SAVE THESE!)"
echo "========================================"
echo ""
echo "Sonarr:"
echo "  Database: sonarr-main"
echo "  User: sonarr"
echo "  Password: $SONARR_PASSWORD"
echo ""
echo "Sonarr2:"
echo "  Database: sonarr2-main"
echo "  User: sonarr2"
echo "  Password: $SONARR2_PASSWORD"
echo ""
echo "Radarr:"
echo "  Database: radarr-main"
echo "  User: radarr"
echo "  Password: $RADARR_PASSWORD"
echo ""
echo "Prowlarr:"
echo "  Database: prowlarr-main"
echo "  User: prowlarr"
echo "  Password: $PROWLARR_PASSWORD"
echo ""
echo "Jellyseerr:"
echo "  Database: jellyseerr"
echo "  User: jellyseerr"
echo "  Password: $JELLYSEERR_PASSWORD"
echo ""
echo "========================================"
echo ""
echo "Next steps:"
echo "1. Save the passwords above securely"
echo "2. Apply the secret: kubectl apply -f secrets/postgres-secret-generated.yaml"
echo "3. Continue with storage and app deployment"
echo ""

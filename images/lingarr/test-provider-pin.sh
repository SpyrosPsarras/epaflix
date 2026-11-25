#!/usr/bin/env bash
# Runs the lingarr enforce-db-invariants init script against a real PostgreSQL
# that is behind, at and past Lingarr's provider-settings move (migration M0026),
# using Lingarr's own migrations at UPSTREAM_COMMIT. Needs /scripts/t3-run
# (2-k3s/29.t3-build). Usage: bash images/lingarr/test-provider-pin.sh
set -euo pipefail
root=$(git rev-parse --show-toplevel)
commit=$(awk -F= '/^ARG UPSTREAM_COMMIT=/ {print $2}' "$root/images/lingarr/Dockerfile")
work=$(mktemp -d)
trap 'rm -r "$work"' EXIT
python3 - "$root" "$work" <<'PY'
import pathlib, sys, yaml
root, work = map(pathlib.Path, sys.argv[1:])
spec = next(yaml.safe_load_all((root / "2-k3s/08.servarr/lingarr/lingarr.yaml").read_text()))
(work / "init.sh").write_text(spec["spec"]["template"]["spec"]["initContainers"][0]["command"][2])
PY
cp "$root/2-k3s/08.servarr/lingarr/files/reconcile-ai-provider.sql" "$work/"
cat > "$work/run.sh" <<'SH'
set -eu
apt-get update -qq >/dev/null && apt-get install -y -qq postgresql >/dev/null 2>&1
bin=$(ls -d /usr/lib/postgresql/*/bin)
mkdir -p /pg /sql && chown postgres /pg && cp /work/reconcile-ai-provider.sql /sql/
su postgres -c "$bin/initdb -D /pg -A trust >/dev/null && $bin/pg_ctl -D /pg -l /pg/log -o '-k /tmp' -w start >/dev/null"
export PGHOST=/tmp PGUSER=postgres DB_HOST=/tmp DB_PORT=5432 DB_USERNAME=postgres PGPASSWORD=unused
export CLIPROXY_API_KEY=omp-test-not-a-secret
git clone -q https://github.com/lingarr-translate/lingarr.git /src && git -C /src checkout -q "$COMMIT"
mkdir /src/Migrator
cat > /src/Migrator/Migrator.csproj <<'XML'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework><Nullable>enable</Nullable><ImplicitUsings>enable</ImplicitUsings></PropertyGroup>
  <ItemGroup><ProjectReference Include="../Lingarr.Migrations/Lingarr.Migrations.csproj" /><PackageReference Include="FluentMigrator.Runner" /></ItemGroup>
</Project>
XML
cat > /src/Migrator/Program.cs <<'CS'
using FluentMigrator.Runner;
using Lingarr.Migrations;
using Microsoft.Extensions.DependencyInjection;
using var provider = new ServiceCollection().AddFluentMigrator(args[0], "postgres").BuildServiceProvider();
using var scope = provider.CreateScope();
scope.ServiceProvider.GetRequiredService<IMigrationRunner>().MigrateUp(long.Parse(args[1]));
CS
dotnet build -c Release --nologo -v q -o /migrator /src/Migrator >/dev/null
migrate() { dotnet /migrator/Migrator.dll "Host=/tmp;Username=postgres;Database=$1" "$2" >/dev/null; }
init() { DB_DATABASE=$1 sh /work/init.sh >/dev/null 2>/tmp/init.err || { cat /tmp/init.err; exit 1; }; }
q() { psql -X -d "$1" -tAc "$2"; }
expect() { [ "$2" = "$3" ] || { echo "FAIL $1: got '$2' want '$3'"; exit 1; }; echo "ok   $1"; }
pinned() {
  expect "$1: one localai service at position 0" "$(q "$2" "select count(*) || ':' || min(provider) || ':' || min(position) from translation_services")" "1:localai:0"
  expect "$1: five pinned service settings" "$(q "$2" "select count(*) from translation_service_settings t join translation_services s on s.id = t.translation_service_id where (t.key, t.value) in (('local_ai_endpoint', 'http://cliproxy-free.remote-pi.svc.cluster.local:8317/v1/chat/completions'), ('local_ai_model', 'or-free'), ('local_ai_chat_request_template', ''), ('local_ai_structured_output', 'true'), ('local_ai_api_key', '$CLIPROXY_API_KEY'))")" "5"
  expect "$1: provider keys gone from settings" "$(q "$2" "select count(*) from settings where key in ('service_type', 'local_ai_model', 'local_ai_endpoint', 'local_ai_api_key', 'local_ai_structured_output', 'local_ai_chat_request_template')")" "0"
}

createdb upgrade
migrate upgrade 23
init upgrade
expect "upgrade: pins in settings before M0026" "$(q upgrade "select count(*) from settings where (key, value) in (('service_type', '[\"localai\"]'), ('local_ai_model', 'or-free'), ('local_ai_structured_output', 'true'))")" "3"
migrate upgrade 26
pinned "upgrade after M0026" upgrade
init upgrade
init upgrade
pinned "upgrade re-run" upgrade
q upgrade "update translation_service_settings set value = 'false' where key = 'local_ai_structured_output'; update translation_service_settings set value = 'other' where key = 'local_ai_model'; update translation_services set position = 3; insert into translation_services (provider, name, position, created_at, updated_at) values ('openai', 'OpenAI', 0, now(), now())" >/dev/null
init upgrade
pinned "upgrade after UI edits" upgrade

createdb fresh
init fresh
expect "fresh: first boot skips" "$(q fresh "select to_regclass('public.settings') is null")" "t"
migrate fresh 26
init fresh
pinned "fresh after all migrations" fresh

createdb early
migrate early 21
init early
migrate early 26
pinned "M0022 still inserts after an early init" early
echo PASS
SH
tar -C "$work" -cf - init.sh reconcile-ai-provider.sql run.sh |
  /scripts/t3-run mcr.microsoft.com/dotnet/sdk:10.0 -- sh -c "mkdir /work && tar -C /work -xf - && COMMIT=$commit sh /work/run.sh"

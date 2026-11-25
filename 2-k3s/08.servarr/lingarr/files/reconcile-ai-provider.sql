-- Boot guard: pin lingarr's AI provider to cliproxy-free's tiered pool.
-- Runs from the enforce-db-invariants
-- initContainer psql flow (see lingarr.yaml); same rationale as #925 - these
-- settings are live-only DB state otherwise, and a rebuild or a stray UI edit
-- silently repoints translations at whatever the UI last left behind.
--
-- cliproxy-free serves or-free: Antigravity via main cliproxy, in order:
-- gemini-3.8-flash-high, claude-opus-4-6-thinking, claude-sonnet-4-6,
-- then Groq and Gemini free as fallback. lingarr retries 429s
-- client-side (max_retries, retry_delay), keeping ONE service, endpoint and model.
-- The Lingarr key must be in main cliproxy's live api-keys list via the
-- management API (not git-durable); if missing, Antigravity 401s and free tiers serve.
--
-- Secret handling: the key is read from the environment and echoed nowhere,
-- so a failing statement cannot leak it into pod logs and on to Loki (#634,
-- #702, #824, #911).
\set cliproxy_key `printenv CLIPROXY_API_KEY`
\o /dev/null
SELECT set_config('lingarr.cliproxy_key', :'cliproxy_key', false);
\o

-- A missing secret is not a first-boot condition: an empty api-key would boot
-- the pod and then 401 on every translation, a failure that looks like a
-- broken model instead of a missing secret. Fail the initContainer instead.
DO $$
BEGIN
  IF current_setting('lingarr.cliproxy_key', true) IS NULL
     OR current_setting('lingarr.cliproxy_key') !~ '^omp-[A-Za-z0-9._-]+$' THEN
    RAISE EXCEPTION 'CLIPROXY_API_KEY is empty or not omp-shaped - the lingarr-cliproxy-api-key secret did not reach the initContainer';
  END IF;
END
$$;

DO $$
DECLARE
  svc integer;
BEGIN
  -- First boot: migrations have not created settings yet. Skipping here is the
  -- established behaviour of the sibling -c guard in lingarr.yaml; the values
  -- land on the next restart.
  IF to_regclass('public.settings') IS NULL THEN
    RAISE NOTICE 'settings table does not exist yet - first boot, skipping';
    RETURN;
  END IF;

  -- Before Lingarr 1.5.0 the provider lives in settings. Migration M0026 moves
  -- these keys into translation_services/translation_service_settings and
  -- deletes them here, so they must be in place when it runs.
  IF to_regclass('public.translation_services') IS NULL THEN
    -- endpoint is the FULL chat-completions URL: LocalAiService POSTs to it
    -- directly and treats a trailing ".../completions" as its chat-API switch.
    -- Cluster-internal name, not the traefik one: no TLS hop, no ingress
    -- dependency, and it still works when the LAN-facing path does not.
    INSERT INTO settings (key, value) VALUES ('local_ai_endpoint', 'http://cliproxy-free.remote-pi.svc.cluster.local:8317/v1/chat/completions')
     ON CONFLICT (key) DO UPDATE SET value = excluded.value;
    -- The one alias lingarr may use, served by cliproxy-free's tiered pool.
    INSERT INTO settings (key, value) VALUES ('local_ai_model', 'or-free')
     ON CONFLICT (key) DO UPDATE SET value = excluded.value;
    -- Empty string makes LocalAiService use its default OpenAI-shaped chat body.
    INSERT INTO settings (key, value) VALUES ('local_ai_chat_request_template', '')
     ON CONFLICT (key) DO UPDATE SET value = excluded.value;
    -- localai only: a provider switch made in the UI survives only until the
    -- next pod restart. To use a different provider, change it HERE.
    INSERT INTO settings (key, value) VALUES ('service_type', '["localai"]')
     ON CONFLICT (key) DO UPDATE SET value = excluded.value;
    -- Written plaintext: Decrypt() falls back to the raw value on
    -- CryptographicException. current_setting keeps the key out of the
    -- statement text, so a failed write cannot log it.
    INSERT INTO settings (key, value) VALUES ('local_ai_api_key', current_setting('lingarr.cliproxy_key'))
     ON CONFLICT (key) DO UPDATE SET value = excluded.value;
    -- UPDATE, not upsert: migration M0022 inserts this row with a plain INSERT.
    -- Off sends every batch to the schema-less path (lingarr-translate/lingarr#574).
    UPDATE settings SET value = 'true' WHERE key = 'local_ai_structured_output';
    RETURN;
  END IF;

  -- From Lingarr 1.5.0: exactly one service, localai, at position 0. Other
  -- services added in the UI are removed with their settings, the same posture
  -- as the service_type pin above.
  SELECT id INTO svc FROM translation_services
   WHERE provider = 'localai' ORDER BY "position", id LIMIT 1;
  IF svc IS NULL THEN
    INSERT INTO translation_services (provider, name, "position", created_at, updated_at)
    VALUES ('localai', 'LocalAI / Ollama', 0, now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc')
    RETURNING id INTO svc;
  END IF;
  DELETE FROM translation_services WHERE id <> svc;
  UPDATE translation_services SET "position" = 0 WHERE id = svc AND "position" <> 0;

  INSERT INTO translation_service_settings (translation_service_id, key, value) VALUES
    (svc, 'local_ai_endpoint', 'http://cliproxy-free.remote-pi.svc.cluster.local:8317/v1/chat/completions'),
    (svc, 'local_ai_model', 'or-free'),
    (svc, 'local_ai_chat_request_template', ''),
    (svc, 'local_ai_api_key', current_setting('lingarr.cliproxy_key')),
    (svc, 'local_ai_structured_output', 'true')
   ON CONFLICT (translation_service_id, key) DO UPDATE SET value = excluded.value;
END
$$;

-- Prove it, and fail the initContainer if any pin did not land: a guard that
-- writes nothing and exits 0 is worse than no guard.
DO $$
DECLARE
  n integer;
  services integer;
BEGIN
  IF to_regclass('public.settings') IS NULL THEN RETURN; END IF;

  IF to_regclass('public.translation_services') IS NULL THEN
    SELECT count(*) INTO n
      FROM settings
     WHERE (key = 'local_ai_endpoint'
            AND value = 'http://cliproxy-free.remote-pi.svc.cluster.local:8317/v1/chat/completions')
        OR (key = 'local_ai_model'    AND value = 'or-free')
        OR (key = 'local_ai_chat_request_template' AND value = '')
        OR (key = 'service_type'      AND value = '["localai"]')
        OR (key = 'local_ai_api_key'  AND value = current_setting('lingarr.cliproxy_key'));
  ELSE
    SELECT count(*) INTO services FROM translation_services;
    SELECT count(*) INTO n
      FROM translation_services s
      JOIN translation_service_settings t ON t.translation_service_id = s.id
     WHERE s.provider = 'localai' AND s."position" = 0
       AND ((t.key = 'local_ai_endpoint'
             AND t.value = 'http://cliproxy-free.remote-pi.svc.cluster.local:8317/v1/chat/completions')
         OR (t.key = 'local_ai_model' AND t.value = 'or-free')
         OR (t.key = 'local_ai_chat_request_template' AND t.value = '')
         OR (t.key = 'local_ai_structured_output' AND t.value = 'true')
         OR (t.key = 'local_ai_api_key' AND t.value = current_setting('lingarr.cliproxy_key')));
    IF services <> 1 THEN
      RAISE EXCEPTION 'AI provider pins incomplete: % translation services, want 1', services;
    END IF;
  END IF;

  IF n <> 5 THEN
    RAISE EXCEPTION 'AI provider pins incomplete: % of 5 settings hold the wanted values', n;
  END IF;

  RAISE NOTICE 'AI provider pinned: or-free -> Antigravity gemini-3.8-flash-high, claude-opus-4-6-thinking, claude-sonnet-4-6; Groq/Gemini free fallback';
END
$$;

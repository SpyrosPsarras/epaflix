-- Boot guard: pin lingarr's AI provider to cliproxy -> OpenRouter's minimax
-- free model. Runs from the same initContainer psql flow as
-- reconcile-job-queue.sql (see lingarr.yaml); same rationale as #925 - these
-- settings are live-only DB state otherwise, and a rebuild or a stray UI edit
-- silently repoints translations at whatever the UI last left behind.
--
-- The 429 daily cap on the free model is handled at the cliproxy layer, not
-- here: the openrouter provider (priority 10) and the ollama provider
-- (aya-expanse:8b, priority 0) both serve the or-minimax-m3:free alias, and
-- cliproxy fails over to ollama while the openrouter credential cools down.
-- lingarr therefore keeps ONE service, endpoint and model.
--
-- Secret handling copies reconcile-config.psql in 17.remote-pi/cliproxy: the
-- key is read from the environment and echoed nowhere, so a failing statement
-- cannot leak it into pod logs and on to Loki (#634, #702, #824, #911).
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
BEGIN
  -- First boot: migrations have not created settings yet. Skipping here is the
  -- established behaviour of the sibling -c guard in lingarr.yaml; the values
  -- land on the next restart.
  IF to_regclass('public.settings') IS NULL THEN
    RAISE NOTICE 'settings table does not exist yet - first boot, skipping';
    RETURN;
  END IF;

  -- Upserts, not bare UPDATEs: lingarr tracks the :main dev branch, so a row
  -- upstream stops seeding must not leave the pin silently absent - the same
  -- INSERT ON CONFLICT shape the sibling guard in lingarr.yaml uses.
  --
  -- endpoint is the FULL chat-completions URL: LocalAiService POSTs to it
  -- directly and treats a trailing ".../completions" as its chat-API switch.
  -- Cluster-internal name, not the traefik one: no TLS hop, no ingress
  -- dependency, and it still works when the LAN-facing path does not.
  INSERT INTO settings (key, value) VALUES ('local_ai_endpoint', 'http://cliproxy.remote-pi.svc.cluster.local:8317/v1/chat/completions')
   ON CONFLICT (key) DO UPDATE SET value = excluded.value;

  -- The one model lingarr may use: OpenRouter's minimax free variant through
  -- cliproxy. It rides OpenRouter's shared daily cap, so 429 bursts are normal;
  -- cliproxy fails over to the local ollama for the duration of the cooldown,
  -- and lingarr retries whatever slips through client-side (max_retries,
  -- retry_delay).
  INSERT INTO settings (key, value) VALUES ('local_ai_model', 'or-minimax-m3:free')
   ON CONFLICT (key) DO UPDATE SET value = excluded.value;

  -- The previous ollama-era template carried ollama-only body fields
  -- ("options"). Empty string makes LocalAiService fall back to its default
  -- OpenAI-shaped chat body.
  INSERT INTO settings (key, value) VALUES ('local_ai_chat_request_template', '')
   ON CONFLICT (key) DO UPDATE SET value = excluded.value;

  -- Provider selection: localai is lingarr's OpenAI-compatible service. Pinned
  -- on every boot on purpose - a provider switch made in the UI survives only
  -- until the next pod restart. Same posture as the batch-translation pins in
  -- lingarr.yaml; if you want a different provider, change it HERE.
  INSERT INTO settings (key, value) VALUES ('service_type', '["localai"]')
   ON CONFLICT (key) DO UPDATE SET value = excluded.value;

  -- Written plaintext: SettingService.Decrypt() falls back to the raw value on
  -- CryptographicException, so LocalAiService reads it as-is. current_setting
  -- keeps the key out of the statement text, so a failed write cannot log it.
  INSERT INTO settings (key, value) VALUES ('local_ai_api_key', current_setting('lingarr.cliproxy_key'))
   ON CONFLICT (key) DO UPDATE SET value = excluded.value;
END
$$;

-- Prove it, and fail the initContainer if any pin did not land: a guard that
-- writes nothing and exits 0 is worse than no guard.
DO $$
DECLARE
  n integer;
BEGIN
  IF to_regclass('public.settings') IS NULL THEN RETURN; END IF;

  SELECT count(*) INTO n
    FROM settings
   WHERE (key = 'local_ai_endpoint'
          AND value = 'http://cliproxy.remote-pi.svc.cluster.local:8317/v1/chat/completions')
      OR (key = 'local_ai_model'    AND value = 'or-minimax-m3:free')
      OR (key = 'local_ai_chat_request_template' AND value = '')
      OR (key = 'service_type'      AND value = '["localai"]')
      OR (key = 'local_ai_api_key'  AND value = current_setting('lingarr.cliproxy_key'));

  IF n <> 5 THEN
    RAISE EXCEPTION 'AI provider pins incomplete: % of 5 settings hold the wanted values', n;
  END IF;

  RAISE NOTICE 'AI provider pinned: cliproxy -> OpenRouter minimax-m3:free';
END
$$;

DO $guard$
DECLARE
  cleared_queue int := 0;
  reaped_jobs   int := 0;
BEGIN
  IF to_regclass('hangfire.jobqueue') IS NULL OR to_regclass('hangfire.job') IS NULL THEN
    RAISE NOTICE 'reconcile-job-queue: hangfire schema absent (first install) - skipped';
    RETURN;
  END IF;

  DELETE FROM hangfire.jobqueue q
   USING hangfire.job j
   WHERE q.jobid = j.id
     AND j.invocationdata->>'Type' = 'Lingarr.Server.Jobs.TranslationJob, Lingarr.Server';
  GET DIAGNOSTICS cleared_queue = ROW_COUNT;

  DELETE FROM hangfire.job j
   WHERE j.invocationdata->>'Type' = 'Lingarr.Server.Jobs.TranslationJob, Lingarr.Server'
     AND j.statename = 'Enqueued'
     AND NOT EXISTS (SELECT 1 FROM hangfire.jobqueue q WHERE q.jobid = j.id);
  GET DIAGNOSTICS reaped_jobs = ROW_COUNT;

  RAISE NOTICE 'reconcile-job-queue: cleared % queued TranslationJob entries, reaped % unreachable job rows', cleared_queue, reaped_jobs;
END
$guard$;

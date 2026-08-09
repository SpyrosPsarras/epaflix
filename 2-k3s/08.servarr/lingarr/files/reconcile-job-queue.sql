-- Clear stale Hangfire TranslationJob work before Lingarr boots (#870).
--
-- Why: on startup ScheduleInitializationService runs ScheduleService.Initialize()
-- (ScheduleService.cs:107), which calls TranslationRequestService
-- .ResumeTranslationRequests() (TranslationRequestService.cs:453-476). That method
-- re-enqueues EVERY Pending/InProgress request unconditionally (line 471) and
-- repoints translation_requests.job_id at the new job, but never deletes the job the
-- request pointed at before. The old hangfire.job row stays 'Enqueued' and its
-- hangfire.jobqueue row stays fetchable, now referenced by nothing. Cancel only
-- deletes the job named by job_id (line 341-344), so the orphan is uncancellable,
-- and TranslationJob has no [DisableConcurrentExecution], so it can really run.
--
-- No work is lost, by case analysis at the instant this runs:
--   a) request is Pending/InProgress -> boot re-enqueues it seconds later anyway;
--   b) request is terminal           -> it must not run (that is the cancel bug);
--   c) request row was deleted       -> it must not run.
--
-- Caveat on case (a) - do not read it as absolute loss-freeness.
-- ScheduleInitializationService.OnApplicationStarted is `async void` and the
-- resume loop inside Initialize() is not transactional. If it throws part way
-- through, the exception is swallowed and boot continues: the requests it had
-- already reached are re-enqueued, and the ones after the throw are left with
-- their old queue row already deleted by this guard and no new job. Those
-- requests then sit idle until the NEXT successful boot. It self-heals, because
-- `translation_requests.job_id` and `status` are durable and the next resume
-- picks them up again from the same rows - so this is delay, not permanent
-- loss. Permanent loss would need `job_id` to be nulled, which this guard
-- never does (see the safety contract below).
--
-- SAFETY CONTRACT - read before changing anything here:
--   * PRE-BOOT ONLY. It is safe because the lingarr Deployment is `replicas: 1`
--     with `strategy: Recreate`, so no Lingarr process is alive while an
--     initContainer runs. That single-writer window is the whole guarantee.
--     NEVER move this to a CronJob or run it against a live pod - scale the
--     Deployment to 0 first (see lingarr/README.md).
--   * NEVER write to public.translation_requests. Nulling job_id sends
--     ResumeTranslationRequests down its JobId == null branch
--     (TranslationRequestService.cs:462-469), which silently marks every pending
--     request Interrupted - permanent, user-visible work loss.
--   * Scope stays TranslationJob. The recurring jobs (SyncMovieJob, SyncShowJob,
--     StatisticsJob, AutomatedTranslationJob) are owned by Hangfire's recurring
--     scheduler and are out of scope.
--   * hangfire.state and hangfire.jobparameter need no cleanup here - both are
--     ON DELETE CASCADE against hangfire.job (state_jobid_fkey,
--     jobparameter_jobid_fkey). Lingarr's own audit trail lives in
--     public.translation_request_events and is untouched.
--
-- This is a WORKAROUND pending an upstream fix. See lingarr/README.md for the
-- retire condition.
--
-- One DO block, so it is atomic and idempotent - it is a delete against a
-- predicate, so a second run on the same state matches nothing and reports 0/0.
-- Do NOT add BEGIN/COMMIT: a DO block is already a single transaction and
-- explicit transaction control is not allowed inside one.
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

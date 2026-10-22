# Serial Greek subtitle translation

## Intent

Spend the free AI tier on finished Greek subtitles, not fragments.

## Measured problem

Measured 15-22 Oct 2026 from the Lingarr database and Loki.

The tier allows about 200 batches of 25 lines each per day. Four to six files
currently translate together because the translate PATCH only queues a Bazarr
job and returns in under one second. Bazarr runs up to four jobs at once.
Lingarr recorded 10 completed and 1,144 failed translations in a week.
Retries wait 216 hours plus three search rounds, longer than Lingarr's weekly
cleanup keeps partial lines. Bazarr's in-memory queue disappears on restart.

## Goals and non-goals

Finish files within the quota, prefer small files, and retry before partial
work expires. Persist ready items in autotranslate's state file.
Do not change Lingarr or Bazarr. Do not impose a file-size cutoff.
The backlog will not reach zero at a capacity of about 5,000 lines per day.

## Approaches

- O1 chosen: autotranslate owns the order and waits for Bazarr jobs to end.
  Its persisted ready items survive a restart.
- O2 rejected: setting Bazarr `concurrent_jobs=1` serialises all searches,
  provides no translation order or retry timing, and still loses its queue
  on restart.
- O3 rejected: patching Lingarr to wait for quota adds more C# to a patch
  meant to be retired. Bazarr also gives up after 32 minutes.

## Translation rules

1. R1: Send one file, then poll Bazarr until no translation job is pending
   or running before sending another. Treat missing jobs, request errors,
   failed jobs, and the four-hour timeout as failed attempts.
2. R2: Pick the ready item whose base-language subtitle has the smallest
   `file_size` in bytes. There is no hard size cutoff.
3. R3: Retry still-wanted items 24 hours after an attempt ends, whether it
   completed or failed. Go straight to translation without new search
   rounds. In Lingarr's batch-contract patch,
   `images/lingarr/batch-contract.patch`, `ReuseOrAwaitExistingAsync` returns
   a completed request's stored lines without quota.
   `CarryOverPriorLinesAsync` carries a failed attempt's translated lines
   into the next request.
4. R4: Pause all sends for one hour after a failed attempt.

New items still get three search rounds eight hours apart. Failed search
requests do not count. Bazarr's six-hourly wanted search keeps running.

## Owner's calls

D1: Use a 24-hour retry grace measured from attempt end, straight to translation.
D2: Pause the worker for one hour after a failure.

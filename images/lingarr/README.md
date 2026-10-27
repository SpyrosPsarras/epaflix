# Lingarr batch-contract build

Upstream `ghcr.io/lingarr-translate/lingarr:1.4.1` at commit `f31ddbb` with one
patch, `batch-contract.patch`, republished as `ghcr.io/spyrospsarras/lingarr`.
Release 1.4.1 ships part of behaviour 4 via lingarr-translate/lingarr#556.
Behaviours 1, 2, 3, 5 and 6 still need the overlay.

## What the patch changes

Lingarr's `LocalAiService` chat batch path (used for cliproxy-free -> OpenRouter
free models) had three defects that together published untranslated subtitles
as translated, and the content-translation HTTP API had a fourth defect that
made big files untranslatable through Bazarr:

1. When the structured (`response_format: json_schema`) request failed to
   parse, it fell back to a request with no schema and no JSON instruction.
   Free models answered that with prose, so the fallback never parsed and the
   batch was lost. The fallback is removed; a parse failure is retried with the
   same structured request (`TranslateBatchAsync` already had the loop).
2. A structured response with fewer, extra, duplicate or empty positions was
   accepted as-is. `ValidateBatchCoverage` now rejects it (retryable). An empty
   source line may still come back empty. Empty `choices` and null `content`
   are retryable too instead of crashing the attempt.
3. `SubtitleTranslationService.RunBatch` filled any position the batch service
   did not return with the English source and logged a warning. It now throws,
   which fails the job (both callers already set `Failed` before writing
   anything: `TranslationJob` and `TranslateContentAsync`).
4. Upstream 1.4.1 lingarr-translate/lingarr#556 detaches `TranslateContentAsync`
   from the request token and reuses completed requests only when every
   position and source line matches. The overlay keeps both. Upstream returns
   409 for an active duplicate, which Bazarr treats as failure after its
   1920s HTTP timeout.
   The overlay instead waits for the running request
   (`AwaitDuplicateRequestAsync`), then uses upstream's completed-request
   check or starts fresh. A disconnecting waiter stops polling without
   cancelling the running translation. A Pending/InProgress request with no
   line activity for 60 minutes is reaped as Interrupted so the next POST
   starts fresh. This completes the retry convergence endorsed in
   lingarr-translate/lingarr#542, maintainer option 3.
5. `TranslateBatchWithStructuredOutput` wrapped every non-200 as
   `TranslationException`, which `TranslateBatchAsync`'s retry catch filters
   out (it matches on `HttpRequestException.StatusCode`) - so a provider 429
   failed the batch on attempt 1. groq's shared free-tier TPM window 429s
   with a "try again in ~8s" hint; 429/503 now surface as
   `HttpRequestException` and the existing 1/2/4/8/16s backoff retries them.
6. Bazarr re-POSTs a failed file as a new request, and `TranslateContentAsync`
   started every request from line 1. With the free pool out of daily quota
   most of the day, each retry re-spent its few successful batches on the same
   opening lines: 1 file completed between 2026-09-27 and 2026-10-05, ~200
   failed per day, one episode retried 62 times in a day. A new batch request
   now carries over the lines of the newest Failed/Interrupted/Cancelled
   request for the same content whose position and source text still match
   and whose target is neither empty nor the source copied through
   (`CarryOverPriorLinesAsync`), stores them on the new request, and sends only
   the rest to the provider.

Generate-API (non-chat) batches and per-line translation internals are unchanged
by the overlay. Upstream's individual-line content path and translated context
remain intact. The upstream test asserting behaviour 3 is deleted; the controller
tests for active duplicates are adapted to behaviour 4; upstream disconnect
and API cancellation tests remain.
The patched suite passes all 335 tests, including TranslationRequestServiceTests
for behaviours 4 and 6 and the rate-limit retry tests for behaviour 5.

M0022 seeds `local_ai_structured_output=true`; the init container resets existing
rows to true so lingarr-translate/lingarr#574 cannot bypass the batch contract.

## Measured need

2026-09-13, production: 91 structured failures, 522 positions kept in English in
three hours across two free models (nemotron-3-super, nex-n2.5-pro). A 100-line
controlled job lost a whole 24-line batch this way while returning HTTP 200.

## Build

`.github/workflows/build-lingarr.yml`, on changes under `images/lingarr/**`.
Pin the printed digest in `2-k3s/08.servarr/kustomization.yaml`.

## Retire when

Retire only when upstream ships all six behaviours above. Behaviour 4 requires
both waiting for an active duplicate instead of returning 409 and reaping a
request with no line activity for 60 minutes. Then drop this directory, the
workflow, and the overlay image pin, and pin `ghcr.io/lingarr-translate/lingarr`
to the new release tag with its GHCR digest. Tracked in
`.github/workflows/upstream-release-watch.yml`.

Moving `UPSTREAM_COMMIT` means re-checking `git apply --check` and picking the
matching upstream image digest for `BASE_IMAGE` (its
`org.opencontainers.image.revision` label must equal `UPSTREAM_COMMIT`).

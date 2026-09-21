# Lingarr batch-contract build

Upstream `ghcr.io/lingarr-translate/lingarr:main` at commit `28f6a19` with one
patch, `batch-contract.patch`, republished as `ghcr.io/spyrospsarras/lingarr`.

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
4. `TranslateContentAsync` tied the translation to `HttpContext.RequestAborted`:
   a caller whose HTTP timeout is shorter than the translation (Bazarr blocks
   1920s) disconnected, the request got cancelled, and the file never
   translated - an infinite retry loop on big files. The translation now runs
   detached from the request token (only the UI/API cancel can stop it), and a
   re-POST for content that is still translating waits for the running request
   and delivers its stored lines (`AwaitDuplicateRequestAsync`). A running
   request with no line activity for 60 minutes is a restart zombie: it is
   reaped as Interrupted so the next POST starts fresh. Endorsed upstream
   (lingarr-translate/lingarr#542, maintainer option 3).
5. `TranslateBatchWithStructuredOutput` wrapped every non-200 as
   `TranslationException`, which `TranslateBatchAsync`'s retry catch filters
   out (it matches on `HttpRequestException.StatusCode`) - so a provider 429
   failed the batch on attempt 1. groq's shared free-tier TPM window 429s
   with a "try again in ~8s" hint; 429/503 now surface as
   `HttpRequestException` and the existing 1/2/4/8/16s backoff retries them.

Generate-API (non-chat) batches and per-line translation are unchanged. The
upstream test asserting behaviour 3 is deleted; the rest of the suite (234
tests) passes with the patch, plus the new TranslationRequestServiceTests for
behaviour 4 and the rate-limit retry tests for behaviour 5.

## Measured need

2026-09-13, production: 91 structured failures, 522 positions kept in English in
three hours across two free models (nemotron-3-super, nex-n2.5-pro). A 100-line
controlled job lost a whole 24-line batch this way while returning HTTP 200.

## Build

`.github/workflows/build-lingarr.yml`, on changes under `images/lingarr/**`.
Pin the printed digest in `2-k3s/08.servarr/kustomization.yaml`.

## Retire when

Upstream `Lingarr.Server/Services/Translation/LocalAiService.cs` no longer
falls back from `TranslateBatchWithStructuredOutput` to a schema-less request,
validates returned positions against the batch, and
`SubtitleTranslationService.RunBatch` throws instead of using original lines
(or an equivalent). Then drop this directory, the workflow, and the image pin,
and go back to `ghcr.io/lingarr-translate/lingarr:main`. Tracked in
`.github/workflows/upstream-release-watch.yml`.

Moving `UPSTREAM_COMMIT` means re-checking `git apply --check` and picking the
matching upstream image digest for `BASE_IMAGE` (its assembly
InformationalVersion is `0.0.0-dev+<commit>`).

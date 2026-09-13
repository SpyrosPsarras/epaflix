# Lingarr batch-contract build

Upstream `ghcr.io/lingarr-translate/lingarr:main` at commit `28f6a19` with one
patch, `batch-contract.patch`, republished as `ghcr.io/spyrospsarras/lingarr`.

## What the patch changes

Lingarr's `LocalAiService` chat batch path (used for cliproxy-free -> OpenRouter
free models) had three defects that together published untranslated subtitles
as translated:

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

Generate-API (non-chat) batches and per-line translation are unchanged. The
upstream test asserting behaviour 3 is deleted; the rest of the suite (234
tests) passes with the patch.

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

/**
 * @process specializations/devops-sre-platform/sonarr-manual-import
 * @description Import 12 locally-downloaded NRK episodes of "series og 290 – eit lite
 *   hotell i Hellas" (Sonarr seriesId=290, S01E01-E12) into the library. The .mkv files sit
 *   on this workstation (~/Downloads) named "...Hellas N. <title> - NRK TV.mkv". Sonarr runs
 *   in k3s and only sees the NFS export (/media = /mnt/pool1/dataset01). So: (1) verify the 12
 *   local files are complete, (2) rsync them (renamed to S01ENN) onto the NFS export at
 *   /mnt/pool1/dataset01/downloads/series 290-hellas (Sonarr sees /media/downloads/...),
 *   (3) drive Sonarr's ManualImport API with explicit episodeIds (move mode), (4) verify all
 *   12 episodes now have files. Transfer route + move mode were owner-approved.
 * @inputs { repoRoot, localDir, remoteHost, remoteDir, sonarrBase, apiKey, seriesId, importMode }
 * @outputs { success, transferred, imported, verified, summary }
 *
 * @agent general-purpose (Sonarr ManualImport API executor)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// N (filename episode number) -> Sonarr episodeId for series 290, season 1.
const EP_IDS = {
  1: 7575, 2: 7576, 3: 7577, 4: 7578, 5: 7579, 6: 7580,
  7: 7581, 8: 7582, 9: 7583, 10: 7584, 11: 7585, 12: 7586,
};

// ---------------------------------------------------------------------------
// PHASE 1 — verify the 12 local source files are present + complete (local-only).
// ---------------------------------------------------------------------------
const verifyLocalTask = defineTask('verify-local', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify 12 complete local source files in ~/Downloads',
  shell: {
    command:
      'set -e; D="' + args.localDir + '"; missing=""; partial=""; n=0; ' +
      'for i in $(seq 1 12); do ' +
      '  f=$(ls "$D"/*"Hellas ${i}. "*.mkv 2>/dev/null | head -1); ' +
      '  if [ -z "$f" ]; then missing="$missing $i"; continue; fi; ' +
      '  sz=$(stat -c%s "$f"); if [ "$sz" -lt 1000000 ]; then partial="$partial $i"; else n=$((n+1)); fi; ' +
      'done; ' +
      'p=$(ls "$D"/*Hellas*.part "$D"/*Hellas*.crdownload 2>/dev/null | head -1 || true); ' +
      'if [ -n "$p" ]; then partial="$partial $p"; fi; ' +
      'if [ -z "$missing" ] && [ -z "$partial" ] && [ "$n" -eq 12 ]; then ok=true; else ok=false; fi; ' +
      'printf \'{"allReady": %s, "completeCount": %s, "missing": "%s", "partial": "%s"}\' "$ok" "$n" "$missing" "$partial"',
    parseOutput: 'json',
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/result.json`,
  },
  labels: ['verify', 'local'],
}));

// ---------------------------------------------------------------------------
// PHASE 2 — rsync the 12 files (renamed to S01ENN) onto the NFS export (owner-approved SSH).
// ---------------------------------------------------------------------------
const transferTask = defineTask('transfer-nfs', (args, taskCtx) => ({
  kind: 'shell',
  title: 'rsync 12 files (renamed S01ENN) to TrueNAS NFS downloads staging dir',
  shell: {
    command:
      'set -e; D="' + args.localDir + '"; H="' + args.remoteHost + '"; R="' + args.remoteDir + '"; ' +
      'ssh -o BatchMode=yes -o ConnectTimeout=15 "$H" "mkdir -p \'$R\'"; ' +
      'sent=0; ' +
      'for i in $(seq 1 12); do ' +
      '  f=$(ls "$D"/*"Hellas ${i}. "*.mkv 2>/dev/null | head -1); ' +
      '  if [ -z "$f" ]; then echo "MISSING $i" >&2; exit 3; fi; ' +
      '  nn=$(printf "%02d" "$i"); ' +
      '  rsync -a --partial "$f" "$H:$R/series.og.290.S01E${nn}.mkv"; ' +
      '  sent=$((sent+1)); ' +
      'done; ' +
      // verify remote count + that none are zero-byte
      'remote=$(ssh -o BatchMode=yes "$H" "ls -1 \'$R\'/series.og.290.S01E*.mkv 2>/dev/null | wc -l"); ' +
      'zero=$(ssh -o BatchMode=yes "$H" "find \'$R\' -name \'series.og.290.S01E*.mkv\' -size 0 | wc -l"); ' +
      'if [ "$remote" -eq 12 ] && [ "$zero" -eq 0 ]; then ok=true; else ok=false; fi; ' +
      'printf \'{"transferOk": %s, "sent": %s, "remoteCount": %s, "remoteDir": "%s"}\' "$ok" "$sent" "$remote" "$R"',
    parseOutput: 'json',
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/result.json`,
  },
  labels: ['transfer', 'rsync', 'ssh'],
}));

// ---------------------------------------------------------------------------
// PHASE 3 — drive Sonarr ManualImport API with explicit episodeIds (move mode).
// ---------------------------------------------------------------------------
const importTask = defineTask('sonarr-import', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Sonarr ManualImport: map 12 files to S01E01-E12 and import (move)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Sonarr operator driving the v3 ManualImport API for series 290',
      task:
        'Import the 12 staged files at the Sonarr-visible folder /media/downloads/series 290-hellas ' +
        'into Sonarr seriesId=' + args.seriesId + ', mapping each file series.og.290.S01ENN.mkv to the ' +
        'episode whose episodeNumber == NN, using importMode=' + args.importMode + '. Then confirm the ' +
        'ManualImport command completed.',
      context: {
        sonarrBase: args.sonarrBase,
        apiKey: args.apiKey,
        seriesId: args.seriesId,
        importMode: args.importMode,
        sonarrVisibleFolder: '/media/downloads/series 290-hellas',
        episodeIdByNumber: EP_IDS,
      },
      instructions: [
        'All HTTP calls use header: X-Api-Key: <apiKey>. Base URL is sonarrBase.',
        'STEP 1 — GET candidates: curl -sG "<base>/api/v3/manualimport" --data-urlencode "folder=/media/downloads/series 290-hellas" --data-urlencode "seriesId=290" --data-urlencode "filterExistingFiles=false" -H "X-Api-Key: <key>". This returns one object per file with fields: path, name/relativePath, quality, languages, releaseGroup, episodes, rejections.',
        'STEP 2 — Build the import payload. For EACH returned file: parse the S01ENN token from its path/name to get NN; look up episodeIdByNumber[NN] (drop the leading zero, e.g. E07 -> 7). Construct a file entry: { path: <the file path from the GET response>, seriesId: 290, episodeIds: [<that one episodeId>], quality: <the quality object from the GET response verbatim>, languages: <the languages array from the GET response verbatim>, releaseGroup: <from GET or ""> }. You MUST cover all 12 files (E01..E12). If the GET returned fewer than 12, report which are missing and stop.',
        'STEP 3 — POST the command: curl -s -X POST "<base>/api/v3/command" -H "X-Api-Key: <key>" -H "Content-Type: application/json" -d \'{ "name": "ManualImport", "importMode": "<importMode>", "files": [ ...the 12 file entries... ] }\'. Capture the returned command id.',
        'STEP 4 — Poll GET "<base>/api/v3/command/<id>" -H "X-Api-Key: <key>" every few seconds until status is "completed" or "failed" (max ~90s). Record final status and any errors.',
        'Do NOT use move mode in a way that deletes source if import fails; just report. Do not touch any other series.',
        'Return ONLY the structured JSON result with the exact fields below. importedCount = number of files Sonarr accepted (episodes mapped).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['candidatesFound', 'filesSubmitted', 'commandId', 'commandStatus', 'importedCount', 'errors'],
      properties: {
        candidatesFound: { type: 'number' },
        filesSubmitted: { type: 'number' },
        commandId: { type: 'number' },
        commandStatus: { type: 'string' },
        importedCount: { type: 'number' },
        errors: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// PHASE 4 — verify all 12 episodes now have files (source of truth = Sonarr API).
// ---------------------------------------------------------------------------
const verifyImportTask = defineTask('verify-import', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify Sonarr series 290 has files on all 12 episodes',
  shell: {
    command:
      'set -e; B="' + args.sonarrBase + '"; K="' + args.apiKey + '"; ' +
      'json=$(curl -sf "$B/api/v3/episode?seriesId=' + args.seriesId + '" -H "X-Api-Key: $K"); ' +
      'withFile=$(echo "$json" | jq "[.[]|select(.hasFile==true)]|length"); ' +
      'total=$(echo "$json" | jq "length"); ' +
      'missing=$(echo "$json" | jq -c "[.[]|select(.hasFile==false)|.episodeNumber]"); ' +
      'if [ "$withFile" -eq 12 ]; then ok=true; else ok=false; fi; ' +
      'printf \'{"allImported": %s, "withFile": %s, "total": %s, "stillMissing": %s}\' "$ok" "$withFile" "$total" "$missing"',
    parseOutput: 'json',
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/result.json`,
  },
  labels: ['verify', 'sonarr'],
}));

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    localDir: '/home/spy/Downloads',
    remoteHost: 'truenas_admin@192.168.10.200',
    remoteDir: '/mnt/pool1/dataset01/downloads/series 290-hellas',
    sonarrBase: 'https://sonarr.epaflix.com',
    apiKey: '<SONARR_API_KEY>',
    seriesId: 290,
    importMode: 'move',
    ...inputs,
  };

  ctx.log('info', 'Import 12 NRK episodes -> Sonarr series 290 (verify -> transfer -> import -> verify)');

  // PHASE 1 — local readiness gate.
  const local = await ctx.task(verifyLocalTask, { localDir: cfg.localDir });
  ctx.log('info', `Local: complete=${local.completeCount}/12 allReady=${local.allReady} missing="${local.missing}" partial="${local.partial}"`);
  if (!local.allReady) {
    return { success: false, transferred: false, imported: false, verified: false,
      summary: `Aborted: local files not all ready (complete=${local.completeCount}/12, missing="${local.missing}", partial="${local.partial}").` };
  }

  // PHASE 2 — transfer to NFS (owner-approved rsync over SSH to TrueNAS).
  const xfer = await ctx.task(transferTask, {
    localDir: cfg.localDir, remoteHost: cfg.remoteHost, remoteDir: cfg.remoteDir,
  });
  ctx.log('info', `Transfer: ok=${xfer.transferOk} sent=${xfer.sent} remoteCount=${xfer.remoteCount}`);
  if (!xfer.transferOk) {
    return { success: false, transferred: false, imported: false, verified: false,
      summary: `Aborted: transfer incomplete (sent=${xfer.sent}, remoteCount=${xfer.remoteCount}/12).` };
  }

  // PHASE 3 — Sonarr ManualImport (move) with explicit episodeIds.
  const imp = await ctx.task(importTask, {
    sonarrBase: cfg.sonarrBase, apiKey: cfg.apiKey, seriesId: cfg.seriesId, importMode: cfg.importMode,
  });
  ctx.log('info', `Import: cmd=${imp.commandId} status=${imp.commandStatus} submitted=${imp.filesSubmitted} imported=${imp.importedCount}`);

  // PHASE 4 — verify against Sonarr (source of truth), independent of import command self-report.
  const ver = await ctx.task(verifyImportTask, {
    sonarrBase: cfg.sonarrBase, apiKey: cfg.apiKey, seriesId: cfg.seriesId,
  });
  ctx.log('info', `Verify: allImported=${ver.allImported} withFile=${ver.withFile}/12 stillMissing=${ver.stillMissing}`);

  return {
    success: ver.allImported === true,
    transferred: xfer.transferOk === true,
    imported: imp.importedCount >= 1,
    verified: ver.allImported === true,
    commandStatus: imp.commandStatus,
    withFile: ver.withFile,
    stillMissing: ver.stillMissing,
    summary: ver.allImported
      ? `All 12 episodes of series 290 now have files in Sonarr (transferred 12, ManualImport=${imp.commandStatus}).`
      : `Incomplete: ${ver.withFile}/12 episodes have files; stillMissing=${ver.stillMissing}. ManualImport cmd status=${imp.commandStatus}, errors=${JSON.stringify(imp.errors)}.`,
  };
}

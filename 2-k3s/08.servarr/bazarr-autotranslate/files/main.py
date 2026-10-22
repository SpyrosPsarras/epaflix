"""Bazarr -> Lingarr glue with serial, shortest-first translation.

New items get SEARCH_ROUNDS_BEFORE_TRANSLATE provider search rounds spaced
ROUND_SPACING_HOURS apart. Failed search requests do not count. Ready items
translate one at a time, smallest source subtitle first, while the worker
waits for Bazarr's translation jobs to end. Still-wanted items retry after
TRANSLATE_GRACE_HOURS from attempt end without new search rounds. A failed
attempt pauses all sends for FAILURE_PAUSE_MINUTES.
Bazarr's own six-hourly wanted search keeps running.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import List

import httpx
from dotenv import load_dotenv

from class_types import Movie, Serie


def get_env_or_default(env, default):
    val = os.getenv(env)
    return val if val is not None else default


def env_flag(env, default):
    return str(get_env_or_default(env, default)).strip().lower() in ("1", "true", "yes", "on")


load_dotenv()
base_url = os.getenv("BAZARR_BASE_URL")
api_key = os.getenv("BAZARR_API_KEY")
base_languages = [lang.strip() for lang in os.getenv("BASE_LANGUAGES", "").split(",") if lang.strip()]
to_languages = [lang.strip() for lang in os.getenv("TO_LANGUAGES", "").split(",") if lang.strip()]

JOB_POLL_SECONDS = 30
translation_job_timeout_seconds = float(get_env_or_default("TRANSLATION_JOB_TIMEOUT_HOURS", 4)) * 3600
failure_pause_seconds = float(get_env_or_default("FAILURE_PAUSE_MINUTES", 60)) * 60
http_timeout = httpx.Timeout(float(get_env_or_default("REQUEST_TIMEOUT", 300)))
search_rounds = int(get_env_or_default("SEARCH_ROUNDS_BEFORE_TRANSLATE", 3))
round_spacing_seconds = float(get_env_or_default("ROUND_SPACING_HOURS", 8)) * 3600
translate_grace_seconds = float(get_env_or_default("TRANSLATE_GRACE_HOURS", 24)) * 3600
interval_between_scans = int(get_env_or_default("INTERVAL_BETWEEN_SCANS", 5 * 60))
log_level = get_env_or_default("LOG_LEVEL", "INFO")
log_directory = get_env_or_default("LOG_DIRECTORY", "logs/")
series_scan = env_flag("SERIES_SCAN", True)
movies_scan = env_flag("MOVIES_SCAN", True)
state_path = os.path.join(log_directory, "state.json")

logger = logging.getLogger("bazarr_lingarr")
shutdown_event = asyncio.Event()


class Candidate:
    """A base-language subtitle that could be translated to a missing language."""

    def __init__(self, base_subtitle, to_language, video_id, is_serie, series_id, hi, forced):
        self.base_subtitle = base_subtitle
        self.to_language = to_language
        self.video_id = video_id
        self.is_serie = is_serie
        self.series_id = series_id
        self.hi = hi
        self.forced = forced


def item_key(is_serie, video_id, to_language):
    return f"{'s' if is_serie else 'm'}{video_id}:{to_language}"


def load_state():
    try:
        with open(state_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, state_path)


state = load_state()


async def get_wanted_episodes(base_url, api_key, start: int = 0, length: int = -1):
    logger.debug("Getting wanted episodes")
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.get(f"{base_url}/api/episodes/wanted",
                                        headers={"X-API-KEY": api_key},
                                        params={"start": start, "length": length})
            response.raise_for_status()
            return [Serie.from_dict(obj) for obj in response.json()["data"]]
    except Exception as e:
        logger.error(f"Error while getting wanted episodes: {type(e).__name__}: {e}")


async def get_wanted_movies(base_url, api_key, start: int = 0, length: int = -1):
    logger.debug("Getting wanted movies")
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.get(f"{base_url}/api/movies/wanted",
                                        headers={"X-API-KEY": api_key},
                                        params={"start": start, "length": length})
            response.raise_for_status()
            return [Movie.from_dict(obj) for obj in response.json()["data"]]
    except Exception as e:
        logger.error(f"Error while getting wanted movies: {type(e).__name__}: {e}")


async def get_episodes_metadata(base_url, api_key, episode_ids: List[int]):
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.get(f"{base_url}/api/episodes",
                                        headers={"X-API-KEY": api_key},
                                        params={"episodeid[]": episode_ids})
            response.raise_for_status()
            return [Serie.from_dict(obj) for obj in response.json()["data"]]
    except Exception as e:
        logger.error(f"Error while getting episodes metadata: {type(e).__name__}: {e}")


async def get_movies_metadata(base_url, api_key, movie_ids: List[int]):
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.get(f"{base_url}/api/movies",
                                        headers={"X-API-KEY": api_key},
                                        params={"radarrid[]": movie_ids})
            response.raise_for_status()
            return [Movie.from_dict(obj) for obj in response.json()["data"]]
    except Exception as e:
        logger.error(f"Error while getting movies metadata: {type(e).__name__}: {e}")


async def find_translatable(base_url, api_key, videos: List) -> List[Candidate]:
    is_serie = isinstance(videos[0], Serie)
    missing_map = {}
    for video in videos:
        video_id = video.sonarr_episode_id if is_serie else video.radarr_id
        for missing_sub in video.missing_subtitles:
            if missing_sub.code2 in to_languages:
                missing_map[video_id] = (video, missing_sub)

    if not missing_map:
        return []

    if is_serie:
        metadata = await get_episodes_metadata(base_url, api_key, episode_ids=list(missing_map))
    else:
        metadata = await get_movies_metadata(base_url, api_key, movie_ids=list(missing_map))
    if metadata is None:
        logger.info("No metadata returned, couldn't find already existing subtitles")
        return []

    metadata_by_id = {(m.sonarr_episode_id if is_serie else m.radarr_id): m for m in metadata}

    candidates = []
    for video_id, (video, missing_sub) in missing_map.items():
        meta = metadata_by_id.get(video_id)
        if meta is None or not meta.subtitles:
            continue
        for sub in meta.subtitles:
            if sub.code2 == missing_sub.code2:
                continue
            if sub.code2 in base_languages:
                candidates.append(Candidate(
                    base_subtitle=sub,
                    to_language=missing_sub.code2,
                    video_id=video_id,
                    is_serie=is_serie,
                    series_id=video.sonarr_series_id if is_serie else None,
                    hi=missing_sub.hi,
                    forced=missing_sub.forced,
                ))
                break
    return candidates


async def request_search(client: httpx.AsyncClient, item: Candidate) -> bool:
    """Trigger Bazarr's provider search + best-match download for one item."""
    params = {
        "language": item.to_language,
        "hi": "True" if item.hi else "False",
        "forced": "True" if item.forced else "False",
    }
    if item.is_serie:
        endpoint = f"{base_url}/api/episodes/subtitles"
        params.update({"seriesid": item.series_id, "episodeid": item.video_id})
    else:
        endpoint = f"{base_url}/api/movies/subtitles"
        params.update({"radarrid": item.video_id})
    try:
        response = await client.patch(endpoint, headers={"X-API-KEY": api_key}, params=params)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[search] PATCH {endpoint} failed for id {item.video_id}: {type(e).__name__}: {e}")
        return False


async def process(client: httpx.AsyncClient, videos: List, kind: str):
    is_serie = kind == "episodes"

    kind_prefix = "s" if is_serie else "m"
    wanted_keys = set()
    for video in videos:
        video_id = video.sonarr_episode_id if is_serie else video.radarr_id
        for missing_sub in video.missing_subtitles:
            if missing_sub.code2 in to_languages:
                wanted_keys.add(item_key(is_serie, video_id, missing_sub.code2))
    for key in [k for k in state if k.startswith(kind_prefix) and k not in wanted_keys]:
        state.pop(key)

    candidates = await find_translatable(base_url, api_key, videos) if videos else []
    now = time.time()
    searched = 0

    for cand in candidates:
        key = item_key(cand.is_serie, cand.video_id, cand.to_language)
        entry = state.get(key) or {"phase": "search", "rounds": 0, "ts": 0}

        if entry["phase"] == "translate":
            if now - entry["ts"] < translate_grace_seconds:
                continue
            logger.info(f"[translate] {kind} id {cand.video_id} still wanted after grace period, ready for translation")
            entry = {"phase": "ready", "rounds": entry["rounds"], "ts": now}

        if entry["phase"] == "search" and entry["rounds"] < search_rounds:
            if now - entry["ts"] < round_spacing_seconds:
                continue
            if await request_search(client, cand):
                entry["rounds"] += 1
                entry["ts"] = now
                searched += 1
                logger.info(f"[search] {kind} id {cand.video_id} for '{cand.base_subtitle.path}' -> {cand.to_language} (round {entry['rounds']}/{search_rounds})")
            else:
                logger.info(f"[search] {kind} id {cand.video_id} not counted, will re-search next scan")
        else:
            entry = {
                "phase": "ready", "rounds": entry["rounds"], "ts": now,
                "size": cand.base_subtitle.file_size,
                "job": {
                    "language": cand.to_language,
                    "path": cand.base_subtitle.path,
                    "type": "episode" if cand.is_serie else "movie",
                    "id": cand.video_id,
                    "forced": cand.base_subtitle.forced,
                    "hi": cand.base_subtitle.hi,
                    "original_format": True,
                },
            }

        state[key] = entry

    save_state(state)
    ready_count = sum(entry["phase"] == "ready" for entry in state.values())
    logger.info(f"[{kind}] search requests: {searched}, ready for translation: {ready_count}")


async def scan_and_process_series(base_url, api_key):
    logger.info("Scanning for episodes")
    series = await get_wanted_episodes(base_url, api_key)
    if series is None:
        return
    logger.info(f"Found {len(series)} wanted episodes")
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        await process(client, series, "episodes")


async def scan_and_process_movies(base_url, api_key):
    logger.info("Scanning for movies")
    movies = await get_wanted_movies(base_url, api_key)
    if movies is None:
        return
    logger.info(f"Found {len(movies)} wanted movies")
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        await process(client, movies, "movies")


async def get_idle_translation_jobs(client, base_url, api_key):
    try:
        response = await client.get(f"{base_url}/api/system/jobs", headers={"X-API-KEY": api_key})
        response.raise_for_status()
        jobs = response.json()["data"]
        translations = [job for job in jobs if "translat" in job["job_name"].lower()]
        if any(job["status"] in ("pending", "running") for job in translations):
            return None
        return translations, max((job["job_id"] for job in jobs), default=0)
    except Exception as e:
        logger.error(f"[translate] Getting jobs failed: {type(e).__name__}: {e}")
        return None


async def translation_worker(base_url, api_key):
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        while not shutdown_event.is_set():
            key = None
            try:
                ready = [(key, entry) for key, entry in state.items() if entry["phase"] == "ready"]
                if not ready:
                    await asyncio.sleep(JOB_POLL_SECONDS)
                    continue
                key, entry = min(ready, key=lambda item: item[1]["size"])
                snapshot = await get_idle_translation_jobs(client, base_url, api_key)
                if snapshot is None:
                    await asyncio.sleep(JOB_POLL_SECONDS)
                    continue
                jobs, highest_job_id = snapshot
                if key not in state or state[key]["phase"] != "ready":
                    continue

                entry = state[key]
                params = {"action": "translate", **entry["job"]}
                size = entry["size"]
                started = time.monotonic()
                result = "failed"
                logger.info(f"[translate] Sending {key} path='{params['path']}' size={size}")
                try:
                    response = await client.patch(f"{base_url}/api/subtitles",
                                                  headers={"X-API-KEY": api_key}, params=params)
                    response.raise_for_status()
                    while time.monotonic() - started < translation_job_timeout_seconds:
                        await asyncio.sleep(JOB_POLL_SECONDS)
                        snapshot = await get_idle_translation_jobs(client, base_url, api_key)
                        if time.monotonic() - started >= translation_job_timeout_seconds:
                            break
                        if snapshot is None:
                            continue
                        jobs, _ = snapshot
                        new_jobs = [job for job in jobs if job["job_id"] > highest_job_id]
                        if new_jobs and max(new_jobs, key=lambda job: job["job_id"])["status"] == "completed":
                            result = "completed"
                        break
                except Exception as e:
                    logger.error(f"[translate] {key} request failed: {type(e).__name__}: {e}")

                if key in state:
                    state[key] = {"phase": "translate", "rounds": entry["rounds"],
                                  "ts": time.time(), "result": result}
                    save_state(state)
                minutes = (time.monotonic() - started) / 60
                logger.info(f"[translate] {key} path='{params['path']}' size={size} result={result} minutes={minutes:.1f}")
                if result == "failed":
                    logger.info(f"[translate] Pausing sends for {failure_pause_seconds / 60:g} minutes after failure")
                    await asyncio.sleep(failure_pause_seconds)
            except Exception as e:
                logger.error(f"[translate] {key or 'worker'} iteration failed: {type(e).__name__}: {e}")
                await asyncio.sleep(JOB_POLL_SECONDS)


async def main(base_url, api_key):
    worker = asyncio.create_task(translation_worker(base_url, api_key))
    try:
        while not shutdown_event.is_set():
            try:
                if series_scan:
                    await scan_and_process_series(base_url, api_key)
                if movies_scan:
                    await scan_and_process_movies(base_url, api_key)
            except Exception as e:
                logger.error(f"Uncaught exception: {type(e).__name__}: {e}")

            await asyncio.sleep(interval_between_scans)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def handle_shutdown():
    logger.info("Received exit signal")
    sys.exit(1)


if __name__ == "__main__":
    if base_url is None:
        print("BAZARR_BASE_URL is missing")
        sys.exit(1)

    if api_key is None:
        print("BAZARR_API_KEY is missing")
        sys.exit(1)

    if len(base_languages) == 0:
        print("Missing BASE_LANGUAGES")
        sys.exit(1)

    wrong_languages = [lang for lang in base_languages if len(lang) != 2]
    if wrong_languages:
        print(f"Wrong languages given in BASE_LANGUAGES, wrong ones: {wrong_languages}, expected to be 2 characters long (code2)")
        sys.exit(1)

    if len(to_languages) == 0:
        print("Missing TO_LANGUAGES")
        sys.exit(1)

    wrong_languages = [lang for lang in to_languages if len(lang) != 2]
    if wrong_languages:
        print(f"Wrong languages given in TO_LANGUAGES, wrong ones: {wrong_languages}, expected to be 2 characters long (code2)")
        sys.exit(1)

    if not series_scan and not movies_scan:
        print("Both series and movies scan are disabled, nothing will be done")
        sys.exit(1)

    logger.propagate = False
    if not log_directory.endswith("/"):
        log_directory += "/"
    os.makedirs(log_directory, exist_ok=True)
    handler = TimedRotatingFileHandler(
        f"{log_directory}bazarr_lingarr_autotranslate.log", when="midnight", interval=1, backupCount=4
    )
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    match log_level.lower():
        case "debug":
            logger.setLevel(logging.DEBUG)
            logger.debug("Configuration: --------------------")
            logger.debug(f"base_languages: {base_languages}")
            logger.debug(f"to_languages: {to_languages}")
            logger.debug(f"search_rounds: {search_rounds}")
            logger.debug(f"round_spacing_hours: {round_spacing_seconds / 3600}")
            logger.debug(f"translate_grace_hours: {translate_grace_seconds / 3600}")
            logger.debug(f"translation_job_timeout_hours: {translation_job_timeout_seconds / 3600}")
            logger.debug(f"failure_pause_minutes: {failure_pause_seconds / 60}")
            logger.debug(f"http_timeout: {http_timeout}")
            logger.debug(f"interval_between_scans: {interval_between_scans}")
            logger.debug(f"state_path: {state_path}")
            logger.debug("End Configuration: ----------------")
        case "error":
            logger.setLevel(logging.ERROR)
        case _:
            logger.setLevel(logging.INFO)

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_shutdown)

    loop.run_until_complete(main(base_url, api_key))

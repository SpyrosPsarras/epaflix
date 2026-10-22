import asyncio
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class Serie:
    def __init__(self, **values):
        self.__dict__.update(values)

    @classmethod
    def from_dict(cls, values):
        return cls(**values)


class Movie:
    def __init__(self, **values):
        self.__dict__.update(values)

    @classmethod
    def from_dict(cls, values):
        return cls(**values)


class Response:
    def __init__(self, jobs=(), status=200):
        self.jobs = jobs
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return {"data": self.jobs}


class Client:
    def __init__(self, jobs=(), patch_status=200):
        self.jobs = list(jobs) or [[]]
        self.patch_status = patch_status
        self.gets = 0
        self.patches = []
        self.on_get = None
        self.on_patch = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, headers, **kwargs):
        assert url == "http://bazarr.invalid/api/system/jobs", url
        assert headers == {"X-API-KEY": "fake"}, headers
        self.gets += 1
        assert self.gets < 50, "Worker did not stop polling"
        if self.on_get:
            self.on_get(self)
        value = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        if isinstance(value, Exception):
            raise value
        return Response(value)

    async def patch(self, url, *, headers, params):
        self.patches.append((url, dict(params), self.gets))
        if self.on_patch:
            self.on_patch(self)
        return Response(status=self.patch_status)


def no_network(*args, **kwargs):
    raise AssertionError("A test tried to create a real HTTP client")


httpx = types.ModuleType("httpx")
httpx.Timeout = lambda value: value
httpx.AsyncClient = no_network
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda: None
class_types = types.ModuleType("class_types")
class_types.Serie = Serie
class_types.Movie = Movie
sys.modules.update(httpx=httpx, dotenv=dotenv, class_types=class_types)
TEMP = tempfile.TemporaryDirectory()
os.environ.update(
    BAZARR_BASE_URL="http://bazarr.invalid", BAZARR_API_KEY="fake",
    BASE_LANGUAGES="en,no", TO_LANGUAGES="el", LOG_DIRECTORY=TEMP.name,
    TRANSLATE_GRACE_HOURS="24", FAILURE_PAUSE_MINUTES="60",
    TRANSLATION_JOB_TIMEOUT_HOURS="4",
)
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "bazarr_autotranslate", ROOT / "2-k3s/08.servarr/bazarr-autotranslate/files/main.py"
)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
app.logger.disabled = True


def job(job_id, status, name="Translating from EN to EL using Lingarr"):
    if status in ("completed", "failed") and "Translating" in name:
        name = "Translated from EN to EL using Lingarr"
    return {
        "job_id": job_id, "job_name": name, "status": status,
        "last_run_time": None, "progress_message": "",
    }


def ready(size=100, video_id=1):
    return {
        "phase": "ready", "rounds": 3, "ts": 0, "size": size,
        "job": {
            "language": "el", "path": f"/media/{video_id}.en.srt",
            "type": "episode", "id": video_id, "forced": False,
            "hi": True, "original_format": True,
        },
    }


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state = {}
        app.save_state(app.state)
        app.shutdown_event = asyncio.Event()
        self.now = 1_000_000.0
        self.elapsed = 0.0
        self.sleeps = []
        self.real_sleep = asyncio.sleep
        self.time_patch = patch.object(app, "time", types.SimpleNamespace(
            time=lambda: self.now + self.elapsed,
            monotonic=lambda: self.elapsed,
        ))
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.sleep_patch = patch.object(app.asyncio, "sleep", self.sleep)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        app.translation_job_timeout_seconds = 90
        app.failure_pause_seconds = 3600

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        await self.real_sleep(0)

    def seed(self, entries):
        app.state.update(entries)
        app.save_state(app.state)

    async def scan(self, entry=None, size=321, is_serie=True):
        if entry is not None:
            self.seed({"s1:el" if is_serie else "m1:el": entry})
        missing = types.SimpleNamespace(code2="el", hi=False, forced=True)
        sub = types.SimpleNamespace(code2="en", path="/media/base.en.srt",
                                    forced=False, hi=True, file_size=size)
        if is_serie:
            video = Serie(sonarr_episode_id=1, sonarr_series_id=9, missing_subtitles=[missing])
            metadata_name = "get_episodes_metadata"
        else:
            video = Movie(radarr_id=1, missing_subtitles=[missing])
            metadata_name = "get_movies_metadata"
        metadata = type(video)(**video.__dict__, subtitles=[sub])

        async def get_metadata(*args, **kwargs):
            return [metadata]

        client = Client()
        with patch.object(app, metadata_name, get_metadata):
            await app.process(client, [video], "episodes" if is_serie else "movies")
        return client

    async def worker(self, client):
        self.assertTrue(inspect.iscoroutinefunction(app.translation_worker),
                        "The translation worker must run in the scanner's event loop")
        save = app.save_state

        def save_and_stop(state):
            save(state)
            if any(entry.get("result") in ("completed", "failed") for entry in state.values()):
                app.shutdown_event.set()

        with patch.object(httpx, "AsyncClient", lambda **kwargs: client), \
                patch.object(app, "save_state", save_and_stop):
            await app.translation_worker(app.base_url, app.api_key)

    async def test_retry_after_grace_goes_straight_to_ready(self):
        for result in ("completed", "failed", None):
            with self.subTest(result=result):
                app.state.clear()
                entry = {"phase": "translate", "rounds": 0, "ts": self.now - 86400}
                if result:
                    entry["result"] = result
                client = await self.scan(entry)
                self.assertEqual(app.state["s1:el"]["phase"], "ready")
                self.assertEqual(app.state["s1:el"]["size"], 321)
                self.assertEqual(client.patches, [])

    async def test_translation_within_grace_is_untouched(self):
        entry = {"phase": "translate", "rounds": 3, "ts": self.now - 86399, "result": "failed"}
        client = await self.scan(entry)
        self.assertEqual(app.state["s1:el"], entry)
        self.assertEqual(client.patches, [])

    async def test_completed_search_rounds_build_ready_job(self):
        for is_serie, key, kind in ((True, "s1:el", "episode"), (False, "m1:el", "movie")):
            with self.subTest(kind=kind):
                client = await self.scan({"phase": "search", "rounds": 3, "ts": self.now}, is_serie=is_serie)
                self.assertEqual(client.patches, [])
                self.assertEqual(app.state[key], {
                    "phase": "ready", "rounds": 3, "ts": self.now, "size": 321,
                    "job": {"language": "el", "path": "/media/base.en.srt", "type": kind,
                            "id": 1, "forced": False, "hi": True, "original_format": True},
                })

    async def test_ready_entry_refreshes_subtitle_details(self):
        client = await self.scan(ready(size=900), size=123)
        self.assertEqual(app.state["s1:el"]["size"], 123)
        self.assertEqual(app.state["s1:el"]["job"]["path"], "/media/base.en.srt")
        self.assertEqual(client.patches, [])

    async def test_new_items_search_first_with_spacing_and_failed_search_not_counted(self):
        client = await self.scan()
        self.assertEqual(len(client.patches), 1)
        self.assertEqual(client.patches[0][0], "http://bazarr.invalid/api/episodes/subtitles")
        self.assertEqual(app.state["s1:el"], {"phase": "search", "rounds": 1, "ts": self.now})
        client = await self.scan()
        self.assertEqual(client.patches, [])
        self.elapsed += 8 * 3600
        with patch.object(Client, "patch", return_value=Response(status=500)):
            await self.scan()
        self.assertEqual(app.state["s1:el"]["rounds"], 1)
        await self.scan()
        self.assertEqual(app.state["s1:el"]["rounds"], 2)

    async def test_smallest_ready_is_picked_and_other_phases_are_ignored(self):
        self.seed({
            "s1:el": ready(500, 1), "s2:el": ready(50, 2),
            "s3:el": {"phase": "search", "rounds": 0, "ts": 0, "size": 1},
            "s4:el": {"phase": "translate", "rounds": 3, "ts": self.now, "size": 0},
        })
        client = Client([[], [job(1, "completed")]])
        await self.worker(client)
        self.assertEqual(client.patches, [("http://bazarr.invalid/api/subtitles", {
            "action": "translate", "language": "el", "path": "/media/2.en.srt",
            "type": "episode", "id": 2, "forced": False, "hi": True,
            "original_format": True,
        }, 1)])
        self.assertEqual(app.state["s1:el"]["phase"], "ready")
        self.assertEqual(app.state["s3:el"]["phase"], "search")
        self.assertEqual(app.state["s4:el"]["phase"], "translate")

    async def test_job_outcomes_and_failure_pause(self):
        old = job(9, "completed")
        cases = (
            ("completed", [[old], [old, job(10, "pending")], [old, job(10, "completed")]], 200, "completed"),
            ("failed", [[old], [old, job(10, "failed")]], 200, "failed"),
            ("missing", [[old], [old]], 200, "failed"),
            ("restart", [[old], []], 200, "failed"),
            ("unrelated", [[old, job(15, "completed", "Search")], [old, job(15, "completed", "Search")]], 200, "failed"),
            ("non-2xx", [[old]], 500, "failed"),
            ("timeout", [[old], [job(10, "running")]], 200, "failed"),
            ("newest", [[old], [job(10, "completed"), job(11, "failed")]], 200, "failed"),
        )
        for label, responses, status, result in cases:
            with self.subTest(case=label):
                app.state.clear()
                app.shutdown_event.clear()
                self.elapsed = 0
                self.sleeps.clear()
                self.seed({"s1:el": ready()})
                client = Client(responses, patch_status=status)
                await self.worker(client)
                entry = app.state["s1:el"]
                self.assertEqual(entry["phase"], "translate")
                self.assertEqual(entry["result"], result)
                self.assertEqual(entry["rounds"], 3)
                end_elapsed = self.elapsed - (3600 if result == "failed" else 0)
                self.assertEqual(entry["ts"], self.now + end_elapsed)
                self.assertEqual(json.loads(Path(app.state_path).read_text())["s1:el"], entry)
                self.assertEqual(3600 in self.sleeps, result == "failed")
                if label == "non-2xx":
                    self.assertEqual(client.gets, 1)
                if label == "timeout":
                    self.assertGreaterEqual(end_elapsed, 90)

    async def test_worker_waits_for_each_outcome_and_pause_before_next_send(self):
        for status, expected_delay in (("completed", 30), ("failed", 3630)):
            with self.subTest(status=status):
                app.state.clear()
                app.shutdown_event.clear()
                self.elapsed = 0
                self.seed({"s1:el": ready(100, 1), "s2:el": ready(200, 2)})
                client = Client([
                    [], [job(1, status)], [job(1, status)],
                    [job(1, status), job(2, "completed")],
                ])
                sends = []

                def record_send(client):
                    sends.append(self.elapsed)
                    if len(sends) == 2:
                        self.assertEqual(app.state["s1:el"]["result"], status)

                def stop_after_second_job(client):
                    if client.gets == 4:
                        app.shutdown_event.set()

                client.on_patch = record_send
                client.on_get = stop_after_second_job
                with patch.object(httpx, "AsyncClient", lambda **kwargs: client):
                    await app.translation_worker(app.base_url, app.api_key)
                self.assertEqual(sends, [0, expected_delay])
                self.assertEqual(app.state["s2:el"]["result"], "completed")

    async def test_state_save_error_is_logged_and_worker_continues(self):
        self.seed({"s1:el": ready(100, 1), "s2:el": ready(200, 2)})
        client = Client([
            [], [job(1, "completed")], [job(1, "completed")],
            [job(1, "completed"), job(2, "completed")],
        ])
        save = app.save_state
        saves = 0

        def fail_once(state):
            nonlocal saves
            saves += 1
            if saves == 1:
                raise OSError("volume full")
            save(state)
            app.shutdown_event.set()

        with patch.object(httpx, "AsyncClient", lambda **kwargs: client), \
                patch.object(app, "save_state", fail_once), \
                patch.object(app.logger, "disabled", False), \
                self.assertLogs(app.logger, level="ERROR") as logs:
            await app.translation_worker(app.base_url, app.api_key)
        self.assertEqual([params["id"] for _, params, _ in client.patches], [1, 2])
        self.assertEqual(self.sleeps, [30, 30, 30])
        self.assertEqual(app.state["s2:el"]["result"], "completed")
        self.assertEqual(json.loads(Path(app.state_path).read_text()), app.state)
        self.assertEqual(len(logs.output), 1)
        for message in ("s1:el", "OSError", "volume full"):
            self.assertIn(message, logs.output[0])

    async def test_refreshed_ready_job_is_used_after_jobs_request(self):
        self.seed({"s1:el": ready()})
        client = Client([[], [job(1, "completed")]])

        def refresh(client):
            if client.gets == 1:
                app.state["s1:el"] = ready(45, 8)

        client.on_get = refresh
        await self.worker(client)
        self.assertEqual(client.patches[0][1]["path"], "/media/8.en.srt")
        self.assertEqual(client.patches[0][1]["id"], 8)

    async def test_key_pruned_before_send_is_not_sent(self):
        self.seed({"s1:el": ready()})
        client = Client()

        def prune(client):
            app.state.clear()
            app.shutdown_event.set()

        client.on_get = prune
        await self.worker(client)
        self.assertEqual(client.patches, [])
        self.assertEqual(app.state, {})

    async def test_existing_translation_blocks_patch(self):
        self.seed({"s1:el": ready()})
        client = Client([
            [job(4, "running")], [job(4, "pending")], [job(4, "completed")],
            [job(4, "completed"), job(5, "completed")],
        ])
        await self.worker(client)
        self.assertEqual(client.patches[0][2], 3)
        self.assertEqual(app.state["s1:el"]["result"], "completed")

    async def test_jobs_get_failure_keeps_polling_and_counts_toward_timeout(self):
        self.seed({"s1:el": ready()})
        client = Client([RuntimeError("unavailable"), [], RuntimeError("unavailable"), [job(1, "completed")]])
        await self.worker(client)
        self.assertEqual(client.patches[0][2], 2)
        self.assertEqual(app.state["s1:el"]["result"], "completed")
        self.assertEqual(self.sleeps, [30, 30, 30])
        app.state.clear()
        app.shutdown_event.clear()
        self.seed({"s1:el": ready()})
        client = Client([[], RuntimeError("unavailable")])
        await self.worker(client)
        self.assertEqual(app.state["s1:el"]["result"], "failed")

    async def test_pruned_key_is_not_readded_after_attempt(self):
        self.seed({"s1:el": ready()})
        client = Client([[], [job(1, "completed")]])

        def prune(client):
            if client.gets == 2:
                app.state.clear()
                app.save_state(app.state)
                app.shutdown_event.set()

        client.on_get = prune
        await self.worker(client)
        self.assertEqual(app.state, {})
        self.assertEqual(json.loads(Path(app.state_path).read_text()), {})

    async def test_empty_wanted_list_prunes_only_that_kind(self):
        self.seed({"s1:el": ready(), "m1:el": ready()})
        await app.process(Client(), [], "episodes")
        self.assertEqual(set(app.state), {"m1:el"})

    async def test_idle_worker_sleeps_without_sending(self):
        client = Client()

        async def stop_after_sleep(seconds):
            await self.sleep(seconds)
            app.shutdown_event.set()

        with patch.object(app.asyncio, "sleep", stop_after_sleep):
            await self.worker(client)
        self.assertEqual(client.patches, [])
        self.assertEqual(client.gets, 0)
        self.assertEqual(len(self.sleeps), 1)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        TEMP.cleanup()

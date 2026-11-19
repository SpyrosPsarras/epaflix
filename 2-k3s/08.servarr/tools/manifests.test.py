#!/usr/bin/env python3
import unittest
from pathlib import Path

import yaml

SERVARR = Path(__file__).resolve().parents[1]
DTS_PROFILES = {
    "radarr": ("UHD Bluray + WEB", {
        "DTS": "1c1a4c5e823891c75bc50380a6866f73",
        "DTS-ES": "f9f847ac70a0af62ea4a08280b859636",
        "DTS-HD HRA": "8e109e50e0a0b83a5098b056e13bf6db",
        "DTS-HD MA": "dcf3ec6938fa32445f590a4da84256cd",
        "DTS X": "2f22d89048b01681dde8afe203bf2e95",
    }),
    "sonarr": ("WEB-2160p", {
        "DTS": "5964f2a8b3be407d083498e4459d05d0",
        "DTS-ES": "c1a25cd67b5d2e08287c957b1eb903ec",
        "DTS-HD HRA": "cfa5fbd8f02a86fc55d8d223d06a5e1f",
        "DTS-HD MA": "c429417a57ea8c41d57e6990a8b0033f",
        "DTS X": "9d00418ba386a083fbf4d58235fc37ef",
    }),
}


class ConfigarrLoader(yaml.SafeLoader):
    pass


ConfigarrLoader.add_constructor("!env", ConfigarrLoader.construct_scalar)


def documents(path):
    return [doc for doc in yaml.safe_load_all((SERVARR / path).read_text()) if doc]


def resource(path, kind, name):
    return next(doc for doc in documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


def configarr():
    config_map = resource("configarr/configarr.yaml", "ConfigMap", "configarr-config")
    return yaml.load(config_map["data"]["config.yml"], Loader=ConfigarrLoader)


def dts_scores(config, app):
    profile, format_ids = DTS_PROFILES[app]
    return {
        name: [
            assignment.get("score")
            for entry in config[app]["main"]["custom_formats"] if trash_id in entry["trash_ids"]
            for assignment in entry["assign_scores_to"] if assignment["name"] == profile
        ]
        for name, trash_id in format_ids.items()
    }


def min_format_score(config, app):
    profile = DTS_PROFILES[app][0]
    return next(p for p in config[app]["main"]["quality_profiles"] if p["name"] == profile)["min_format_score"]


class ConfigarrTests(unittest.TestCase):
    def test_radarr_ranks_dts_last_but_accepts_it(self):
        config = configarr()
        self.assertEqual(dts_scores(config, "radarr"), dict.fromkeys(DTS_PROFILES["radarr"][1], [-1]))
        self.assertGreaterEqual(-1, min_format_score(config, "radarr"))

    def test_sonarr_leaves_dts_unscored(self):
        # The profile floor is +1 and TRaSH gives DTS 0 there, so any penalty rejects DTS-only releases; Tdarr fixes them instead.
        config = configarr()
        self.assertEqual(dts_scores(config, "sonarr"), dict.fromkeys(DTS_PROFILES["sonarr"][1], []))


if __name__ == "__main__":
    unittest.main()

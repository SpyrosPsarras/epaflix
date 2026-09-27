#!/usr/bin/env python3
"""Run SeaDexArr only for anime that Sonarr cannot finish on its own.

SeaDexArr grabs the SeaDex pick whenever your files come from another
release group, so on its own it would replace complete shows with remuxes.
This wrapper passes it only ended, monitored anime series with missing
episodes. Airing shows are left to Sonarr's own search.

Each series runs alone so its torrents carry a seadexarr-sonarr-<id> tag.
seadexarr-import reads that tag, because batch file names are often too
bare for Sonarr to guess the series.

Exits 1 when SeaDexArr logged an error (it logs and continues per series).
"""
import json
import logging
import os
import sys

from seadexarr import SeaDexSonarr

WORK = "/work"


def wanted(s):
    return (s.seriesType == "anime" and s.status == "ended" and s.monitored
            and (s.episodeFileCount or 0) < (s.episodeCount or 0))


class ErrorCount(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


def main():
    config = {
        "sonarr_url": os.environ["SONARR_URL"],
        "sonarr_api_key": os.environ["SONARR_API_KEY"],
        "qbit_info": {
            "host": os.environ["QBT_URL"],
            "username": os.environ["QBT_USERNAME"],
            "password": os.environ["QBT_PASSWORD"],
        },
        "sonarr_torrent_category": os.environ.get("QBT_CATEGORY", "tv-sonarr"),
        "max_torrents_to_add": int(os.environ.get("MAX_TORRENTS", "2")),
        "public_only": True,
        "prefer_dual_audio": True,
        "want_best": True,
        "interactive": False,
        "log_level": "INFO",
    }
    os.makedirs(WORK, exist_ok=True)
    os.chdir(WORK)
    # JSON is valid YAML, and it needs no quoting of the secrets.
    with open("config.yml", "w") as f:
        json.dump(config, f)

    sds = SeaDexSonarr(config="config.yml", cache="cache.json")
    errors = ErrorCount()
    sds.logger.addHandler(errors)

    # ponytail: relies on SeaDexArr 1.0.0 internals (get_all_sonarr_series,
    # torrent_tags, torrents_added); the CronJob pins that version.
    series = [s for s in sds.get_all_sonarr_series() if wanted(s)]
    print("%d ended anime series with missing episodes: %s"
          % (len(series), ", ".join(s.title for s in series) or "-"))
    for s in series:
        sds.torrent_tags = "seadexarr,seadexarr-sonarr-%d" % s.id
        sds.get_all_sonarr_series = lambda s=s: [s]
        sds.run()
        if sds.torrents_added >= sds.max_torrents_to_add:
            print("torrent limit reached, the rest waits for the next run")
            break

    print("added %d torrent(s), %d error(s)" % (sds.torrents_added, errors.count))
    return 1 if errors.count else 0


if __name__ == "__main__":
    sys.exit(main())

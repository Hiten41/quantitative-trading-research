"""Profile a half-hour of real replay with the same engine as the full study."""

import cProfile
import pstats
from pathlib import Path

from mmbacktest.config import Config
from mmbacktest.data.feed import HistoricalDataFeed
from mmbacktest.engine import EventEngine


def main():
    c = Config.load(Path("configs/default.yaml"))
    feed = HistoricalDataFeed(Path("data"), c)
    day = feed.load(c.evaluation_start, 1800)
    engine = EventEngine(c, "adaptive")
    profiler = cProfile.Profile()
    profiler.runcall(engine.replay, day)
    pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(12)


if __name__ == "__main__":
    main()

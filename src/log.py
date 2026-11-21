import logging
import os
import sys


def setup_logging(level: str = "INFO") -> None:
    os.makedirs("data", exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/pipeline.log", encoding="utf-8"),
        ],
        force=True,
    )
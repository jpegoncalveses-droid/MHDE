import logging
import sys


def setup_logging(settings: dict) -> None:
    log_cfg = settings.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt = log_cfg.get("format", "%(asctime)s %(name)s %(levelname)s %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger("mhde")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

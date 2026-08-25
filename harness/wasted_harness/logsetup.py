"""Structured (logfmt-style) logging for the harness.

Every record renders as `ts=<iso> level=<lvl> logger=<name> msg="..." key=value ...`
so the 3am operator can grep the run log by key. Extra fields are passed via
``logger.info("msg", extra={"kv": {...}})``.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


def _fmt_value(v: object) -> str:
    s = str(v)
    if any(c in s for c in ' "='):
        return '"' + s.replace('"', "'") + '"'
    return s


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        parts = [
            f"ts={ts}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f'msg="{record.getMessage().replace(chr(34), chr(39))}"',
        ]
        kv = getattr(record, "kv", None)
        if isinstance(kv, dict):
            parts.extend(f"{k}={_fmt_value(v)}" for k, v in kv.items())
        if record.exc_info and record.exc_info[0] is not None:
            parts.append(f"exc={_fmt_value(self.formatException(record.exc_info))}")
        return " ".join(parts)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root.addHandler(handler)
    # Third-party chatter stays at WARNING; our loggers speak at INFO.
    for noisy in ("httpx", "httpcore", "websocket", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # obsws-python re-logs connection failures with full tracebacks at ERROR;
    # our obs wrapper reports the same failure with operator context.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

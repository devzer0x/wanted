"""Structured (logfmt-style) logging for the harness.

Every record renders as `ts=<iso> level=<lvl> logger=<name> msg="..." key=value ...`
so the 3am operator can grep the run log by key. Extra fields are passed via
``logger.info("msg", extra={"kv": {...}})``.

Console encoding: log lines and ``--check`` output contain non-ASCII text
(section marks, em dashes). A Windows console defaults to a legacy code page
(cp437/cp850/cp1252) that cannot encode them, and a single UnicodeEncodeError
inside a logging handler is enough to lose the run log. :func:`force_utf8_console`
reconfigures stdout/stderr to UTF-8 with a replacement fallback and is called by
every entrypoint before anything prints.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from datetime import UTC, datetime


def _fmt_value(v: object) -> str:
    s = str(v)
    if any(c in s for c in ' "='):
        return '"' + s.replace('"', "'") + '"'
    return s


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
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


def force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 tolerant. Idempotent; never raises.

    ``errors="replace"`` is deliberate: a mangled character in a log line is a
    cosmetic problem, a crashed logging handler at 3am is not.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # Already-detached or non-reconfigurable stream (pytest capture, a pipe
        # opened in binary mode): leave it as it is.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def setup_logging(level: int = logging.INFO) -> None:
    force_utf8_console()
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root.addHandler(handler)
    # Third-party chatter stays at WARNING; our loggers speak at INFO.
    # "httpx2" is the vendored client the Anthropic SDK logs through; without it
    # every model call prints an INFO request line into the operator's log.
    for noisy in ("httpx", "httpx2", "httpcore", "websocket", "urllib3", "hpack", "h11"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # obsws-python re-logs connection failures with full tracebacks at ERROR;
    # our obs wrapper reports the same failure with operator context.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

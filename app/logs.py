"""Structured JSON logs with request/job correlation IDs and redaction.

Every record passes through `redact()` (message, args, and traceback), so a
stray email address, API key, OAuth token, or draft text in an exception
message is scrubbed before it reaches stderr. Code still never logs draft
bodies, page text, or names on purpose; redaction is the safety net.
"""
import contextvars
import json
import logging
import re
import sys
import time

request_id = contextvars.ContextVar("request_id", default=None)
job_id = contextvars.ContextVar("job_id", default=None)

_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer [redacted]"),
    # key/value pairs whose value is always sensitive: JSON, query strings, headers, kwargs
    (
        re.compile(
            r"""(?i)(["']?(?:access_token|refresh_token|id_token|client_secret|api[_-]?key|password|passphrase|"""
            r"""secret|token|authorization|cookie|x-app-token|x-csrf-token|subject|body|code)["']?\s*[:=]\s*)"""
            r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,&;}]+)"""
        ),
        r"\1[redacted]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[api-key]"),
    (re.compile(r"\bya29\.[\w-]+"), "[oauth-token]"),
    (re.compile(r"\b1//[\w-]{10,}"), "[oauth-token]"),
    (re.compile(r"\bgAAAAA[\w-]{20,}={0,2}"), "[ciphertext]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"(?<![\w/])\+?\d[\d ().-]{8,}\d(?![\w/])"), "[phone]"),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "[secret]"),  # long opaque tokens (session ids, app tokens)
]


def redact(text):
    text = str(text)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for k, var in (("request_id", request_id), ("job_id", job_id)):
            if var.get():
                out[k] = var.get()
        for k in ("campaign_id", "kind", "status", "duration_ms", "method", "path", "user_id"):
            if hasattr(record, k):
                out[k] = redact(getattr(record, k))
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def setup(level="INFO", stream=None):
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # URL-level request logs from these libraries can carry query strings (OAuth codes); keep them quiet.
    for noisy in ("httpx", "httpcore", "googleapiclient", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler

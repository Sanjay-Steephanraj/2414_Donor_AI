import atexit
import logging
import logging.handlers
import queue
from logging_loki import LokiHandler
import uuid
import os
import contextvars

# Context variable for request ID
_request_id_ctx_var = contextvars.ContextVar("request_id", default=None)

# Module-level reference to the QueueListener so it is not garbage collected.
_loki_listener = None

# Safety-net timeout for Loki push requests performed by the background listener thread.
LOKI_PUSH_TIMEOUT_SECONDS = 2


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        request_id = _request_id_ctx_var.get()
        record.request_id = request_id or "unknown"
        return True


def set_request_id(request_id=None):
    if not request_id:
        request_id = str(uuid.uuid4())
    _request_id_ctx_var.set(request_id)
    return request_id


logger_instance = None


def setup_console_handler(logger):
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(request_id)s - %(message)s'
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def _patch_loki_session_timeout(loki_handler):
    """Inject a timeout into the Loki emitter's session POST calls.

    The installed logging-loki 0.3.1 emitter invokes
    ``self.session.post(self.url, json=payload)`` without a timeout argument.
    We wrap the session's post method so every call includes ``timeout=2``.
    """
    emitter = loki_handler.emitter
    # Accessing the session property creates the requests.Session if needed.
    session = emitter.session
    original_post = session.post

    def post_with_timeout(url, **kwargs):
        kwargs.setdefault("timeout", LOKI_PUSH_TIMEOUT_SECONDS)
        return original_post(url, **kwargs)

    session.post = post_with_timeout


def setup_loki_handler(logger):
    global _loki_listener

    loki_url = os.getenv('LOKI_URL')
    loki_username = os.getenv('LOKI_USERNAME')
    loki_password = os.getenv('LOKI_PASSWORD')

    if not loki_url:
        print("No Loki URL configured, skipping Loki handler.")
        return

    try:
        if not loki_username:
            loki_handler = LokiHandler(
                url=loki_url,
                tags={"application": "24:14-gloo"},
                version="1",
            )
        else:
            loki_handler = LokiHandler(
                url=loki_url,
                tags={"application": "24:14-gloo"},
                auth=(loki_username, loki_password),
                version="1",
            )
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(request_id)s - %(message)s')
        loki_handler.setFormatter(formatter)

        # Inject a 2-second network timeout on the Loki push so a slow Loki server
        # does not hang the background listener thread.
        _patch_loki_session_timeout(loki_handler)

        # Wrap the synchronous Loki handler in a QueueHandler/QueueListener so the
        # actual HTTP POST happens on a background thread and never blocks the
        # request path.
        loki_queue = queue.Queue(-1)
        queue_handler = logging.handlers.QueueHandler(loki_queue)
        _loki_listener = logging.handlers.QueueListener(
            loki_queue, loki_handler, respect_handler_level=True
        )
        _loki_listener.start()
        atexit.register(_loki_listener.stop)

        logger.addHandler(queue_handler)
        print(f"Initialized Loki handler at {loki_url} (queued, timeout={LOKI_PUSH_TIMEOUT_SECONDS}s)")
    except Exception as e:
        print(f"Failed to initialize LokiHandler: {e}")


def get_logger():
    global logger_instance
    if logger_instance is None:
        logger_instance = logging.getLogger("ark-ai")
        logger_instance.setLevel(logging.DEBUG)
        logger_instance.addFilter(RequestIdFilter())
        setup_console_handler(logger_instance)
        setup_loki_handler(logger_instance)
        logger_instance.propagate = False
        print("Logger initialized")
    return logger_instance

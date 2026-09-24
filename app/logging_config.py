import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Deliberately exclude messages, exception bodies, URLs and request inputs.
        return json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                           "level": record.levelname,
                           "event": getattr(record, "event", "application_event"),
                           "status": getattr(record, "status", None)})


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("market_app")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Uvicorn access logs include callback query strings. Never emit them.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("kiteconnect", "urllib3", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    # SDK callbacks log raw connection reasons, potentially containing credential URLs.
    for name in ("kiteconnect.ticker", "twisted", "autobahn"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)

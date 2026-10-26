import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

class LogBufferHandler(logging.Handler):
    """Logging handler that retains recent application logs in a thread-safe ring buffer for the web terminal."""

    def __init__(self, capacity: int = 300):
        super().__init__()
        self.capacity = capacity
        self.buffer = deque(maxlen=capacity)
        self.counter = 0

    def emit(self, record: logging.LogRecord):
        try:
            self.counter += 1
            # Format timestamp nicely (HH:MM:SS)
            log_time = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
            msg = self.format(record)
            
            # Identify special success markers
            level = record.levelname
            if "Successfully" in msg or "SUCCESS" in msg or "🎉" in msg or "published successfully" in msg:
                level = "SUCCESS"

            entry = {
                "id": self.counter,
                "timestamp": log_time,
                "level": level,
                "logger": record.name,
                "message": msg
            }
            self.buffer.append(entry)
        except Exception:
            self.handleError(record)

    def get_logs(self, since_id: int = 0) -> List[Dict[str, Any]]:
        """Return logs added after since_id."""
        return [entry for entry in self.buffer if entry["id"] > since_id]

    def clear(self):
        """Empty the buffer."""
        self.buffer.clear()

# Global terminal log handler
terminal_logger_handler = LogBufferHandler(capacity=400)
terminal_logger_handler.setLevel(logging.INFO)
terminal_formatter = logging.Formatter("%(message)s")
terminal_logger_handler.setFormatter(terminal_formatter)

def setup_terminal_logging():
    """Attaches the terminal buffer handler to the root and child loggers."""
    root_logger = logging.getLogger()
    if terminal_logger_handler not in root_logger.handlers:
        root_logger.addHandler(terminal_logger_handler)

"""Console tee logging for API startup and active-vault log files."""

import sys
import threading

from .logger import log_dirs


class TerminalLogger:
    def __init__(self, filename, original_stream):
        self.terminal = original_stream
        self.filename = filename
        raw_logs_dir, _ = log_dirs()
        self.log_path = raw_logs_dir / filename
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115 - stream stays open until close().

    def write(self, message):
        self.terminal.write(message)
        with self._lock:
            self._handle.write(message)
            self._handle.flush()

    def flush(self):
        self.terminal.flush()
        with self._lock:
            self._handle.flush()

    def isatty(self):
        return hasattr(self.terminal, "isatty") and self.terminal.isatty()

    def close(self):
        with self._lock:
            self._handle.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - __del__ must not leak shutdown-time errors.
            pass

    def __getattr__(self, attr):
        return getattr(self.terminal, attr)


_terminal_logging_configured = False


def configure_terminal_logging():
    global _terminal_logging_configured
    raw_logs_dir, _ = log_dirs()
    target_path = raw_logs_dir / "console.log"
    if (
        _terminal_logging_configured
        and isinstance(sys.stdout, TerminalLogger)
        and isinstance(sys.stderr, TerminalLogger)
        and sys.stdout.log_path == target_path
        and sys.stderr.log_path == target_path
    ):
        return
    original_stdout = sys.stdout.terminal if isinstance(sys.stdout, TerminalLogger) else sys.stdout
    original_stderr = sys.stderr.terminal if isinstance(sys.stderr, TerminalLogger) else sys.stderr
    if isinstance(sys.stdout, TerminalLogger):
        sys.stdout.close()
    if isinstance(sys.stderr, TerminalLogger):
        sys.stderr.close()
    sys.stdout = TerminalLogger("console.log", original_stdout)
    sys.stderr = TerminalLogger("console.log", original_stderr)
    _terminal_logging_configured = True


def restore_terminal_logging():
    global _terminal_logging_configured
    if isinstance(sys.stdout, TerminalLogger):
        original_stdout = sys.stdout.terminal
        sys.stdout.close()
        sys.stdout = original_stdout
    if isinstance(sys.stderr, TerminalLogger):
        original_stderr = sys.stderr.terminal
        sys.stderr.close()
        sys.stderr = original_stderr
    _terminal_logging_configured = False

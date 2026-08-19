"""Process-wide diagnostics for the Pocket TestAgent desktop application."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import sys
import threading
import traceback
from typing import Any

from .app_paths import logs_dir


_LOGGER_NAME = "pocket_testagent"
_LOGGER: logging.Logger | None = None
_INSTALL_LOCK = threading.Lock()
_PREVIOUS_SYS_EXCEPTHOOK = sys.excepthook


def _qt_message_handler(message_type: Any, context: Any, message: str) -> None:
    logger = get_logger()
    logger.error(
        "qt_message type=%s file=%s line=%s function=%s message=%s",
        getattr(message_type, "name", str(message_type)),
        getattr(context, "file", ""),
        getattr(context, "line", ""),
        getattr(context, "function", ""),
        message,
    )


def get_logger() -> logging.Logger:
    global _LOGGER
    with _INSTALL_LOCK:
        if _LOGGER is not None:
            return _LOGGER
        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            try:
                path = os.path.join(logs_dir(), "app.log")
                handler: logging.Handler = RotatingFileHandler(
                    path,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
            except OSError:
                # A read-only test/container environment must not make the GUI
                # fail during import. Windows installations normally use the
                # writable LOCALAPPDATA path above.
                handler = logging.NullHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(handler)
        _LOGGER = logger
        return logger


def install_logging() -> logging.Logger:
    """Install file logging, Python exception hooks, and the Qt message hook."""
    logger = get_logger()
    with _INSTALL_LOCK:
        if getattr(install_logging, "_installed", False):
            return logger

        def handle_exception(exc_type: type[BaseException], exc_value: BaseException,
                             exc_tb: Any) -> None:
            if issubclass(exc_type, KeyboardInterrupt):
                _PREVIOUS_SYS_EXCEPTHOOK(exc_type, exc_value, exc_tb)
                return
            logger.critical("unhandled_exception", exc_info=(exc_type, exc_value, exc_tb))

        def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
            logger.critical(
                "unhandled_thread_exception thread=%s",
                getattr(args.thread, "name", "unknown"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        sys.excepthook = handle_exception
        threading.excepthook = handle_thread_exception
        try:
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(_qt_message_handler)
        except Exception:
            logger.debug("qt_message_handler_install_failed", exc_info=True)

        logger.info(
            "application_start version=%s pid=%s python=%s platform=%s executable=%s",
            _read_version(), os.getpid(), platform.python_version(), platform.platform(),
            sys.executable,
        )
        install_logging._installed = True  # type: ignore[attr-defined]
    return logger


def _read_version() -> str:
    try:
        from .app_paths import resource_path
        with open(resource_path("VERSION"), "r", encoding="utf-8") as fp:
            return fp.read().strip() or "unknown"
    except OSError:
        return "unknown"


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    logger.error("%s: %s\n%s", message, exc, "".join(traceback.format_exception(exc)))

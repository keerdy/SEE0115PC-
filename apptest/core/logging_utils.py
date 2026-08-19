from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path


LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class LoggingContext:
    log_dir: Path
    log_file: Path


def setup_logging(log_dir: str | Path, case_name: str) -> LoggingContext:
    target_dir = Path(log_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    log_file = target_dir / f"{case_name}.log"

    suite_logger = logging.getLogger("pocket_app_automation")
    suite_logger.setLevel(logging.INFO)
    suite_logger.propagate = False
    for handler in list(suite_logger.handlers):
        if getattr(handler, "_pocket_automation_owned", False):
            suite_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler._pocket_automation_owned = True  # type: ignore[attr-defined]
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler._pocket_automation_owned = True  # type: ignore[attr-defined]
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    suite_logger.addHandler(console_handler)
    suite_logger.addHandler(file_handler)

    logger = suite_logger
    logger.info("logging initialized")
    logger.info("python executable=%s", sys.executable)
    logger.info("current working directory=%s", os.getcwd())
    logger.info("log file=%s", log_file)
    return LoggingContext(log_dir=target_dir, log_file=log_file)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

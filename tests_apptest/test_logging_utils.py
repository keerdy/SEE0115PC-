from __future__ import annotations

import logging

from apptest.core.logging_utils import setup_logging


class TrackingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.was_closed = False

    def emit(self, record: logging.LogRecord) -> None:
        return None

    def close(self) -> None:
        self.was_closed = True
        super().close()


def test_setup_logging_preserves_root_and_closes_replaced_suite_handlers(tmp_path) -> None:
    root_logger = logging.getLogger()
    suite_logger = logging.getLogger("pocket_app_automation")
    root_handler = TrackingHandler()
    previous_suite_handler = TrackingHandler()
    previous_owned_handler = TrackingHandler()
    previous_owned_handler._pocket_automation_owned = True
    root_logger.addHandler(root_handler)
    suite_logger.addHandler(previous_suite_handler)
    suite_logger.addHandler(previous_owned_handler)
    try:
        context = setup_logging(tmp_path, "case3")
        assert root_handler in root_logger.handlers
        assert root_handler.was_closed is False
        assert previous_suite_handler in suite_logger.handlers
        assert previous_suite_handler.was_closed is False
        assert previous_owned_handler.was_closed is True
        assert context.log_file.name == "case3.log"
        assert context.log_file.exists()
    finally:
        root_logger.removeHandler(root_handler)
        root_handler.close()
        for handler in list(suite_logger.handlers):
            suite_logger.removeHandler(handler)
            handler.close()

"""Backend services exposed to CLI tools and future GUI integrations."""

from apptest.services.case_service import CASE_DEFINITIONS, CaseRunRequest, list_cases, run_case, run_cases
from apptest.services.otg_service import OtgMonitorConfig, OtgMonitorService
from apptest.core.execution import CancellationToken, ProgressCallback

__all__ = [
    "CASE_DEFINITIONS",
    "CaseRunRequest",
    "CancellationToken",
    "OtgMonitorConfig",
    "OtgMonitorService",
    "ProgressCallback",
    "list_cases",
    "run_case",
    "run_cases",
]

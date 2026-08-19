"""Validated PC-side model for ``custom_test/C01`` configuration.

The device remains the authority for capability checks and persistence.  This
module only builds a bounded request from values returned by the device; it
never writes ``active_config.bin`` or invents unsupported values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, Iterable, Mapping


CUSTOM_CONFIG_VERSION = 8
SUPPORTED_CUSTOM_CONFIG_VERSIONS = (7, 8)
CUSTOM_POLICY_VERSION = 1
MAX_STEPS_FALLBACK = 16
CUSTOM_STEP_PARAM_COUNT = 4
CUSTOM_C01_MAX_TRACKED_MEDIA_ARTIFACTS = 512
CUSTOM_MAX_RECORD_DURATION_SECONDS = 24 * 60 * 60

# C01 may run for up to 30 days on the device.  The PC is a status observer
# and must not turn that into a false one-hour failure.  Keep a small terminal
# status/reporting window after the device's own estimate.
CUSTOM_C01_MAX_RUNTIME_SECONDS = 30 * 24 * 60 * 60
CUSTOM_C01_MIN_MONITOR_GRACE_SECONDS = 10 * 60
CUSTOM_C01_MONITOR_GRACE_RATIO = 0.20
CUSTOM_C01_COMPLETION_GRACE_SECONDS = 15 * 60
CUSTOM_C01_STORAGE_MIB = 1024 * 1024
CUSTOM_C01_PHOTO_WORST_CASE_BYTES = 25 * CUSTOM_C01_STORAGE_MIB
CUSTOM_C01_MEDIA_FILE_OVERHEAD_BYTES = 2 * CUSTOM_C01_STORAGE_MIB
CUSTOM_C01_VIDEO_WORST_CASE_BYTES_PER_SECOND = {
    "1080P": 5 * CUSTOM_C01_STORAGE_MIB,
    "2.7K": 8 * CUSTOM_C01_STORAGE_MIB,
    "3K": 10 * CUSTOM_C01_STORAGE_MIB,
    "4K": 15 * CUSTOM_C01_STORAGE_MIB,
}
CUSTOM_C01_SLOW_MOTION_WORST_CASE_BYTES_PER_SECOND = {
    0: 8 * CUSTOM_C01_STORAGE_MIB,   # 2X / 60fps
    1: 12 * CUSTOM_C01_STORAGE_MIB,  # 4X / 120fps
}
CUSTOM_C01_PER_STEP_BUDGET_MS = 4_000
CUSTOM_C01_SCREEN_ROTATION_BUDGET_MS = 6_000
CUSTOM_C01_MEDIA_SETTLE_BUDGET_MS = 10_000
CUSTOM_C01_PLAYBACK_CHECK_BUDGET_MS = 12_000
CUSTOM_C01_MAX_RUNTIME_MS = 30 * 24 * 60 * 60 * 1000

ACTION_NAV = 1
ACTION_MODE = 2
ACTION_VIDEO = 3
ACTION_VERIFY = 4
ACTION_CLICK = 5
ACTION_SWIPE = 6
ACTION_SLIDER = 7
ACTION_VIDEO_RECORD = 8
ACTION_PHOTO_CAPTURE = 9
ACTION_SLOW_MOTION_RECORD = 10
ACTION_SCREEN_ROTATE = 11
ACTION_NAV_TARGET = 12

ACTION_LABELS = {
    ACTION_NAV: "进入基础页面",
    ACTION_MODE: "切换拍摄模式",
    ACTION_VIDEO: "设置视频规格",
    ACTION_VERIFY: "确认设备状态",
    ACTION_CLICK: "界面点击（如进入设置）",
    ACTION_SWIPE: "界面滑动（如控制中心）",
    ACTION_SLIDER: "调节已授权滑杆",
    ACTION_VIDEO_RECORD: "录制视频",
    ACTION_PHOTO_CAPTURE: "拍摄照片",
    ACTION_SLOW_MOTION_RECORD: "慢动作录像",
    ACTION_SCREEN_ROTATE: "旋转屏幕",
    ACTION_NAV_TARGET: "进入语义页面目标",
}

CHECK_BASELINE = 0
CHECK_FILE = 1
CHECK_PLAYBACK = 2
CHECK_PLAYBACK_DAMAGE = 3

PAGE_WAIT_AUTO = 0
PAGE_WAIT_ADDITIONAL = 1

VIDEO_CANVAS_LANDSCAPE = 0
VIDEO_CANVAS_PORTRAIT = 2

class CustomConfigError(ValueError):
    """A local validation or unsupported-device error."""


def custom_c01_monitor_timeout_seconds(estimated_runtime_ms: int | None) -> float:
    """Return C01's PC monitoring limit from the device canonical estimate.

    Missing, malformed, or out-of-range device estimates use the device's
    maximum run budget plus a completion window; they must never fall back to
    RecordWorker's generic one-hour timeout.
    """
    maximum = float(CUSTOM_C01_MAX_RUNTIME_SECONDS + CUSTOM_C01_COMPLETION_GRACE_SECONDS)
    if isinstance(estimated_runtime_ms, bool) or not isinstance(estimated_runtime_ms, int):
        return maximum
    if estimated_runtime_ms <= 0 or estimated_runtime_ms > CUSTOM_C01_MAX_RUNTIME_SECONDS * 1000:
        return maximum
    estimated_seconds = estimated_runtime_ms / 1000.0
    monitor_grace = max(
        float(CUSTOM_C01_MIN_MONITOR_GRACE_SECONDS),
        estimated_seconds * CUSTOM_C01_MONITOR_GRACE_RATIO,
    )
    return min(estimated_seconds + monitor_grace, maximum)


def _next_cleanup_cycle(frequency: int, current_cycle: int, cycles: int) -> int:
    if frequency == 0:
        return cycles
    return min(cycles, ((current_cycle + frequency) // frequency) * frequency)


def _retained_media_budget_bytes(
    config: "CustomConfig", capabilities: "CustomCapabilities", action: int,
    current_cycle: int = 0, current_step: int = 0,
) -> int:
    frequency = (
        config.photo_cleanup_every_cycles
        if action == ACTION_PHOTO_CAPTURE else config.video_cleanup_every_cycles
    )
    end_cycle = _next_cleanup_cycle(frequency, current_cycle, config.cycles)
    total = 0
    for cycle in range(current_cycle, end_cycle):
        first_step = current_step if cycle == current_cycle else 0
        for step in config.steps[first_step:]:
            if step.action != action or (step.run_once != 0 and cycle != 0):
                continue
            if action == ACTION_PHOTO_CAPTURE:
                total += CUSTOM_C01_PHOTO_WORST_CASE_BYTES
                continue
            if action == ACTION_SLOW_MOTION_RECORD:
                total += (
                    CUSTOM_C01_SLOW_MOTION_WORST_CASE_BYTES_PER_SECOND.get(
                        step.params[1], CUSTOM_C01_SLOW_MOTION_WORST_CASE_BYTES_PER_SECOND[1],
                    ) * step.params[2] + CUSTOM_C01_MEDIA_FILE_OVERHEAD_BYTES
                )
                continue
            profile = capabilities.video_profiles.get((step.video_canvas, step.arg0))
            label = profile.resolution_label if profile is not None else "4K"
            total += (
                CUSTOM_C01_VIDEO_WORST_CASE_BYTES_PER_SECOND.get(
                    label, CUSTOM_C01_VIDEO_WORST_CASE_BYTES_PER_SECOND["4K"],
                ) * step.arg2 + CUSTOM_C01_MEDIA_FILE_OVERHEAD_BYTES
            )
    return total


def custom_c01_storage_budget_bytes(
    config: "CustomConfig", capabilities: "CustomCapabilities",
    current_cycle: int = 0, current_step: int = 0,
) -> int:
    """Return conservative bytes retained before each media type's next cleanup."""
    return (
        _retained_media_budget_bytes(
            config, capabilities, ACTION_PHOTO_CAPTURE, current_cycle, current_step,
        ) +
        _retained_media_budget_bytes(
            config, capabilities, ACTION_VIDEO_RECORD, current_cycle, current_step,
        ) + _retained_media_budget_bytes(
            config, capabilities, ACTION_SLOW_MOTION_RECORD, current_cycle, current_step,
        )
    )


def custom_c01_max_tracked_media_artifacts(config: "CustomConfig") -> int:
    """Return the highest number of C01 media primaries retained before cleanup.

    The device keeps one bounded tracking record per newly-created photo/video
    primary.  It removes each primary's exact-name thumbnail/finalize family
    together, so sidecars do not multiply this count.
    """
    active_photos = 0
    active_videos = 0
    maximum = 0
    for cycle in range(config.cycles):
        for step in config.steps:
            if step.run_once != 0 and cycle != 0:
                continue
            if step.action == ACTION_PHOTO_CAPTURE and config.photo_cleanup_every_cycles:
                active_photos += 1
            elif step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD) and config.video_cleanup_every_cycles:
                active_videos += 1
        maximum = max(maximum, active_photos + active_videos)
        cycle_number = cycle + 1
        if (config.photo_cleanup_every_cycles and
                cycle_number % config.photo_cleanup_every_cycles == 0):
            active_photos = 0
        if (config.video_cleanup_every_cycles and
                cycle_number % config.video_cleanup_every_cycles == 0):
            active_videos = 0
    return maximum


def custom_c01_media_artifact_counts(config: "CustomConfig") -> tuple[int, int]:
    """Return total photos and video primaries C01 will request across all cycles."""
    photos = 0
    videos = 0
    for cycle in range(config.cycles):
        for step in config.steps:
            if step.run_once != 0 and cycle != 0:
                continue
            if step.action == ACTION_PHOTO_CAPTURE:
                photos += 1
            elif step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD):
                videos += 1
    return photos, videos


def custom_c01_estimated_runtime_ms(
    config: "CustomConfig", capabilities: "CustomCapabilities",
) -> int:
    """Mirror the device's conservative C01 runtime estimator before saving.

    The device remains authoritative after it validates and persists the
    configuration.  This copy is only a transparent pre-save preview and uses
    the same per-step, media-settle, playback, and cleanup accounting.
    """
    total = 0
    for cycle in range(config.cycles):
        for index, step in enumerate(config.steps):
            if step.run_once != 0 and cycle != 0:
                continue
            total += CUSTOM_C01_PER_STEP_BUDGET_MS
            if step.action == ACTION_SCREEN_ROTATE:
                total += CUSTOM_C01_SCREEN_ROTATION_BUDGET_MS - CUSTOM_C01_PER_STEP_BUDGET_MS
            if step.page_wait_mode == PAGE_WAIT_ADDITIONAL:
                total += config.page_settle_ms
            if step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD):
                seconds = step.params[2] if step.action == ACTION_SLOW_MOTION_RECORD else step.arg2
                total += seconds * 1000 + CUSTOM_C01_MEDIA_SETTLE_BUDGET_MS
                if config.video_check_mode in (CHECK_PLAYBACK, CHECK_PLAYBACK_DAMAGE):
                    total += CUSTOM_C01_PLAYBACK_CHECK_BUDGET_MS
            elif step.action == ACTION_PHOTO_CAPTURE:
                total += CUSTOM_C01_MEDIA_SETTLE_BUDGET_MS
            if any(next_step.run_once == 0 or cycle == 0 for next_step in config.steps[index + 1:]):
                total += step.step_interval_ms
        if cycle + 1 < config.cycles:
            total += config.cycle_interval_ms

    cleanup_cycles = sum(
        1 for cycle in range(1, config.cycles + 1)
        if ((config.photo_cleanup_every_cycles and cycle % config.photo_cleanup_every_cycles == 0) or
            (config.video_cleanup_every_cycles and cycle % config.video_cleanup_every_cycles == 0))
    )
    cleanup_wait_ms = sum(
        capabilities.cleanup_wait_options[index]
        for index in (
            config.cleanup_before_wait_index,
            config.cleanup_between_wait_index,
            config.cleanup_after_wait_index,
        )
    )
    total += cleanup_cycles * (cleanup_wait_ms + CUSTOM_C01_MEDIA_SETTLE_BUDGET_MS)
    return min(total, CUSTOM_C01_MAX_RUNTIME_MS)


def custom_c01_compatibility_media_interval_ms(capabilities: "CustomCapabilities") -> int:
    """Return the legacy global media interval value for V7/V8 requests.

    Current C01 execution uses ``CustomStep.step_interval_ms`` for every
    action.  ``media_interval_ms`` remains in the wire format so existing
    saved plans can be read, but new editor saves must not expose a second,
    ineffective timing control.  Zero is the neutral value; the fallback
    keeps compatibility with a device that advertises a nonzero-only list.
    """
    options = capabilities.interval_options["media_interval_ms"]
    return 0 if 0 in options else options[0]


def _int_list(value: Any, name: str, *, minimum: int = 0) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise CustomConfigError(f"device capability '{name}' must be a list")
    result: list[int] = []
    for item in value:
        if not isinstance(item, int) or item < minimum:
            raise CustomConfigError(f"device capability '{name}' contains an invalid value")
        if item not in result:
            result.append(item)
    return tuple(result)


def _range(value: Any, name: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        low, high = value.get("min"), value.get("max")
    elif isinstance(value, list) and len(value) == 2:
        low, high = value
    else:
        raise CustomConfigError(f"device capability '{name}' must contain min/max")
    if not isinstance(low, int) or not isinstance(high, int) or low <= 0 or high < low:
        raise CustomConfigError(f"device capability '{name}' has an invalid range")
    return low, high


def _integer_mapping(value: Any, name: str) -> Dict[int, str]:
    if not isinstance(value, list):
        raise CustomConfigError(f"device capability '{name}' must be a list")
    result: Dict[int, str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise CustomConfigError(f"device capability '{name}' contains an invalid item")
        raw_id = item.get("id", item.get("value"))
        label = item.get("label", item.get("name", raw_id))
        if not isinstance(raw_id, int) or not isinstance(label, (str, int)):
            raise CustomConfigError(f"device capability '{name}' contains an invalid id")
        result[raw_id] = str(label)
    return result


@dataclass(frozen=True)
class VideoProfile:
    canvas: int
    resolution_id: int
    resolution_label: str
    fps: Dict[int, str]


@dataclass(frozen=True)
class CustomPageTarget:
    target_id: int
    page: int
    key: str
    mode_context: str
    subpage: str
    label: str


@dataclass(frozen=True)
class CustomCapabilities:
    config_version: int
    policy_version: int
    max_steps: int
    cycles_range: tuple[int, int]
    record_seconds_range: tuple[int, int]
    actions: tuple[int, ...]
    safe_pages: Dict[int, str]
    page_targets: Dict[int, CustomPageTarget]
    mode_options: Dict[int, str]
    video_presets: Dict[int, str]
    verify_options: Dict[int, str]
    policy_steps: Dict[int, Dict[str, Any]]
    video_profiles: Dict[tuple[int, int], VideoProfile]
    slow_motion_resolutions: Dict[int, str]
    slow_motion_rates: Dict[int, str]
    slow_motion_record_seconds_range: tuple[int, int] | None
    slow_motion_orientation: str | None
    interval_options: Dict[str, tuple[int, ...]]
    page_wait_modes: tuple[int, ...]
    cleanup_wait_options: tuple[int, ...]
    photo_check_modes: tuple[int, ...]
    video_check_modes: tuple[int, ...]
    media_manifest_supported: bool
    cleanup_supported: bool
    available_storage_bytes: int | None
    safe_free_storage_bytes: int | None
    active_revision: int
    active_crc: int | None
    supports_ui_complete: bool
    supports_ui_frozen: bool

    @classmethod
    def from_reply(cls, reply: Mapping[str, Any]) -> "CustomCapabilities":
        if reply.get("code") != 0:
            raise CustomConfigError(str(reply.get("msg", "get_custom_capabilities failed")))
        version = reply.get("config_version")
        if (not isinstance(version, int) or isinstance(version, bool) or
                version not in SUPPORTED_CUSTOM_CONFIG_VERSIONS):
            raise CustomConfigError(
                f"device custom config protocol version {version!r} is unsupported; "
                "update the device firmware and the PC client to the same protocol version"
            )
        policy_version = reply.get("policy_version")
        max_steps = reply.get("max_steps")
        if (not isinstance(policy_version, int) or isinstance(policy_version, bool) or
                policy_version != CUSTOM_POLICY_VERSION):
            raise CustomConfigError(
                f"device custom policy version {policy_version!r} != {CUSTOM_POLICY_VERSION}; "
                "update the device firmware and the PC client to the same policy version"
            )
        if not isinstance(max_steps, int) or not 1 <= max_steps <= MAX_STEPS_FALLBACK:
            raise CustomConfigError("device returned an invalid custom step limit")

        actions = _int_list(reply.get("actions"), "actions", minimum=ACTION_NAV)
        if not actions or any(action not in ACTION_LABELS for action in actions):
            raise CustomConfigError("device returned an unsupported custom action")

        interval_raw = reply.get("interval_options")
        if not isinstance(interval_raw, Mapping):
            raise CustomConfigError("device did not return interval options")
        interval_options = {
            field: _int_list(interval_raw.get(field), f"interval_options.{field}")
            for field in ("page_settle_ms", "step_interval_ms", "media_interval_ms", "cycle_interval_ms")
        }
        if any(not values for values in interval_options.values()):
            raise CustomConfigError("device returned an empty interval option set")
        page_wait_modes = _int_list(reply.get("page_wait_modes"), "page_wait_modes")
        if not page_wait_modes or any(mode not in (PAGE_WAIT_AUTO, PAGE_WAIT_ADDITIONAL)
                                      for mode in page_wait_modes):
            raise CustomConfigError("device returned an invalid page wait mode set")

        profiles: Dict[tuple[int, int], VideoProfile] = {}
        raw_profiles = reply.get("video_profiles", [])
        if not isinstance(raw_profiles, list):
            raise CustomConfigError("device capability 'video_profiles' must be a list")
        for raw in raw_profiles:
            if not isinstance(raw, Mapping):
                raise CustomConfigError("device returned an invalid video profile")
            resolution_id = raw.get("resolution_id")
            resolution_label = raw.get("resolution_label", raw.get("resolution"))
            canvas = raw.get("canvas", VIDEO_CANVAS_LANDSCAPE)
            if (not isinstance(canvas, int) or
                    canvas not in (VIDEO_CANVAS_LANDSCAPE, VIDEO_CANVAS_PORTRAIT) or
                    not isinstance(resolution_id, int) or not isinstance(resolution_label, (str, int))):
                raise CustomConfigError("device returned an invalid video resolution")
            profiles[(canvas, resolution_id)] = VideoProfile(
                canvas=canvas,
                resolution_id=resolution_id,
                resolution_label=str(resolution_label),
                fps=_integer_mapping(raw.get("fps"), "video_profiles.fps"),
            )
            if any(fps <= 0 for fps in profiles[(canvas, resolution_id)].fps):
                raise CustomConfigError(
                    "device advertises an invalid video frame rate"
                )

        raw_policy_steps = reply.get("policy_steps", [])
        policy_steps: Dict[int, Dict[str, Any]] = {}
        if not isinstance(raw_policy_steps, list):
            raise CustomConfigError("device capability 'policy_steps' must be a list")
        for item in raw_policy_steps:
            if not isinstance(item, Mapping):
                raise CustomConfigError("device returned an invalid policy step")
            policy_id = item.get("policy_id")
            action = item.get("action")
            page = item.get("page", 0)
            label = item.get("label", policy_id)
            if (not isinstance(policy_id, int) or not isinstance(action, int) or
                    not isinstance(page, int) or
                    action not in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER)):
                raise CustomConfigError("device returned an invalid policy step")
            policy_steps[policy_id] = {"action": action, "label": str(label), "page": page}

        safe_pages = _integer_mapping(reply.get("safe_pages", []), "safe_pages")
        raw_page_targets = reply.get("page_targets", [])
        if not isinstance(raw_page_targets, list):
            raise CustomConfigError("device capability 'page_targets' must be a list")
        page_targets: Dict[int, CustomPageTarget] = {}
        for raw in raw_page_targets:
            if not isinstance(raw, Mapping):
                raise CustomConfigError("device returned an invalid page target")
            target_id = raw.get("target_id")
            page = raw.get("page")
            key = raw.get("key")
            mode_context = raw.get("mode_context")
            subpage = raw.get("subpage")
            label = raw.get("label")
            if (not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0 or
                    not isinstance(page, int) or isinstance(page, bool) or
                    not isinstance(key, str) or not key or
                    not isinstance(mode_context, str) or not mode_context or
                    not isinstance(subpage, str) or not subpage or
                    not isinstance(label, str) or not label or
                    target_id in page_targets):
                raise CustomConfigError("device returned an invalid page target")
            page_targets[target_id] = CustomPageTarget(
                target_id=target_id,
                page=page,
                key=key,
                mode_context=mode_context,
                subpage=subpage,
                label=label,
            )
        mode_options = _integer_mapping(reply.get("mode_options", []), "mode_options")
        video_presets = _integer_mapping(reply.get("video_presets", []), "video_presets")
        verify_options = _integer_mapping(reply.get("verify_options", []), "verify_options")
        if ACTION_NAV in actions and not safe_pages:
            raise CustomConfigError("device exposes NAV without a safe page")
        if ACTION_NAV_TARGET in actions and not page_targets:
            raise CustomConfigError("device exposes NAV_TARGET without a page target")
        if ACTION_MODE in actions and not mode_options:
            raise CustomConfigError("device exposes MODE without a mode option")
        if ACTION_VIDEO in actions and not video_presets:
            raise CustomConfigError("device exposes VIDEO without a video preset")
        if ACTION_VERIFY in actions and not verify_options:
            raise CustomConfigError("device exposes VERIFY without a verify option")
        for action in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER):
            if action in actions and not any(policy["action"] == action for policy in policy_steps.values()):
                raise CustomConfigError("device exposes a policy action without an authorized option")
        if ACTION_VIDEO_RECORD in actions and not any(profile.fps for profile in profiles.values()):
            raise CustomConfigError("device exposes VIDEO_RECORD without a resolution/frame-rate pair")
        slow_motion_resolutions: Dict[int, str] = {}
        slow_motion_rates: Dict[int, str] = {}
        slow_motion_record_seconds_range: tuple[int, int] | None = None
        slow_motion_orientation: str | None = None
        if ACTION_SLOW_MOTION_RECORD in actions:
            slow_motion = reply.get("slow_motion")
            if not isinstance(slow_motion, Mapping):
                raise CustomConfigError("device exposes SLOW_MOTION_RECORD without slow-motion capability")
            slow_motion_resolutions = _integer_mapping(
                slow_motion.get("resolution_options"), "slow_motion.resolution_options",
            )
            slow_motion_rates = _integer_mapping(
                slow_motion.get("rate_options"), "slow_motion.rate_options",
            )
            slow_motion_record_seconds_range = _range(
                slow_motion.get("record_seconds_range"), "slow_motion.record_seconds_range",
            )
            if (slow_motion_record_seconds_range[0] < 1 or
                    slow_motion_record_seconds_range[1] > CUSTOM_MAX_RECORD_DURATION_SECONDS):
                raise CustomConfigError(
                    "device returned a slow-motion recording duration range outside the PC limit"
                )
            slow_motion_orientation = slow_motion.get("orientation")
            if (not slow_motion_resolutions or not slow_motion_rates or
                    slow_motion_orientation != "adaptive"):
                raise CustomConfigError("device returned an invalid slow-motion capability")

        active_crc = reply.get("active_config_crc")
        available_storage = reply.get("available_storage_bytes")
        safe_storage = reply.get("safe_free_storage_bytes")
        if (available_storage is not None and
                (not isinstance(available_storage, int) or available_storage < 0)):
            raise CustomConfigError("device returned an invalid available storage value")
        if (safe_storage is not None and
                (not isinstance(safe_storage, int) or safe_storage < 0)):
            raise CustomConfigError("device returned an invalid safe storage value")
        cleanup_wait_options = _int_list(reply.get("cleanup_wait_options"), "cleanup_wait_options")
        if not cleanup_wait_options:
            raise CustomConfigError("device returned an empty cleanup wait option set")
        photo_check_modes = _int_list(reply.get("photo_check_modes"), "photo_check_modes")
        video_check_modes = _int_list(reply.get("video_check_modes"), "video_check_modes")
        if (not photo_check_modes or not video_check_modes or
                any(mode not in (CHECK_BASELINE, CHECK_FILE) for mode in photo_check_modes) or
                any(mode not in (CHECK_BASELINE, CHECK_FILE, CHECK_PLAYBACK, CHECK_PLAYBACK_DAMAGE)
                    for mode in video_check_modes)):
            raise CustomConfigError("device returned an unsupported media check mode")
        media_manifest_supported = reply.get("media_manifest_supported")
        cleanup_supported = reply.get("cleanup_supported")
        if not isinstance(media_manifest_supported, bool) or not isinstance(cleanup_supported, bool):
            raise CustomConfigError("device returned invalid media capability flags")
        supports_ui_complete = False
        supports_ui_frozen = False
        if version >= 8:
            ui_checks = reply.get("ui_checks")
            if not isinstance(ui_checks, Mapping):
                raise CustomConfigError("device returned invalid UI check capabilities")
            supports_ui_complete = ui_checks.get("ui_complete") is True
            supports_ui_frozen = ui_checks.get("ui_frozen") is True
        record_seconds_range = _range(reply.get("record_seconds_range"), "record_seconds_range")
        if (record_seconds_range[0] < 1 or
                record_seconds_range[1] > CUSTOM_MAX_RECORD_DURATION_SECONDS):
            raise CustomConfigError(
                "device returned a recording duration range outside the PC limit"
            )
        return cls(
            config_version=version,
            policy_version=policy_version,
            max_steps=max_steps,
            cycles_range=_range(reply.get("cycles_range"), "cycles_range"),
            record_seconds_range=record_seconds_range,
            actions=actions,
            safe_pages=safe_pages,
            page_targets=page_targets,
            mode_options=mode_options,
            video_presets=video_presets,
            verify_options=verify_options,
            policy_steps=policy_steps,
            video_profiles=profiles,
            slow_motion_resolutions=slow_motion_resolutions,
            slow_motion_rates=slow_motion_rates,
            slow_motion_record_seconds_range=slow_motion_record_seconds_range,
            slow_motion_orientation=slow_motion_orientation,
            interval_options=interval_options,
            page_wait_modes=page_wait_modes,
            cleanup_wait_options=cleanup_wait_options,
            photo_check_modes=photo_check_modes,
            video_check_modes=video_check_modes,
            media_manifest_supported=media_manifest_supported,
            cleanup_supported=cleanup_supported,
            available_storage_bytes=available_storage if isinstance(available_storage, int) else None,
            safe_free_storage_bytes=safe_storage if isinstance(safe_storage, int) else None,
            active_revision=int(reply.get("active_config_revision", 0) or 0),
            active_crc=active_crc if isinstance(active_crc, int) else None,
            supports_ui_complete=supports_ui_complete,
            supports_ui_frozen=supports_ui_frozen,
        )


@dataclass
class CustomStep:
    action: int
    page: int = 0
    arg0: int = 0
    arg1: int = 0
    arg2: int = 0
    page_wait_mode: int = PAGE_WAIT_AUTO
    step_interval_ms: int = 0
    run_once: int = 0
    video_canvas: int = VIDEO_CANVAS_LANDSCAPE
    check_ui_complete: int = 0
    check_ui_frozen: int = 0
    params: tuple[int, int, int, int] = (0, 0, 0, 0)

    def as_payload(self, include_ui_checks: bool = True) -> Dict[str, Any]:
        payload = {
            "action": self.action,
            "page": self.page,
            "arg0": self.arg0,
            "arg1": self.arg1,
            "arg2": self.arg2,
            "page_wait_mode": self.page_wait_mode,
            "step_interval_ms": self.step_interval_ms,
            "run_once": self.run_once,
            "video_canvas": self.video_canvas,
            "params": list(self.params),
        }
        if include_ui_checks:
            payload["check_ui_complete"] = self.check_ui_complete
            payload["check_ui_frozen"] = self.check_ui_frozen
        return payload


@dataclass
class CustomConfig:
    cycles: int
    steps: list[CustomStep] = field(default_factory=list)
    page_settle_ms: int = 0
    step_interval_ms: int = 0
    media_interval_ms: int = 0
    cycle_interval_ms: int = 0
    photo_check_mode: int = CHECK_BASELINE
    photo_check_every_cycles: int = 0
    video_check_mode: int = CHECK_BASELINE
    video_check_every_cycles: int = 0
    photo_cleanup_every_cycles: int = 0
    video_cleanup_every_cycles: int = 0
    cleanup_before_wait_index: int = 0
    cleanup_between_wait_index: int = 0
    cleanup_after_wait_index: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], capabilities: CustomCapabilities) -> "CustomConfig":
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raise CustomConfigError("device returned an invalid saved step list")
        steps: list[CustomStep] = []
        for raw in raw_steps:
            if not isinstance(raw, Mapping):
                raise CustomConfigError("device returned an invalid saved step")
            values = {
                key: raw.get(key, 0)
                for key in (
                    "action", "page", "arg0", "arg1", "arg2", "page_wait_mode",
                    "step_interval_ms", "run_once", "video_canvas",
                    "check_ui_complete", "check_ui_frozen",
                )
            }
            params = raw.get("params", [0] * CUSTOM_STEP_PARAM_COUNT)
            if (not all(isinstance(value, int) and not isinstance(value, bool) for value in values.values()) or
                    not isinstance(params, list) or len(params) != CUSTOM_STEP_PARAM_COUNT or
                    not all(isinstance(value, int) and not isinstance(value, bool) for value in params)):
                raise CustomConfigError("device returned a non-integer saved step")
            steps.append(CustomStep(**values, params=tuple(params)))
        config = cls(
            cycles=int(payload.get("cycles", 0)), steps=steps,
            page_settle_ms=int(payload.get("page_settle_ms", 0)),
            step_interval_ms=int(payload.get("step_interval_ms", 0)),
            media_interval_ms=int(payload.get("media_interval_ms", 0)),
            cycle_interval_ms=int(payload.get("cycle_interval_ms", 0)),
            photo_check_mode=int(payload.get("photo_check_mode", CHECK_BASELINE)),
            photo_check_every_cycles=int(payload.get("photo_check_every_cycles", 0)),
            video_check_mode=int(payload.get("video_check_mode", CHECK_BASELINE)),
            video_check_every_cycles=int(payload.get("video_check_every_cycles", 0)),
            photo_cleanup_every_cycles=int(payload.get("photo_cleanup_every_cycles", 0)),
            video_cleanup_every_cycles=int(payload.get("video_cleanup_every_cycles", 0)),
            cleanup_before_wait_index=int(payload.get("cleanup_before_wait_index", 0)),
            cleanup_between_wait_index=int(payload.get("cleanup_between_wait_index", 0)),
            cleanup_after_wait_index=int(payload.get("cleanup_after_wait_index", 0)),
        )
        validate_config(config, capabilities)
        return config

    def as_payload(self) -> Dict[str, Any]:
        return {
            "cycles": self.cycles,
            "steps": [step.as_payload() for step in self.steps],
            "page_settle_ms": self.page_settle_ms,
            "step_interval_ms": self.step_interval_ms,
            "media_interval_ms": self.media_interval_ms,
            "cycle_interval_ms": self.cycle_interval_ms,
            "photo_check_mode": self.photo_check_mode,
            "photo_check_every_cycles": self.photo_check_every_cycles,
            "video_check_mode": self.video_check_mode,
            "video_check_every_cycles": self.video_check_every_cycles,
            "photo_cleanup_every_cycles": self.photo_cleanup_every_cycles,
            "video_cleanup_every_cycles": self.video_cleanup_every_cycles,
            "cleanup_before_wait_index": self.cleanup_before_wait_index,
            "cleanup_between_wait_index": self.cleanup_between_wait_index,
            "cleanup_after_wait_index": self.cleanup_after_wait_index,
        }


def _require_option(value: int, options: Iterable[int], name: str) -> None:
    if value not in options:
        raise CustomConfigError(f"{name} is not supported by the selected device")


def _validate_cycle_frequency(value: int, cycles: int, name: str) -> None:
    if not isinstance(value, int) or value < 0 or value > cycles:
        raise CustomConfigError(f"{name} must be 0 or between 1 and the cycle count")


def validate_config(config: CustomConfig, capabilities: CustomCapabilities) -> None:
    if not capabilities.cycles_range[0] <= config.cycles <= capabilities.cycles_range[1]:
        raise CustomConfigError("cycle count is outside the device-supported range")
    if not 1 <= len(config.steps) <= capabilities.max_steps:
        raise CustomConfigError("a custom test needs between 1 and the device step limit")
    for field, options in capabilities.interval_options.items():
        _require_option(int(getattr(config, field)), options, field)
    for field in (
        "cleanup_before_wait_index", "cleanup_between_wait_index", "cleanup_after_wait_index",
    ):
        value = int(getattr(config, field))
        if not 0 <= value < len(capabilities.cleanup_wait_options):
            raise CustomConfigError(f"{field} is not supported by the selected device")
    _require_option(config.photo_check_mode, capabilities.photo_check_modes, "photo check mode")
    _require_option(config.video_check_mode, capabilities.video_check_modes, "video check mode")
    for field in (
        "photo_check_every_cycles", "video_check_every_cycles",
        "photo_cleanup_every_cycles", "video_cleanup_every_cycles",
    ):
        _validate_cycle_frequency(int(getattr(config, field)), config.cycles, field)
    if config.photo_check_mode == CHECK_BASELINE and config.photo_check_every_cycles != 0:
        raise CustomConfigError("photo check frequency requires FILE mode")
    if config.video_check_mode == CHECK_BASELINE and config.video_check_every_cycles != 0:
        raise CustomConfigError("video check frequency requires FILE or PLAYBACK mode")
    if ((config.photo_check_mode != CHECK_BASELINE and config.photo_check_every_cycles == 0) or
            (config.video_check_mode != CHECK_BASELINE and config.video_check_every_cycles == 0)):
        raise CustomConfigError("FILE/PLAYBACK mode requires a check frequency")
    if ((config.photo_cleanup_every_cycles or config.video_cleanup_every_cycles) and
            (not capabilities.cleanup_supported or not capabilities.media_manifest_supported)):
        raise CustomConfigError("the device cannot safely clean media for this custom test")
    if ((config.photo_check_mode == CHECK_FILE or config.video_check_mode == CHECK_FILE) and
            not capabilities.media_manifest_supported):
        raise CustomConfigError("the device cannot safely identify media files for FILE checking")
    if (config.video_check_mode in (CHECK_PLAYBACK, CHECK_PLAYBACK_DAMAGE) and
            not capabilities.media_manifest_supported):
        raise CustomConfigError("the device cannot safely identify recorded videos for playback checking")

    has_photo_step = any(step.action == ACTION_PHOTO_CAPTURE for step in config.steps)
    has_video_step = any(
        step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD) for step in config.steps
    )
    has_repeating_photo_step = any(
        step.action == ACTION_PHOTO_CAPTURE and step.run_once == 0
        for step in config.steps
    )
    has_repeating_video_step = any(
        step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD) and step.run_once == 0
        for step in config.steps
    )
    if ((config.photo_check_mode != CHECK_BASELINE or config.photo_cleanup_every_cycles) and
            not has_photo_step):
        raise CustomConfigError("photo media policy requires a PHOTO_CAPTURE step")
    if (config.video_check_mode in (CHECK_PLAYBACK, CHECK_PLAYBACK_DAMAGE) and
            not has_video_step):
        raise CustomConfigError("playback checking requires a video record step")
    if ((config.video_check_mode != CHECK_BASELINE or config.video_cleanup_every_cycles) and
            not has_video_step):
        raise CustomConfigError("video media policy requires a VIDEO_RECORD step")
    tracked_artifacts = custom_c01_max_tracked_media_artifacts(config)
    if tracked_artifacts > CUSTOM_C01_MAX_TRACKED_MEDIA_ARTIFACTS:
        raise CustomConfigError(
            "media tracking window contains "
            f"{tracked_artifacts} files; shorten the cleanup interval or reduce media steps "
            f"(limit {CUSTOM_C01_MAX_TRACKED_MEDIA_ARTIFACTS})"
        )
    if (config.photo_check_mode != CHECK_BASELINE and not has_repeating_photo_step and
            config.photo_check_every_cycles != 1):
        raise CustomConfigError(
            "first-cycle-only photo capture requires check frequency 1"
        )
    if (config.video_check_mode != CHECK_BASELINE and not has_repeating_video_step and
            config.video_check_every_cycles != 1):
        raise CustomConfigError(
            "first-cycle-only video record requires check frequency 1"
        )

    for index, step in enumerate(config.steps, start=1):
        if not isinstance(step.action, int) or isinstance(step.action, bool):
            raise CustomConfigError(f"step {index} action must be an integer")
        step_values = (
            step.page, step.arg0, step.arg1, step.arg2,
            step.page_wait_mode, step.step_interval_ms,
            step.run_once, step.video_canvas,
            step.check_ui_complete, step.check_ui_frozen,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) for value in step_values):
            raise CustomConfigError(f"step {index} contains a non-integer field")
        if step.check_ui_complete not in (0, 1) or step.check_ui_frozen not in (0, 1):
            raise CustomConfigError(f"step {index} has invalid UI check flags")
        if step.check_ui_complete and not capabilities.supports_ui_complete:
            raise CustomConfigError(
                f"step {index} UI completeness check is not supported by the selected device"
            )
        if step.check_ui_frozen and not capabilities.supports_ui_frozen:
            raise CustomConfigError(
                f"step {index} UI frozen check is not supported by the selected device"
            )
        if (not isinstance(step.params, tuple) or len(step.params) != CUSTOM_STEP_PARAM_COUNT or
                any(not isinstance(value, int) or isinstance(value, bool) for value in step.params)):
            raise CustomConfigError(f"step {index} has invalid action parameters")
        if step.action not in capabilities.actions:
            raise CustomConfigError(f"step {index} action is not supported by the selected device")
        if step.page_wait_mode not in capabilities.page_wait_modes:
            raise CustomConfigError(f"step {index} has an unsupported page wait mode")
        _require_option(step.step_interval_ms,
                        capabilities.interval_options["step_interval_ms"],
                        f"step {index} execution interval")
        if step.run_once not in (0, 1):
            raise CustomConfigError(f"step {index} has an invalid first-cycle-only setting")
        if step.video_canvas not in (VIDEO_CANVAS_LANDSCAPE, VIDEO_CANVAS_PORTRAIT):
            raise CustomConfigError(f"step {index} has an unsupported recording orientation")
        if step.action not in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD) and step.video_canvas != VIDEO_CANVAS_LANDSCAPE:
            raise CustomConfigError(f"step {index} recording orientation only applies to video recording")
        if step.action != ACTION_SLOW_MOTION_RECORD and any(step.params):
            raise CustomConfigError(f"step {index} action parameters are reserved for slow motion")
        if step.action == ACTION_NAV:
            if step.page not in capabilities.safe_pages:
                raise CustomConfigError(f"step {index} has an unsupported target page")
            if step.arg0 != 0 or step.arg1 != 0 or step.arg2 != 0:
                raise CustomConfigError(f"step {index} NAV parameters must be zero")
        elif step.action == ACTION_NAV_TARGET:
            if step.page not in capabilities.page_targets:
                raise CustomConfigError(f"step {index} has an unsupported semantic page target")
            if step.arg0 != 0 or step.arg1 != 0 or step.arg2 != 0:
                raise CustomConfigError(f"step {index} NAV_TARGET parameters must be zero")
        elif step.action == ACTION_MODE:
            if step.arg0 not in capabilities.mode_options:
                raise CustomConfigError(f"step {index} has an unsupported capture mode")
            if step.page != 0 or step.arg1 != 0 or step.arg2 != 0:
                raise CustomConfigError(f"step {index} MODE accepts only arg0")
        elif step.action == ACTION_VIDEO:
            if step.arg0 not in capabilities.video_presets:
                raise CustomConfigError(f"step {index} has an unsupported video preset")
            if step.page != 0 or step.arg1 != 0 or step.arg2 != 0:
                raise CustomConfigError(f"step {index} VIDEO accepts only arg0")
        elif step.action == ACTION_VERIFY:
            if step.arg0 not in capabilities.verify_options:
                raise CustomConfigError(f"step {index} has an unsupported verify option")
            if step.arg0 == 0:
                if step.page not in capabilities.safe_pages:
                    raise CustomConfigError(f"step {index} has an unsupported verify page")
            elif step.page != 0:
                raise CustomConfigError(f"step {index} verify page must be zero for this option")
            if step.arg1 != 0 or step.arg2 != 0:
                raise CustomConfigError(f"step {index} VERIFY parameters must be zero")
        elif step.action in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER):
            policy = capabilities.policy_steps.get(step.arg0)
            if policy is None or policy["action"] != step.action:
                raise CustomConfigError(f"step {index} is not an authorized policy action")
            if step.page != policy.get("page", 0):
                raise CustomConfigError(f"step {index} policy source page does not match the device capability")
            if step.arg1 != 0 or step.arg2 != 0:
                raise CustomConfigError(f"step {index} policy parameters must be zero")
        elif step.action == ACTION_VIDEO_RECORD:
            if step.page != 0:
                raise CustomConfigError(f"step {index} VIDEO_RECORD page must be zero")
            profile = capabilities.video_profiles.get((step.video_canvas, step.arg0))
            if profile is None or step.arg1 not in profile.fps:
                raise CustomConfigError(f"step {index} has an unsupported resolution/frame-rate pair")
            if not capabilities.record_seconds_range[0] <= step.arg2 <= capabilities.record_seconds_range[1]:
                raise CustomConfigError(f"step {index} has an unsupported recording duration")
        elif step.action == ACTION_SLOW_MOTION_RECORD:
            if (step.page != 0 or step.arg0 != 0 or step.arg1 != 0 or step.arg2 != 0 or
                    step.video_canvas != VIDEO_CANVAS_LANDSCAPE):
                raise CustomConfigError(f"step {index} slow-motion fields outside params must be zero")
            if step.params[0] not in capabilities.slow_motion_resolutions:
                raise CustomConfigError(f"step {index} has an unsupported slow-motion resolution")
            if step.params[1] not in capabilities.slow_motion_rates:
                raise CustomConfigError(f"step {index} has an unsupported slow-motion rate")
            record_range = capabilities.slow_motion_record_seconds_range
            if record_range is None or not record_range[0] <= step.params[2] <= record_range[1]:
                raise CustomConfigError(f"step {index} has an unsupported slow-motion recording duration")
            if step.params[3] != 0:
                raise CustomConfigError(f"step {index} slow-motion reserved parameter must be zero")
        elif step.action == ACTION_SCREEN_ROTATE:
            if (step.page != 0 or step.arg0 not in (0, 1) or step.arg1 != 0 or
                    step.arg2 != 0 or step.video_canvas != VIDEO_CANVAS_LANDSCAPE or
                    any(step.params)):
                raise CustomConfigError(
                    f"step {index} screen rotation accepts only arg0=0/1 and zero reserved fields"
                )
        elif step.action == ACTION_PHOTO_CAPTURE:
            if step.page or step.arg0 or step.arg1 or step.arg2:
                raise CustomConfigError(f"step {index} photo capture must not carry parameters")
    if config.cycles > 1 and not any(step.run_once == 0 for step in config.steps):
        raise CustomConfigError("multiple cycles require at least one repeating step")
    capture_requested = has_photo_step or has_video_step
    if (capture_requested and capabilities.available_storage_bytes is not None and
            capabilities.safe_free_storage_bytes is not None):
        required_bytes = capabilities.safe_free_storage_bytes + custom_c01_storage_budget_bytes(
            config, capabilities,
        )
        if capabilities.available_storage_bytes < required_bytes:
            raise CustomConfigError(
                "the device does not have enough free storage for media retained before cleanup "
                f"({capabilities.available_storage_bytes // CUSTOM_C01_STORAGE_MIB} MiB available; "
                f"{(required_bytes + CUSTOM_C01_STORAGE_MIB - 1) // CUSTOM_C01_STORAGE_MIB} MiB required)"
            )


def make_set_custom_config_payload(
    config: CustomConfig, capabilities: CustomCapabilities, base_revision: int,
) -> Dict[str, Any]:
    validate_config(config, capabilities)
    if not isinstance(base_revision, int) or base_revision < 0:
        raise CustomConfigError("base revision is invalid")
    uses_ui_checks = any(
        step.check_ui_complete or step.check_ui_frozen for step in config.steps
    )
    if uses_ui_checks and capabilities.config_version < 8:
        raise CustomConfigError(
            "the selected device uses custom config protocol v7 and cannot accept UI check options"
        )
    wire_version = 8 if uses_ui_checks else capabilities.config_version
    if wire_version not in SUPPORTED_CUSTOM_CONFIG_VERSIONS:
        raise CustomConfigError("device custom config protocol version is unsupported")
    return {
        "cmd": "set_custom_config",
        "config_version": wire_version,
        "base_revision": base_revision,
        **{
            **config.as_payload(),
            "steps": [
                step.as_payload(include_ui_checks=wire_version >= 8)
                for step in config.steps
            ],
        },
    }


def make_get_custom_config_payload() -> Dict[str, Any]:
    """Request the canonical config using the current PC feature set."""
    return {
        "cmd": "get_custom_config",
        "supports_playback_damage_check": True,
    }


def saved_config_from_reply(
    reply: Mapping[str, Any], capabilities: CustomCapabilities,
) -> tuple[CustomConfig, int, int | None, int | None]:
    if reply.get("code") != 0:
        raise CustomConfigError(str(reply.get("msg", "get_custom_config failed")))
    payload = reply.get("canonical_config", reply.get("config"))
    if not isinstance(payload, Mapping):
        raise CustomConfigError("device did not return a saved custom configuration")
    config = CustomConfig.from_payload(payload, capabilities)
    revision = reply.get("config_revision")
    if not isinstance(revision, int) or revision <= 0:
        raise CustomConfigError("device returned an invalid custom config revision")
    crc = reply.get("config_crc")
    runtime = reply.get("estimated_runtime_ms")
    return config, revision, crc if isinstance(crc, int) else None, runtime if isinstance(runtime, int) else None


_DEVICE_REASON_CODE_MESSAGES: dict[str, str] = {
    # Values MUST mirror the firmware enumeration; see
    # custom_test/custom_config.cpp (validation_failure call sites) and
    # custom_test/testagent_ui_bridge.cpp (bridge-side checks).
    "PAGE_MUST_BE_ZERO": "设备要求该步骤的 page 参数为 0，请清除页面值后重试。",
    "WIRE_STEP_INVALID": "设备拒绝该步的动作类型或参数组合，请修改步骤后重试。",
    "STORAGE_UNAVAILABLE": "设备剩余存储空间不足或不可用，请清理 SD 卡后重试。",
    "STORAGE_BUDGET_EXHAUSTED": "当前可用空间不足以容纳下一次清理前可能产生的媒体，请缩短录像、降低循环数或提高清理频率。",
    "CONFIG_REVISION_CONFLICT": "设备配置已被其它窗口更新，请重新读取后再保存。",
    "CONFIG_SAVE_FAILED": "设备保存配置失败，请重试或在设备端确认存储状态。",
    "STEP_COUNT_RANGE": "测试步骤数量超出当前固件允许范围，请调整步骤数。",
    "CYCLES_RANGE": "循环次数超出当前固件允许范围，请调整循环次数。",
    "TIMING_UNSUPPORTED": "页面等待或步骤间隔等时序参数不被当前固件支持。",
    "MEDIA_CHECK_MODE_UNSUPPORTED": "媒体检查模式不被当前固件支持，请重新选择。",
    "MEDIA_FREQUENCY_UNSUPPORTED": "媒体检查频率不被当前固件支持，请调整检查频率。",
    "MEDIA_MODE_FREQUENCY_CONFLICT": "媒体检查模式与检查频率的组合不被当前固件支持。",
    "CLEANUP_UNSUPPORTED": "媒体清理策略不被当前固件支持，请重新选择。",
    "POLICY_VERSION_UNSUPPORTED": "设备端操作策略版本不匹配，请重新读取配置后再保存。",
    "RESERVED_FIELD_NONZERO": "配置元数据保留字段非零，请重新生成方案后重试。",
    "RUNTIME_BUDGET_EXCEEDED": "方案预计执行时长超出设备限制（约 30 天），请精简方案。",
    "PAGE_WAIT_MODE_UNSUPPORTED": "页面等待模式不被当前固件支持，请重新选择。",
    "STEP_CADENCE_ORIENTATION_UNSUPPORTED": "执行后等待时间或录像横竖屏参数不被当前固件支持。",
    "STEP_INVALID": "该操作或参数组合不被当前固件允许，请删除后重新添加该步骤或选择其它操作。",
    "VIDEO_FPS_UNSUPPORTED": "当前设备能力不支持所选视频帧率，请重新读取设备能力后选择。",
    "VIDEO_PRESET_UNSUPPORTED": "当前设备能力不支持所选视频规格，请重新读取设备能力后选择。",
    "POLICY_STEP_UNAUTHORIZED": "该步骤的操作不在设备授权列表中，请更换操作类型后重试。",
    "REPEATING_STEP_REQUIRED": "多轮检查要求方案中至少有一个会重复执行的步骤。",
    "PHOTO_STEP_REQUIRED": "照片检查策略要求方案中至少包含一个拍照步骤。",
    "VIDEO_STEP_REQUIRED": "视频检查/录制策略要求方案中至少包含一个录像步骤。",
    "MEDIA_TRACKING_WINDOW_EXCEEDED": "下一次清理前需要追踪的媒体数量超过设备安全上限，请缩短清理间隔或减少媒体步骤。",
    "FIRST_CYCLE_MEDIA_FREQUENCY": "仅首轮执行媒体检查时，检查频率必须为 1。",
    "VALIDATION_ERROR": "设备拒绝该配置，请检查步骤与参数后重试。",
}


def _reason_message(reason: str) -> str | None:
    """Return the actionable message for an exact device reason_code (or None)."""
    text = _DEVICE_REASON_CODE_MESSAGES.get(reason)
    if text is not None:
        return text
    for key, candidate in _DEVICE_REASON_CODE_MESSAGES.items():
        if candidate == "VALIDATION_ERROR":
            continue
        if key in reason:
            return candidate
    return None


def custom_config_error_text(
    message: str, structured: Mapping[str, Any] | None = None,
) -> str:
    """Turn expected custom-configuration protocol errors into actionable PC-side text.

    ``structured`` is the failed device reply; its ``reason_code``/``step_index``/
    ``field``/``actual``/``allowed`` fields (when present) take precedence over
    text-only parsing of ``message``.
    """
    if structured:
        reason = str(structured.get("reason_code") or "").upper()
        step_index = structured.get("step_index")
        field = structured.get("field")
        actual = structured.get("actual")
        allowed = structured.get("allowed")
        detail: list[str] = []
        if isinstance(step_index, int):
            detail.append(f"第 {step_index} 步")
        if field:
            detail.append(f"字段 {field}")
        if actual is not None:
            detail.append(f"实际值 {actual}")
        if allowed not in (None, ""):
            detail.append(f"允许值 {allowed}")
        detail_suffix = "（" + "，".join(detail) + "）" if detail else ""
        if reason:
            text = _reason_message(reason)
            if text is None:
                text = f"设备拒绝了配置：{reason}"
            return text + detail_suffix
        if detail:
            return "设备拒绝配置：" + "，".join(detail) + "。"
        return str(message or "custom configuration request failed")
    text = str(message or "custom configuration request failed")
    lowered = text.lower()
    invalid_step = re.search(r"step\s+(\d+)\s+is\s+invalid:\s*(.+)", text, re.IGNORECASE)
    if invalid_step:
        step_number, reason = invalid_step.groups()
        reason_text = reason.strip().lower()
        if reason_text == "action is not allowed":
            return (
                f"设备拒绝第 {step_number} 步：该操作或参数组合不被当前固件允许。"
                "请删除后重新添加该步骤，或选择其它操作。"
            )
        if "cadence or orientation" in reason_text:
            return f"设备拒绝第 {step_number} 步：执行后等待时间或录像横竖屏参数不被当前固件支持。"
        if "not authorized" in reason_text:
            return f"设备拒绝第 {step_number} 步：该点击、滑动或滑杆操作不在设备授权列表中。"
        if "video frame rate" in reason_text or "video preset" in reason_text:
            return f"设备拒绝第 {step_number} 步：当前设备能力不支持所选视频帧率或规格，请重新读取设备能力后选择。"
        if "policy version" in reason_text:
            return f"设备拒绝第 {step_number} 步：设备端操作策略版本不匹配，请重新读取配置后再保存。"
        return f"设备拒绝第 {step_number} 步：{reason.strip()}"
    if ("unknown command" in lowered or "unknown cmd" in lowered or
            "unsupported command" in lowered):
        return "设备端尚未支持自定义配置协议，请升级测试固件后重试。"
    if "does not support custom configuration" in lowered or "configuration v" in lowered:
        return "设备端不支持当前自定义配置协议，请升级测试固件后重试。"
    if "no saved configuration" in lowered or "config_not_saved" in lowered:
        return "设备尚未保存自定义配置，请先在 PC 端保存步骤。"
    if "config_conflict" in lowered or "revision" in lowered and "mismatch" in lowered:
        return "设备配置已被其它窗口更新，请重新读取后再保存。"
    if "device_busy" in lowered or "busy" in lowered:
        return "设备正在运行或处理其它命令，无法修改自定义配置。"
    if "media" in lowered and ("manifest" in lowered or "cleanup" in lowered):
        return "设备无法安全识别本次测试媒体，不能启用媒体检测或清理。"
    if "insufficient sdcard space" in lowered or "not have enough free storage" in lowered:
        return "设备剩余存储空间不足，请清理 SD 卡后重试。"
    if "needs between 1" in lowered or "step count must be" in lowered:
        return "请至少添加 1 个测试步骤。"
    if "cycle count" in lowered or "cycles must be" in lowered:
        return "循环次数不在当前固件允许的范围内。"
    if "first-cycle-only photo capture requires check frequency 1" in lowered:
        return "拍照步骤仅第一轮执行时，照片检查必须设置为每 1 轮检查。"
    if "first-cycle-only video record requires check frequency 1" in lowered:
        return "录像步骤仅第一轮执行时，视频检查必须设置为每 1 轮检查。"
    if "check frequency" in lowered or "media frequency" in lowered:
        return "“每 N 轮”必须为 0（不执行）或不大于总循环次数。"
    if "file/playback mode requires a check frequency" in lowered:
        return "选择“检查文件”或“回放检查”后，必须填写大于 0 的“每 N 轮检查”。"
    if "requires file mode" in lowered:
        return "选择“不检查”时，“每 N 轮检查”必须为 0。"
    if "playback" in lowered and ("video record" in lowered or "video_record" in lowered):
        return "选择“回放检查视频”时，步骤中必须包含“录制视频”。"
    if "photo media policy" in lowered and "photo_capture" in lowered:
        return "已设置照片检查或自动删除，但步骤中没有“拍摄照片”。请添加拍摄照片步骤，或关闭照片策略。"
    if "video media policy" in lowered and "video_record" in lowered:
        return "已设置视频检查或自动删除，但步骤中没有“录制视频”。请添加录制视频步骤，或关闭视频策略。"
    if "resolution/frame-rate" in lowered:
        return "所选分辨率和帧率组合不被当前设备固件支持，请重新选择录像参数。"
    if "video frame rate" in lowered or "video preset" in lowered:
        return "当前设备能力不支持所选视频帧率或规格，请重新读取设备能力后选择。"
    if "recording duration" in lowered:
        return "录像时长不在当前设备允许的范围内。"
    if "timing option" in lowered or "interval" in lowered:
        return "所选等待时间不被当前固件支持，请从下拉选项中重新选择。"
    if "time budget" in lowered:
        return "当前步骤、录像时长和循环次数的预计总耗时超过设备上限，请减少其中一项后重试。"
    if "multiple cycles require at least one repeating step" in lowered:
        return "总轮数大于 1 时，至少要保留一条不勾选“仅第一轮执行”的重复步骤。"
    return f"自定义配置校验失败：{text}"


def is_config_revision_conflict(structured: Mapping[str, Any] | None) -> bool:
    """Return whether a failed device reply is a safe-to-refresh C01 conflict."""
    if not structured:
        return False
    return str(structured.get("reason_code") or "").upper() == "CONFIG_REVISION_CONFLICT"

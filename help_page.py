#!/usr/bin/env python3
"""使用帮助页：介绍设备测试、App 测试、OTG 传输与自定义测试的使用流程。

纯静态说明页，无后台线程，供用户在左侧导航查看各页面的使用步骤与按钮释义。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-size:14px; font-weight:800; color:#0f172a;")
    return label


def _sub_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-size:13px; font-weight:700; color:#0f766e; margin-top:6px;")
    return label


def _body(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-size:13px; color:#334155;")
    label.setWordWrap(True)
    return label


def _help_card(title: str, lines: list[str]) -> QGroupBox:
    group = QGroupBox(title)
    layout = QVBoxLayout()
    for line in lines:
        layout.addWidget(_body(line))
    group.setLayout(layout)
    return group


def _build_device_tab() -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout()
    layout.setSpacing(10)

    layout.addWidget(_section_title("设备测试页：用于发现、连接并控制多台测试设备，执行联网/串口相关测试。"))

    layout.addWidget(_sub_title("使用流程"))
    layout.addWidget(
        _help_card(
            "步骤",
            [
                "① 点击顶部「刷新设备」：自动扫描并发现网络中的测试设备，随后会自动完成网络配置。",
                "② 在设备表中选中需要测试的一台设备，右侧会出现「设备 / 测试集 / 用例 / 进度 / 状态」详情。",
                "③ 在「测试集」「用例」下拉框选择要执行的测试项。",
                "④ 点击「开始运行」启动测试并记录状态，可随时用「停止」结束；需要单独观察设备时使用「监听」。",
                "⑤ 测试结束后可用「报告」查看结果、「记录」查看历史测试痕迹。",
                "⑥ 如需重启设备或导出故障信息，用「重启」「导出崩溃日志」；「截屏」「屏幕预览」可实时查看画面。",
                "⑦ 底部日志区默认显示当前设备日志，可通过「当前设备 / 全部设备 / 仅错误」过滤，「收起日志」可折叠。",
            ],
        )
    )

    layout.addWidget(_sub_title("按钮释义"))
    layout.addWidget(
        _help_card(
            "顶部按钮",
            [
                "刷新设备：重新扫描并自动配置发现的设备。",
                "设备连接：手动打开网络配置窗口，处理未自动配好的设备。",
                "OTA升级：对选中设备执行固件 OTA 升级。",
            ],
        )
    )
    layout.addWidget(
        _help_card(
            "命令按钮",
            [
                "开始运行 / 停止：启动并记录，或终止选中设备的测试。",
                "监听：持续监听设备状态；开始运行已经包含测试记录。",
                "状态 / 报告 / 记录：查看当前状态、生成测试报告、查看历史记录。",
                "重启：重启选中设备。",
                "截屏 / 屏幕预览 / 导出崩溃日志：截图、实时预览画面、导出崩溃日志；用例运行中也可预览。",
            ],
        )
    )
    layout.addWidget(_body("注意：退出程序时若仍有测试、OTA、录制等后台任务在运行，会被拦截，需先等待任务完成。"))
    layout.addStretch(1)
    widget.setLayout(layout)
    return widget


def _build_app_test_tab() -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout()
    layout.setSpacing(10)

    layout.addWidget(_section_title("App 测试页：在 PC 侧直接执行 apptest 后端用例并查看实时进度与日志。"))

    layout.addWidget(_sub_title("使用流程"))
    layout.addWidget(
        _help_card(
            "步骤",
            [
                "① 在「运行配置」中选择 target.yaml 目标配置文件（含设备 / 云端 / 迭代参数），可点「浏览…」手动选取。",
                "② 点「配置预检」校验配置：会检查云端地址、设备序列号、激活码、APK/固件 MD5 等关键字段是否齐备。",
                "③ 在「用例」下拉框选择要执行的用例（共 6 个）。",
                "④ 「迭代」「并发」留 0 表示采用配置中的默认值，也可手动覆盖。",
                "⑤ 点「开始运行」执行；运行中可用「停止」提前终止，进度条实时显示进度。",
                "⑥ 用例执行完成或中途失败时，「打开报告」按钮启用，点击即可在浏览器中查看 HTML 报告。",
                "⑦ 底部「实时日志」区滚动显示运行日志，输出目录在配置区展示。",
            ],
        )
    )

    layout.addWidget(_sub_title("用例说明"))
    layout.addWidget(
        _help_card(
            "用例",
            [
                "用例 1（uia）：App 视频下载与删除。",
                "用例 2（uia）：App 视频预览循环。",
                "用例 3（protocol）：APK 下载压力测试。",
                "用例 4（protocol）：固件下载压力测试。",
                "用例 5（protocol）：设备激活循环测试。",
                "用例 6（uia）：App 设备连接测试。",
            ],
        )
    )
    layout.addWidget(_body("提示：uia 用例需 Android 设备在前台运行目标 App，且需在配置中指定包名；protocol 用例需云端接口地址与密钥配置。"))
    layout.addStretch(1)
    widget.setLayout(layout)
    return widget


def _build_otg_tab() -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout()
    layout.setSpacing(10)

    layout.addWidget(_section_title("OTG 传输页：监控可移动存储，自动将源目录中的文件传输到插入的设备。"))

    layout.addWidget(_sub_title("使用流程"))
    layout.addWidget(
        _help_card(
            "步骤",
            [
                "① 在「监控设置」中填写源目录：点「浏览…」选择存放待传输文件的文件夹。",
                "② 在「盘符」下拉框选择可移动存储对应的盘符（E ~ I）。",
                "③ 点击「启动监控」开始监控；监控运行后，插入对应盘符的可移动存储即自动传输随机源文件。",
                "④ 进度信息实时显示在「已传输」计数与「传输记录」日志中。",
                "⑤ 不再需要时点「停止监控」结束；关闭窗口时若仍在监控会被拦截。",
            ],
        )
    )

    layout.addWidget(_sub_title("注意事项"))
    layout.addWidget(
        _help_card(
            "说明",
            [
                "仅当对应盘符的可移动存储出现检测完成事件时才触发一次传输，每次随机选取一个源文件。",
                "传输记录展示目标路径、文件字节数与传输速度；同一目标文件不会重复记录。",
            ],
        )
    )
    layout.addStretch(1)
    widget.setLayout(layout)
    return widget


def _build_custom_test_tab() -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout()
    layout.setSpacing(10)
    layout.addWidget(_section_title("自定义测试：在 PC 端编排 C01 步骤，保存到设备后由设备端执行。"))
    layout.addWidget(_sub_title("使用流程"))
    layout.addWidget(_help_card("步骤", [
        "① 在“设备测试”中选中已在线的设备，测试集选择“自定义测试”，用例选择 C01。",
        "② 点击“配置自定义步骤”。PC 会读取当前固件支持的步骤、全部可安全进入的页面、视频参数和保存版本。",
        "③ 配置窗口顶部可将当前编辑内容“另存为新方案”。已保存方案仅保存在 PC；可在“自定义方案”下拉框选择，或在配置窗口中载入、更新、删除。",
        "④ 选择一个操作及其参数后点击“新增步骤”。步骤按列表从上到下执行；可用“上移/下移”调整顺序，也可删除选中步骤或清空后重新配置。",
        "⑤ 每新增一条步骤时，单独设置该步骤“执行后等待”。页面操作、拍照和录像完成后，都只使用这条步骤自己的等待值；总配置只保留额外页面稳定等待和每轮结束后等待。",
        "⑥ “前置条件（可选）”可把某条步骤设为仅第一轮执行，后续轮次自动跳过。例如先进入指定页面或设置一次录像参数；多轮测试仍必须至少保留一条会重复执行的步骤。",
        "⑦ 录制视频先选择横屏 16:9 或竖屏 9:16，随后只显示该方向下由当前固件支持的分辨率和帧率。",
        "⑧ 选择媒体检查和自动删除策略，点击“保存到设备”。保存成功后 PC 会自动回读确认；也可以点击“保存并运行 C01”。只选择 PC 方案不会自动覆盖设备，必须先保存到设备。",
    ]))
    layout.addWidget(_sub_title("媒体检测与删除"))
    layout.addWidget(_help_card("字段说明", [
        "不检查（仅确认操作已发送）：不校验照片或视频文件，适合只做 UI/命令流程压力。对应“每 N 轮检查”必须为 0。",
        "检查新生成的媒体文件：设备在设定轮次确认本次测试新拍的照片或新录的视频已生成。填写 N 表示每完成 N 轮检查一次；0 表示关闭。",
        "进入回放并检查最新视频：设备确认最新视频生成后进入回放播放，再回到主界面。仅视频可用，且步骤里必须有“录制视频”。",
        "照片检查或删除必须配合“拍摄照片”步骤；视频检查、回放检查或删除必须配合“录制视频”步骤。否则 PC 和设备都会拒绝保存，避免配置看似成功但策略永远不会执行。",
        "录制视频会自动进入录像模式并应用所选横屏/竖屏方向、分辨率、帧率和时长，无需额外添加“切换拍摄模式”或“设置视频规格”步骤。",
        "每 N 轮删除照片/视频：只清理本次自定义测试新生成的对应媒体；0 表示不自动删除。N 不能大于总循环次数。",
        "删除过程中的三段等待目前由固件固定为 2 秒，PC 不提供无效的修改入口。",
    ]))
    layout.addWidget(_sub_title("常见提示"))
    layout.addWidget(_help_card("如何处理", [
        "“设备拒绝第 N 步”：该步骤的操作或参数组合不被当前测试固件允许。页面无关的拍照、录像、模式和规格步骤会由测试桥自动使用设备内部页面值；仍被拒绝时，请删除该步骤后重新添加，或改用其它设备已列出的操作。",
        "“每 N 轮”提示超出范围：先把总循环次数调到不小于 N，再保存。当前固件的上限以配置窗口“循环次数”为准。",
        "“保存配置超时”：检查设备仍在线；重新打开配置窗口后再保存，避免与正在运行的用例同时修改。",
    ]))
    layout.addWidget(_sub_title("推荐示例"))
    layout.addWidget(_help_card("录像耐久", [
        "步骤：录制视频（4K / 30fps / 10 秒）。",
        "循环：15 轮；视频检测方式选择“检查新生成的媒体文件”，每 1 轮检查一次；每 5 轮删除视频。",
        "如设备切换页面较慢，仅在相关步骤选择“额外等待页面稳定”，再设置对应等待时长；不要无差别给所有步骤增加等待。",
    ]))
    layout.addStretch(1)
    widget.setLayout(layout)
    return widget


class HelpPage(QWidget):
    """使用帮助页：汇集设备测试 / 自定义测试 / App 测试 / OTG 传输说明。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout()
        outer.setSpacing(10)

        top_bar = QHBoxLayout()
        title = QLabel("使用帮助")
        title.setStyleSheet("font-size:15px; font-weight:800; color:#0f172a;")
        top_bar.addWidget(title)
        top_bar.addStretch(1)
        hint = QLabel("点击左侧导航切换功能页，并可在各标签页查看对应使用说明")
        hint.setStyleSheet("font-size:11px; color:#94a3b8;")
        top_bar.addWidget(hint)
        outer.addLayout(top_bar)

        tabs = QTabWidget()
        tabs.addTab(_build_device_tab(), "设备测试")
        tabs.addTab(_build_custom_test_tab(), "自定义测试")
        tabs.addTab(_build_app_test_tab(), "App 测试")
        tabs.addTab(_build_otg_tab(), "OTG 传输")
        outer.addWidget(tabs, 1)

        self.setLayout(outer)

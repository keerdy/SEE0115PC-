from apptest.mobile_uia.case6 import _find_device_by_name


class FakeDevice:
    def __init__(self) -> None:
        self.swipes = 0

    def dump_hierarchy(self, compressed: bool = False) -> str:
        return (
            '<hierarchy><node content-desc="连接"/>'
            '<node content-desc="Gimbal Camera-123456\\n点击连接设备"/>'
            '</hierarchy>'
        )

    def swipe(self, *args) -> None:
        self.swipes += 1


def test_device_card_match_accepts_gimbal_full_name() -> None:
    device = FakeDevice()
    assert _find_device_by_name(device, "gimbal camera-123456", max_swipes=1)
    assert device.swipes == 0

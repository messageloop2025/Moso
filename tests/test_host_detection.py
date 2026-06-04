"""主机类型检测（含 ESXi）单元测试。"""

from __future__ import annotations

import unittest

from services.host_detection import (
    _esxi_version_from_os_release,
    _parse_esxcli_version_output,
    _parse_esxi_uname_release,
    _parse_vmware_v_output,
)


class TestEsxiDetection(unittest.TestCase):
    def test_vmware_v(self):
        text = "VMware ESXi 8.0.2 build-22380479\n"
        self.assertIn("8.0.2", _parse_vmware_v_output(text))
        self.assertIn("22380479", _parse_vmware_v_output(text))

    def test_esxcli_version(self):
        text = """
Product Name: VMware ESXi
Version: 7.0.3
Build: Releasebuild-19193900
"""
        out = _parse_esxcli_version_output(text)
        self.assertIn("7.0.3", out)

    def test_os_release_vmware_esxi(self):
        lines = [
            'NAME="VMware ESXi"',
            "VERSION=7.0.3",
            "ID=vmware-esxi",
            'PRETTY_NAME="VMware ESXi 7.0.3"',
        ]
        self.assertIn("7.0.3", _esxi_version_from_os_release(lines))

    def test_uname_release(self):
        self.assertIn("7.0", _parse_esxi_uname_release("7.0.3-0.0.19193900"))


if __name__ == "__main__":
    unittest.main()

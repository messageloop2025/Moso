"""host_duplicate 归一化与判重规则单元测试。"""

from __future__ import annotations

import unittest

from services.host_duplicate import normalize_host_address, normalize_host_port


class TestHostDuplicateNormalize(unittest.TestCase):
    def test_address_strip(self):
        self.assertEqual(normalize_host_address("  10.0.0.1  "), "10.0.0.1")

    def test_port_default_22(self):
        self.assertEqual(normalize_host_port(None), 22)
        self.assertEqual(normalize_host_port(""), 22)

    def test_port_explicit(self):
        self.assertEqual(normalize_host_port(2222), 2222)

    def test_different_ports_not_same_value(self):
        self.assertNotEqual(normalize_host_port(22), normalize_host_port(2222))


if __name__ == "__main__":
    unittest.main()

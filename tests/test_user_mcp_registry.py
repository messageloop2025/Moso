"""user_mcp_registry 脱敏与占位符单元测试。"""

from __future__ import annotations

import unittest

from services.user_mcp_registry import (
    is_masked_secret_placeholder,
    mask_secret_value,
)


class TestMcpSecretMask(unittest.TestCase):
    def test_mask_bearer_dashscope_key(self):
        raw = "Bearer sk-abc123def456789"
        self.assertEqual(mask_secret_value(raw), "Bearer sk-****")

    def test_mask_sk_prefix_without_bearer(self):
        self.assertEqual(mask_secret_value("sk-live-xyz"), "sk-****")

    def test_mask_empty(self):
        self.assertEqual(mask_secret_value(""), "")
        self.assertEqual(mask_secret_value("   "), "")

    def test_placeholder_detection(self):
        self.assertTrue(is_masked_secret_placeholder(""))
        self.assertTrue(is_masked_secret_placeholder("***"))
        self.assertTrue(is_masked_secret_placeholder("Bearer sk-****"))
        self.assertTrue(is_masked_secret_placeholder("sk-****"))
        self.assertFalse(is_masked_secret_placeholder("Bearer sk-newkey123"))


if __name__ == "__main__":
    unittest.main()

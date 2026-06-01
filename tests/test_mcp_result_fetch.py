"""MCP 结果外链拉取单元测试。"""

from __future__ import annotations

import json
import unittest

from services.mcp_result_fetch import extract_fetchable_urls


class TestMcpResultFetch(unittest.TestCase):
    def test_extract_from_nested_content_json(self):
        inner = {
            "request_id": "abc",
            "results": [
                {
                    "url": "https://dashscope-result-wlcb.oss-cn-wulanchabu.aliyuncs.com/a/b/test.png?Expires=1&Signature=xyz"
                }
            ],
        }
        payload = {
            "success": True,
            "content": json.dumps(inner, ensure_ascii=False),
        }
        urls = extract_fetchable_urls(payload)
        self.assertEqual(len(urls), 1)
        self.assertIn("test.png", urls[0])

    def test_ignores_non_image_http(self):
        payload = {"success": True, "content": "see https://example.com/page.html for docs"}
        self.assertEqual(extract_fetchable_urls(payload), [])


if __name__ == "__main__":
    unittest.main()

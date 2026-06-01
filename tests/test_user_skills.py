"""Agent Skills registry / runtime 单元测试。"""

from __future__ import annotations

import unittest

from services.user_skills_registry import (
    collect_description_warnings,
    is_placeholder_description,
    looks_like_agent_skill_fs_path,
    normalize_skill_file_content,
    parse_skill_markdown,
    read_skill_resource_file,
    render_skill_markdown,
    skill_should_always_apply,
    write_skill_resource_file,
    delete_skill_resource_file,
)
from services.user_skills_runtime import _format_skill_body_block


class TestSkillFrontmatter(unittest.TestCase):
    def test_skill_should_always_apply_cursor_defaults(self):
        meta, _ = parse_skill_markdown(render_skill_markdown(name="demo-skill", description="Does X. Use when Y."))
        self.assertFalse(skill_should_always_apply(meta))

    def test_always_apply_true(self):
        md = """---
name: demo
always-apply: true
description: Test skill.
---
# Demo
"""
        meta, _ = parse_skill_markdown(md)
        self.assertTrue(skill_should_always_apply(meta))

    def test_disable_model_invocation_false(self):
        md = """---
name: demo
disable-model-invocation: false
description: Inline skill.
---
"""
        meta, _ = parse_skill_markdown(md)
        self.assertTrue(skill_should_always_apply(meta))

    def test_normalize_frontmatter_name(self):
        raw = """---
name: wrong-name
description: Hello world for testing purposes here.
---
# Body
"""
        out = normalize_skill_file_content(raw, "right-name")
        self.assertIn("name: right-name", out)
        self.assertIn("# Body", out)

    def test_placeholder_description(self):
        self.assertTrue(is_placeholder_description(""))
        self.assertTrue(is_placeholder_description("Use when the user mentions …"))
        self.assertFalse(is_placeholder_description("Deploys nginx. Use when user asks for nginx."))

    def test_collect_warnings_underscore(self):
        w = collect_description_warnings("my_skill", "Does things. Use when needed.")
        self.assertTrue(any("下划线" in x for x in w))


class TestSkillResources(unittest.TestCase):
    def test_read_skill_resource_rejects_traversal(self):
        user = {"id": 1, "username": "_test_skills_user"}
        with self.assertRaises(ValueError):
            read_skill_resource_file(user, "my-skill", "../secret.txt")

    def test_looks_like_agent_skill_fs_path(self):
        self.assertTrue(looks_like_agent_skill_fs_path("skills/my-skill/reference.md"))
        self.assertTrue(looks_like_agent_skill_fs_path("skills"))
        self.assertTrue(looks_like_agent_skill_fs_path("chats/2026/05/31/skills/foo/reference.md"))
        self.assertFalse(looks_like_agent_skill_fs_path("chats/2026/05/31/scripts/job.sh"))
        self.assertFalse(looks_like_agent_skill_fs_path("data/output.csv"))

    def test_write_skill_resource_rejects_skill_md(self):
        user = {"id": 1, "username": "_test_skills_user"}
        with self.assertRaises(ValueError):
            write_skill_resource_file(user, "demo-skill", "SKILL.md", "# body")

    def test_delete_skill_resource_rejects_skill_md(self):
        user = {"id": 1, "username": "_test_skills_user"}
        with self.assertRaises(ValueError):
            delete_skill_resource_file(user, "demo-skill", "SKILL.md")


class TestRuntimeHelpers(unittest.TestCase):
    def test_format_skill_body_truncates(self):
        block = _format_skill_body_block("T", "n", "d", "x" * 100, 20)
        self.assertIn("截断", block)


if __name__ == "__main__":
    unittest.main()

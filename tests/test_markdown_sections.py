"""Markdown 章节解析单元测试。"""

from __future__ import annotations

import unittest

from services.markdown_sections import (
    get_markdown_section,
    list_markdown_sections,
    parse_headings,
    read_markdown_document,
    replace_markdown_section,
    search_markdown_sections,
)


SAMPLE = """# Root

Intro paragraph.

## Alpha

Alpha body.

### Alpha one

Detail one.

## Beta

Beta body.
"""


class TestMarkdownSections(unittest.TestCase):
    def test_parse_skips_fence(self):
        md = "# Real\n\n```\n# fake\n```\n\n## Child\n"
        hs = parse_headings(md)
        self.assertEqual(len(hs), 2)
        self.assertEqual(hs[1].title, "Child")

    def test_list_max_level(self):
        out = list_markdown_sections(SAMPLE, max_level=2)
        titles = [s["title"] for s in out["sections"]]
        self.assertEqual(titles, ["Root", "Alpha", "Beta"])
        self.assertTrue(all(s["level"] <= 2 for s in out["sections"]))

    def test_get_section_by_path(self):
        out = get_markdown_section(
            SAMPLE,
            section_path=["Root", "Alpha", "Alpha one"],
            max_chars=10_000,
            include_children=False,
            include_heading=True,
        )
        self.assertIn("Detail one", out["content"])
        self.assertNotIn("## Beta", out["content"])

    def test_get_section_no_children(self):
        out = get_markdown_section(
            SAMPLE,
            heading="Alpha",
            include_children=False,
            include_heading=False,
        )
        self.assertIn("Alpha body", out["content"])
        self.assertNotIn("Detail one", out["content"])

    def test_get_truncated(self):
        out = get_markdown_section(SAMPLE, section_index=0, max_chars=80)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["returned_chars"], 80)
        self.assertGreater(out["total_chars"], 80)

    def test_replace_body(self):
        out = replace_markdown_section(
            SAMPLE,
            heading="Alpha",
            new_content="NEW alpha body.\n",
            mode="replace_body",
        )
        self.assertIn("## Alpha\nNEW alpha body", out["content"])
        self.assertIn("### Alpha one", out["content"])
        self.assertIn("## Beta", out["content"])

    def test_replace_all_removes_subsection(self):
        out = replace_markdown_section(
            SAMPLE,
            heading="Alpha",
            new_content="## Alpha\nReplaced entirely.\n",
            mode="replace_all",
        )
        self.assertIn("Replaced entirely", out["content"])
        self.assertNotIn("Alpha one", out["content"])
        self.assertIn("## Beta", out["content"])

    def test_ambiguous_heading(self):
        md = "## X\na\n## X\nb\n"
        with self.assertRaises(ValueError):
            get_markdown_section(md, heading="X")

    def test_read_document_sections_only(self):
        out = read_markdown_document(SAMPLE, sections_only=True, max_level=2)
        self.assertEqual(out["mode"], "sections")
        self.assertEqual(len(out["sections"]), 3)

    def test_search_titles(self):
        out = search_markdown_sections(SAMPLE, "Beta", scope="titles")
        self.assertEqual(out["hit_count"], 1)
        self.assertEqual(out["hits"][0]["title"], "Beta")

    def test_search_content(self):
        out = search_markdown_sections(SAMPLE, "Detail one", scope="content")
        self.assertGreaterEqual(out["hit_count"], 1)
        self.assertTrue(any("body" in h["match_in"] for h in out["hits"]))


if __name__ == "__main__":
    unittest.main()

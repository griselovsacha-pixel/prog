"""Проверки публичного слоя (seo.py): python -m unittest discover -s tests -v"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import seo  # noqa: E402

TOOLS = (Path(ROOT) / "data" / "tools.json").read_bytes()
seo.configure(tools_json=lambda: TOOLS, site="https://example.com", channel="https://t.me/+x")


class SeoTests(unittest.TestCase):
    def test_hidden_categories_not_public(self):
        d = seo.build()
        names = {t["name"] for t in d["tools"]}
        for bad in ("Ember Snos", "Rendy Snos", "Rendy Logger", "Prank Bot", "Lolz Market", "Максонов search"):
            self.assertNotIn(bad, names)
        self.assertIn("PimEyes", names)

    def test_no_tool_links_leak(self):
        html = seo.render_index() + "".join(seo.render_tool(t["slug"]) for t in seo.build()["tools"])
        self.assertNotIn("pimeyes.com", html)
        self.assertNotIn("t.me/exifksia_bot", html)

    def test_slugs_unique_and_epieos_deduped(self):
        slugs = [t["slug"] for t in seo.build()["tools"]]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(sum(1 for t in seo.build()["tools"] if t["name"] == "Epieos"), 1)

    def test_tool_page(self):
        h = seo.render_tool("pimeyes")
        self.assertIn("<title>PimEyes", h)
        self.assertIn('rel="canonical" href="https://example.com/osint/pimeyes"', h)
        self.assertIn("application/ld+json", h)
        self.assertIsNone(seo.render_tool("nope"))

    def test_home_and_categories(self):
        h = seo.render_home()
        self.assertIn("home-gate.js", h)
        self.assertIn("favicon.svg", h)
        self.assertIn("FAQPage", h)
        c = seo.build()["cats"][0]
        self.assertIn("<h1>", seo.render_category(c["slug"]))
        self.assertIsNone(seo.render_category("nope"))

    def test_broken_catalog(self):
        seo.configure(tools_json=lambda: None)
        try:
            self.assertIsNone(seo.render_index())
        finally:
            seo.configure(tools_json=lambda: TOOLS)


if __name__ == "__main__":
    unittest.main()

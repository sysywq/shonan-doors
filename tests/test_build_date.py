import os
import unittest
from datetime import datetime, date
from unittest.mock import patch

import build


class BuildDateTests(unittest.TestCase):
    def test_before_utc_midnight_uses_jst_for_sitemap_and_pickup(self):
        # Scheduled publication occurs before UTC midnight, after JST midnight.
        with patch.dict(os.environ, {build.BUILD_DATE_ENV: ""}), patch("build._datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 13, 4, 22)
            self.assertEqual(build.build_today(), date(2026, 10, 13))
            self.assertEqual(str(clock.now.call_args.args[0]), "Asia/Tokyo")
            self.assertIn("<lastmod>2026-10-13</lastmod>", build.render_sitemap([]))
            article = dict(id=89, cat="e", date="2026-09-19", eventStartDate="2026-10-12", eventEndDate="2026-10-12")
            self.assertEqual(build.select_pickup_articles([article]), [])

    def test_ci_override_is_shared_by_sitemap_and_pickup(self):
        with patch.dict(os.environ, {build.BUILD_DATE_ENV: "2026-10-12"}):
            self.assertEqual(build.build_today(), date(2026, 10, 12))
            self.assertIn("<lastmod>2026-10-12</lastmod>", build.render_sitemap([]))
            article = dict(id=89, cat="e", date="2026-09-19")
            self.assertEqual(build.select_pickup_articles([article])[0]["id"], 89)

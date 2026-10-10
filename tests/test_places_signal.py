import os
import unittest
from unittest import mock

import places_signal


class PlacesSignalTest(unittest.TestCase):
    def test_future_opening_becomes_metadata_lead(self):
        response = {
            "places": [
                {
                    "id": "place-1",
                    "displayName": {"text": "湘南テスト食堂"},
                    "formattedAddress": "神奈川県藤沢市テスト1-2-3",
                    "businessStatus": "FUTURE_OPENING",
                    "openingDate": {"year": 2026, "month": 10, "day": 20},
                    "primaryType": "restaurant",
                },
                {
                    "id": "place-2",
                    "displayName": {"text": "既存店"},
                    "businessStatus": "OPERATIONAL",
                    "primaryType": "store",
                },
            ]
        }
        with mock.patch.dict(os.environ, {
            "GOOGLE_PLACES_API_KEY": "test-key",
            "PLACES_SEARCH_TERMS": "restaurant",
            "PLACES_MAX_QUERIES": "1",
        }, clear=False), mock.patch.object(places_signal, "_post", return_value=response):
            leads = places_signal.discover_future_openings(["藤沢"])
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["subject"], "湘南テスト食堂")
        self.assertEqual(lead["openingDate"], "2026-10-20")
        self.assertEqual(lead["cat"], "g")
        self.assertEqual(lead["leadSourceType"], "google_places_future_opening")
        self.assertEqual(lead["sourceUrl"], "")
        self.assertIn("place_id:place-1", lead["leadUrl"])

    def test_missing_key_fails_soft(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(places_signal.discover_future_openings(["藤沢"]), [])


    def test_operational_baseline_then_new_id(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            state = str(Path(d) / "seen.json")
            def place(id, name):
                return {"id": id, "displayName": {"text": name},
                        "formattedAddress": "神奈川県藤沢市",
                        "businessStatus": "OPERATIONAL", "primaryType": "restaurant"}
            with mock.patch.dict(os.environ, {"GOOGLE_PLACES_API_KEY": "test-key",
                    "PLACES_SEARCH_TERMS": "restaurant", "PLACES_MAX_QUERIES": "1"}), mock.patch.object(
                    places_signal, "_post", side_effect=[
                        {"places": [place("old", "既存店")]},
                        {"places": [place("old", "既存店"), place("new", "新規観測店")]},
                        {"places": [place("old", "既存店"), place("new", "新規観測店")]},
                    ]):
                self.assertEqual(places_signal.discover_future_openings(
                    ["藤沢"], include_operational=True, state_path=state), [])
                leads = places_signal.discover_future_openings(
                    ["藤沢"], include_operational=True, state_path=state)
                self.assertEqual(len(leads), 1)
                self.assertEqual(leads[0]["placeId"], "new")
                self.assertEqual(leads[0]["leadSourceType"], "google_places_newly_observed")
                self.assertEqual(leads[0]["openingDate"], "")
                self.assertEqual(places_signal.discover_future_openings(
                    ["藤沢"], include_operational=True, state_path=state), [])
                self.assertEqual(set(__import__("json").loads(Path(state).read_text())["place_ids"]),
                                 {"old", "new"})


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the YouTube Data API fetcher and the fetch fallback.

HTTP is stubbed; no network or API key needed. Run: make test
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from media_to_nblm import fetch, select, ytapi  # noqa: E402


def _video(vid: str, views: str, published: str, title: str = "t") -> dict:
    return {
        "id": vid,
        "snippet": {
            "title": title,
            "description": f"desc {vid}",
            "publishedAt": published,
            "channelTitle": "Chan",
        },
        "statistics": {"viewCount": views},
        "contentDetails": {"duration": "PT1H2M3S"},
    }


IDS = [f"vid{n:08d}" for n in range(120)]  # 11-char ids


class FakeAPI:
    """Minimal stand-in for ytapi._get covering channels/playlistItems/videos."""

    def __init__(self, handle_needs_at: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.handle_needs_at = handle_needs_at

    def __call__(self, endpoint: str, params: dict, key: str) -> dict:
        assert key == "k"
        self.calls.append((endpoint, dict(params)))
        if endpoint == "channels":
            h = params.get("forHandle")
            ok = params.get("id") == "UC" + "x" * 22 or params.get("forUsername") == "legacy"
            ok = ok or (h == "@chan" if self.handle_needs_at else h == "chan")
            if not ok:
                return {"items": []}
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUchan"}}}]}
        if endpoint == "playlistItems":
            start = int(params.get("pageToken") or 0)
            size = params["maxResults"]
            assert 1 <= size <= 50
            chunk = IDS[start : start + size]
            data = {"items": [{"contentDetails": {"videoId": v}} for v in chunk]}
            if start + size < len(IDS):
                data["nextPageToken"] = str(start + size)
            return data
        if endpoint == "videos":
            ids = params["id"].split(",")
            assert len(ids) <= 50
            # Return out of order and drop one to check order preservation / missing ids.
            items = [_video(v, str(i * 10), "2026-09-01T10:00:00Z") for i, v in enumerate(ids) if v != "vid00000003"]
            return {"items": list(reversed(items))}
        raise AssertionError(endpoint)


class ParseURLTests(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": ("video", "dQw4w9WgXcQ"),
            "https://youtu.be/dQw4w9WgXcQ?si=abc": ("video", "dQw4w9WgXcQ"),
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": ("video", "dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123": ("video", "dQw4w9WgXcQ"),
            "https://www.youtube.com/playlist?list=PL123": ("playlist", "PL123"),
            "https://youtube.com/@ApertureThinking": ("handle", "ApertureThinking"),
            "https://www.youtube.com/@ApertureThinking/videos": ("handle", "ApertureThinking"),
            "https://www.youtube.com/channel/UC" + "x" * 22: ("channel_id", "UC" + "x" * 22),
            "https://www.youtube.com/user/legacy": ("username", "legacy"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(ytapi.parse_url(url), expected)

    def test_custom_url_unsupported(self):
        with self.assertRaises(ytapi.YouTubeAPIError):
            ytapi.parse_url("https://www.youtube.com/c/SomeName")


class NormalizeTests(unittest.TestCase):
    def test_fields(self):
        n = ytapi._normalize(_video("dQw4w9WgXcQ", "1234", "2024-05-01T12:34:56Z", "Title"))
        self.assertEqual(n["view_count"], 1234)
        self.assertEqual(n["upload_date"], "20240501")
        self.assertEqual(n["duration"], 3723)
        self.assertEqual(n["webpage_url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(n["channel"], "Chan")
        self.assertEqual(set(n), set(fetch.KEEP_FIELDS))

    def test_duration(self):
        self.assertEqual(ytapi._duration_seconds("PT45S"), 45)
        self.assertEqual(ytapi._duration_seconds("P1DT1S"), 86401)
        self.assertEqual(ytapi._duration_seconds("P0D"), 0)
        self.assertIsNone(ytapi._duration_seconds(None))
        self.assertIsNone(ytapi._duration_seconds("garbage"))

    def test_missing_views(self):
        item = _video("dQw4w9WgXcQ", "0", "2024-05-01T00:00:00Z")
        del item["statistics"]["viewCount"]
        self.assertIsNone(ytapi._normalize(item)["view_count"])


class FetchTests(unittest.TestCase):
    def test_channel_paginates_caps_and_preserves_order(self):
        fake = FakeAPI()
        with mock.patch.object(ytapi, "_get", fake):
            out = ytapi.fetch("https://youtube.com/@chan", recent_cap=75, key="k")
        expected = [v for v in IDS[:75] if v != "vid00000003"]
        self.assertEqual([v["id"] for v in out], expected)
        pages = [p for e, p in fake.calls if e == "playlistItems"]
        self.assertEqual([p["maxResults"] for p in pages], [50, 25])
        self.assertEqual(sum(1 for e, _ in fake.calls if e == "videos"), 2)

    def test_handle_falls_back_to_bare_form(self):
        fake = FakeAPI(handle_needs_at=False)
        with mock.patch.object(ytapi, "_get", fake):
            out = ytapi.fetch("https://youtube.com/@chan", recent_cap=5, key="k")
        self.assertEqual(len(out), 4)  # vid00000003 dropped by fake
        handles = [p["forHandle"] for e, p in fake.calls if e == "channels"]
        self.assertEqual(handles, ["@chan", "chan"])

    def test_unknown_channel_raises(self):
        with mock.patch.object(ytapi, "_get", FakeAPI()):
            with self.assertRaises(ytapi.YouTubeAPIError):
                ytapi.fetch("https://youtube.com/@nobody", key="k")

    def test_no_key_raises(self):
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}):
            with self.assertRaises(ytapi.YouTubeAPIError):
                ytapi.fetch("https://youtube.com/@chan")

    def test_results_pass_selection_window(self):
        with mock.patch.object(ytapi, "_get", FakeAPI()):
            out = ytapi.fetch("https://youtube.com/@chan", recent_cap=10, key="k")
        picked = select.select(out, days=365, limit=3, max_sources=45)
        self.assertEqual(len(picked), 3)
        self.assertGreaterEqual(picked[0]["view_count"], picked[1]["view_count"])


class FallbackTests(unittest.TestCase):
    def test_uses_api_when_key_set(self):
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "k"}), \
             mock.patch.object(ytapi, "_get", FakeAPI()), \
             mock.patch.object(fetch, "_fetch_flat_ytdlp") as ytdlp:
            out = fetch.fetch_flat("https://youtube.com/@chan", recent_cap=5)
        ytdlp.assert_not_called()
        self.assertTrue(all(v["upload_date"] for v in out))

    def test_falls_back_on_api_error(self):
        def boom(*_a, **_k):
            raise ytapi.YouTubeAPIError("quotaExceeded")

        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "k"}), \
             mock.patch.object(ytapi, "_get", boom), \
             mock.patch.object(fetch, "_fetch_flat_ytdlp", return_value=[{"id": "x"}]) as ytdlp:
            out = fetch.fetch_flat("https://youtube.com/@chan", recent_cap=5)
        ytdlp.assert_called_once()
        self.assertEqual(out, [{"id": "x"}])

    def test_no_key_uses_ytdlp(self):
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}), \
             mock.patch.object(fetch, "_fetch_flat_ytdlp", return_value=[]) as ytdlp:
            fetch.fetch_flat("https://youtube.com/@chan")
        ytdlp.assert_called_once()

    def test_enrich_skips_complete_entries(self):
        complete = [{"id": "a", "webpage_url": "u", "upload_date": "20260101"}]
        with mock.patch.object(fetch.subprocess, "run") as run:
            self.assertEqual(fetch.enrich(complete), complete)
        run.assert_not_called()


class LiveErrorShapeTest(unittest.TestCase):
    """Real HTTP error parsing, using a deliberately invalid key. Skipped offline."""

    def test_invalid_key_error(self):
        if os.environ.get("YTAPI_LIVE_TEST") != "1":
            self.skipTest("set YTAPI_LIVE_TEST=1 to hit the real endpoint")
        with self.assertRaises(ytapi.YouTubeAPIError) as ctx:
            ytapi._get("channels", {"part": "id", "forHandle": "@GoogleDevelopers"}, "invalid")
        self.assertIn("400", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

"""Free, bounded headline discovery. Never a factual source for publication."""
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

FEEDS = [('湘南経済新聞', 'https://shonan.keizai.biz/rss.xml')]


def headlines(fetcher=None, now=None):
    now = now or datetime.now(timezone.utc)
    def fetch(url):
        req = urllib.request.Request(url, headers={'User-Agent': 'ShonanDoors-topic-discovery/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(1_000_001)
    fetcher = fetcher or fetch
    result = []
    for name, url in FEEDS:
        try:
            raw = fetcher(url)
            if len(raw) > 1_000_000:
                continue
            root = ET.fromstring(raw)
            for item in root.findall('.//item')[:20]:
                link = item.findtext('link', '').strip()
                date = parsedate_to_datetime(item.findtext('pubDate', ''))
                if date.tzinfo is None or not now - timedelta(days=14) <= date <= now:
                    continue
                if urlparse(link).hostname != urlparse(url).hostname:
                    continue
                result.append({'media': name, 'title': item.findtext('title', '')[:160],
                               'leadUrl': link, 'leadSourceType': 'local_media',
                               'published': date.isoformat()})
        except (OSError, ValueError, TypeError, ET.ParseError):
            continue  # Discovery degradation never relaxes the primary-source gate.
    return result[:20]

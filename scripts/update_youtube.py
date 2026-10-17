#!/usr/bin/env python3
"""Refresh data/youtube.json from the public YouTube channel feed and about page.

Runs on a schedule in GitHub Actions (see .github/workflows/youtube.yml) and can
be run by hand: python3 scripts/update_youtube.py
Standard library only. If YouTube cannot be reached, the old file is kept.
"""
import json, re, sys, time, urllib.error, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CHANNEL = "UCa9gQxrfBl-bLk97KOjYOSw"
HANDLE = "reality-engineer"
OUT = Path(__file__).resolve().parent.parent / "data" / "youtube.json"
UA = {"User-Agent": "Mozilla/5.0 (site data refresh)", "Accept-Language": "en-US,en;q=0.9"}


def get_lang(url, accept="en-US,en;q=0.9", tries=4):
    """YouTube sometimes answers 404 or 5xx for a moment (the RSS feed above all), so retry with a pause."""
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, "Accept-Language": accept}), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as err:
            if n == tries - 1:
                raise
            print(f"retry {n + 1} for {url}: {err}", file=sys.stderr)
            time.sleep(5 * (n + 1))


def get(url):
    return get_lang(url)


def views_num(t):
    t = (t or "").replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KMB]?)", t, re.I)
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def lockups(html):
    """Videos from the channel's Videos tab: id, title, views, relative date, duration."""
    data = json.loads(re.search(r"var ytInitialData = (\{.*?\});</script>", html).group(1))
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:
                found.append(o["lockupViewModel"])
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    out = []
    for v in found:
        md = v.get("metadata", {}).get("lockupMetadataViewModel", {})
        parts = [p.get("text", {}).get("content") for r in md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", []) for p in r.get("metadataParts", [])]
        dur = re.search(r'"thumbnailBadgeViewModel":\s*\{"text":\s*"([\d:]+)"', json.dumps(v.get("contentImage", {})))
        if not v.get("contentId") or len(parts) < 2:
            continue
        out.append({"id": v["contentId"], "title": md.get("title", {}).get("content", ""), "views": views_num(parts[0]), "ago": parts[1], "duration": dur.group(1) if dur else ""})
    return out


def first(pattern, text):
    m = re.search(pattern, text)
    return m.group(1) if m else None


def main():
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
    old_data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    items = []
    try:
        root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"))
    except Exception as err:  # the feed only adds exact view counts and dates; the Videos tab below is enough
        print("feed failed, keeping the last feed items:", err, file=sys.stderr)
        root = None
    for e in (root.findall("a:entry", ns) if root is not None else []):
        vid = e.findtext("yt:videoId", namespaces=ns)
        link = e.find("a:link", ns).get("href", "")
        stats = e.find("media:group/media:community/media:statistics", ns)
        items.append({
            "id": vid,
            "title": e.findtext("a:title", namespaces=ns),
            "published": e.findtext("a:published", namespaces=ns),
            "views": int(stats.get("views", 0)) if stats is not None else 0,
            "short": "/shorts/" in link,
        })
    if not items:
        items = old_data.get("items", [])

    # the latest regular videos (no Shorts) with view counts, titles in both languages
    videos = []
    try:
        pages = {lang: lockups(get_lang(f"https://www.youtube.com/@{HANDLE}/videos", al)) for lang, al in (("en", "en-US,en;q=0.9"), ("ar", "ar,en;q=0.5"))}
        ar = {v["id"]: v for v in pages["ar"]}
        feed = {x["id"]: x for x in items}
        for v in pages["en"]:
            a_ = ar.get(v["id"], v)
            f = feed.get(v["id"], {})
            videos.append({"id": v["id"], "title": {"en": v["title"], "ar": a_["title"]}, "views": f.get("views") or v["views"], "ago": {"en": v["ago"], "ar": a_["ago"]}, "published": f.get("published"), "duration": v["duration"]})
    except Exception as err:
        print("videos page failed:", err, file=sys.stderr)

    channel = {}
    try:
        about = get(f"https://www.youtube.com/@{HANDLE}/about")
        channel = {
            "subscribers": first(r'subscriberCountText":"([^"]+?) subscribers', about),
            "videos": first(r'videoCountText":"([\d,]+) videos', about),
            "views": first(r'viewCountText":"([\d,]+) views', about),
            "joined": first(r'joinedDateText":\{"content":"Joined ([^"]+)"', about),
        }
    except Exception as err:  # keep the last known stats
        print("about page failed:", err, file=sys.stderr)
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8")).get("channel", {})
        channel = {k: channel.get(k) or old.get(k) for k in set(old) | set(channel)}

    # only videos Siddek presents (colleagues also publish on the channel); see data/youtube-mine.json
    mine_file = OUT.parent / "youtube-mine.json"
    mine = set(json.loads(mine_file.read_text(encoding="utf-8")).get("mine", [])) if mine_file.exists() else set()
    for v in videos:
        v["mine"] = v["id"] in mine
    if not videos:
        if root is None:
            print("YouTube unreachable this run; data/youtube.json left as it was")
            return
        videos = old_data.get("videos", [])
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "channel": channel, "videos": videos, "items": items}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(videos)} videos, {len(items)} feed items, channel {channel}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Stand-in for yt-dlp used by the queue tests. Behaviour is chosen by the URL:

  fake://video/<name>[?slow]       one video, H.264 + AAC split streams
  fake://playlist/<n>              a flat playlist with n entries
  fake://needs-cookies/<name>      fails with a bot check unless --cookies is passed
  fake://gone/<name>               fails: video unavailable
"""
import json
import os
import shutil
import sys
import time
from urllib.parse import urlparse

args = sys.argv[1:]
if "--version" in args:
    print("2099.01.01")
    sys.exit(0)
url = args[-1]
if "--load-info-json" in args:
    info = json.load(open(args[args.index("--load-info-json") + 1], encoding="utf-8"))
    url = info["webpage_url"]
p = urlparse(url)
kind, name = p.netloc, p.path.strip("/") or "x"
slow = "slow" in p.query


def info_for(n):
    return {"id": n, "title": f"Fake {n}", "extractor_key": "Fake", "extractor": "fake", "duration": 2,
            "webpage_url": url, "uploader": "Tester", "format_id": "v1+a1", "thumbnail": "",
            "requested_formats": [
                {"format_id": "v1", "vcodec": "avc1.64001f", "acodec": "none", "ext": "mp4", "width": 640,
                 "height": 360, "fps": 30, "filesize": 1000},
                {"format_id": "a1", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "filesize": 100}]}


if kind == "needs-cookies" and "--cookies" not in args:
    print("ERROR: [fake] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies",
          file=sys.stderr)
    sys.exit(1)
if kind == "gone":
    print(f"ERROR: [fake] {name}: Video unavailable. This video has been removed by the uploader", file=sys.stderr)
    sys.exit(1)

if "--dump-single-json" in args:
    if kind == "playlist":
        n = int(name)
        print(json.dumps({"_type": "playlist", "id": "pl", "title": "Fake list", "entries": [
            {"_type": "url", "url": f"fake://video/e{i}", "title": f"Entry {i}", "duration": 2}
            for i in range(1, n + 1)]}))
    else:
        d = info_for(name)
        d["webpage_url"] = url
        print(json.dumps(d))
    sys.exit(0)

work = args[args.index("-P") + 1]
ids = args[args.index("-f") + 1].split(",")
for fid in ids:
    src = os.environ["FAKE_MEDIA_" + fid.upper()]
    ext = os.path.splitext(src)[1]
    dst = os.path.join(work, f"{name}.f{fid}{ext}")
    steps = 40 if slow else 3
    for i in range(steps + 1):
        print("@@P %s %s" % (json.dumps(fid), json.dumps({
            "status": "downloading", "downloaded_bytes": i * 10, "total_bytes": steps * 10, "speed": 100.0,
            "eta": steps - i})), flush=True)
        time.sleep(0.1 if slow else 0.005)
    shutil.copy(src, dst)
    print("@@F %s %s" % (json.dumps(fid), json.dumps(dst)), flush=True)

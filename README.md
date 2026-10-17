# Ingest

A Windows app for downloading video and audio from almost any site, built on
yt-dlp. Every file comes out ready for Premiere Pro and After Effects, with
nothing to transcode, interpret or relink afterwards.

## What "ready for Premiere and After Effects" means here

Neither app opens VP9, AV1, Opus, WebM or MKV, and both mishandle variable
frame rates, odd frame sizes, untagged color and cover-art streams. Web video
has all of these. For each download Ingest:

1. Picks formats by resolution and frame rate first, then prefers H.264 and
   AAC when they're just as good, so most YouTube downloads up to 1080p are
   copied without re-encoding.
2. Measures what actually arrived with ffprobe: codec, bit depth, chroma,
   frame timing (to catch variable frame rate), HDR transfer, pixel aspect,
   rotation, interlacing, sample rate.
3. Copies every stream it can. It only re-encodes for a stated reason
   ("VP9 isn't supported by Premiere or After Effects", "variable frame
   rate"), and header-only problems such as missing color tags or a 640:639
   pixel aspect are fixed in the bitstream without re-encoding. Constant-rate
   video whose timestamps were rounded on the way (Twitch and other RTMP
   streams, MKV, FLV) has every frame restamped onto an exact grid instead:
   the same frames, bit for bit, in seconds rather than a full encode.
4. When it encodes: constant frame rate snapped to the nearest standard rate,
   even dimensions, square pixels, BT.709 tags, HDR tone-mapped to SDR, a
   keyframe every second for fast scrubbing, AAC-LC or PCM at 48 kHz, index at
   the front of the file, no chapter or cover-art tracks.
5. Measures the result again and shows `Pr ✓  Ae ✓` only when every check
   passed.

| Output | What you get | Use it for |
| --- | --- | --- |
| H.264 (default) | MP4, H.264 High 8-bit, AAC 48 kHz | Everything. Smallest files |
| ProRes 422 | MOV, Proxy/LT/422/HQ, 10-bit 4:2:2, PCM | Smooth scrubbing in After Effects |
| DNxHR | MOV, LB/SQ/HQ/HQX, PCM | Fast timeline playback in Premiere |
| HEVC | MP4, 10-bit, keeps HDR if you want | Smaller HDR masters |
| Audio | WAV 24-bit, AAC or MP3 at 48 kHz | Music, voice, sound effects |
| Original | Exactly what the site serves | Archiving |

Images come through too: JPEG, PNG, GIF, TIFF and BMP are kept as served;
WebP, AVIF, HEIC and JPEG XL are converted to PNG, or kept with a warning if
FFmpeg can't read them.

## Getting it

Download `Ingest-<version>-Setup-x64.exe` (per-user install, no admin) or the
portable zip from the latest successful
[GitHub release](../../releases/latest). Unreleased builds are also available
from successful [build runs](../../actions/workflows/build.yml) under Artifacts. The build
isn't code-signed, so SmartScreen warns on first run: More info > Run anyway.

On first launch Ingest installs its engines from their official GitHub
releases, checks each against the published SHA-256, and keeps them in
`%LOCALAPPDATA%\Ingest\engines`:

- **yt-dlp** for downloading (1,800+ sites and most pages with an embedded video)
- **FFmpeg** builds from yt-dlp/FFmpeg-Builds, for merging, measuring and converting
- **Deno**, which yt-dlp needs to read YouTube
- **gallery-dl** (optional), tried when yt-dlp finds no video, for image galleries

yt-dlp updates itself daily if you leave auto-update on; switch to the
nightly channel in Settings > Engines when a site breaks.

## Using it

| | |
| --- | --- |
| `Enter` | Download the link with the Output settings on the right |
| `Shift+Enter` | Inspect first: pick formats, trim, subtitles, playlist entries |
| `Ctrl+V` | Paste links from anywhere in the window |
| Drop | Links, browser tabs or a `.txt` list onto the window |
| Drag out | Finished files straight into Premiere's Project panel |
| `Space` / `Delete` | Pause or resume / remove the selected rows |
| Bookmarklet | Settings > General > Copy bookmarklet. Clicking it on any page sends that page to Ingest |

Downloads and conversions run in separate slots, so the next download starts
while the last one converts. Pausing a download keeps the partial files;
pausing a conversion suspends FFmpeg in place. Unfinished work resumes on the
next launch.

Epidemic Sound music links (`epidemicsound.com/music/tracks/<UUID>/`) work in
the same URL field, including Inspect. Ingest reads the track's published
audio URLs and prefers the full mix over individual stems. Select Original
to keep the served MP3, or an audio output profile to convert it. This uses
the site's MP3 playback stream.

When a download fails, Ingest retries on its own where that can help:
with your browser cookies when a site asks you to sign in, with browser
impersonation when a site blocks the request (HTTP 403, Cloudflare), with a
fresh extraction when links expire, with yt-dlp nightly when a site changed
and the fix isn't in a release yet, and with gallery-dl when there's no video
to find. A failure that's left over says why and what to do next.

Sites that need you signed in (members-only, age-restricted, private, YouTube
bot checks): Settings > Accounts > cookies from a browser. Chrome and Edge
encrypt cookies with app-bound keys on Windows, so yt-dlp often can't read
them; Firefox works, or export a `cookies.txt`. Cookies are only sent when a
site asks, unless you choose otherwise.

## Building

```
pip install -r requirements-dev.txt
python -m ingest                      # run from source (src/ on PYTHONPATH)
pyinstaller --noconfirm packaging/ingest.spec
iscc /DAppVersion=1.0.1 packaging/ingest.iss
```

CI (`.github/workflows/build.yml`) builds on `windows-latest`, runs the frozen
exe through `--selftest` and `--screenshot`, downloads a real video with
`--e2e`, then produces the installer and portable zip.

Tests: `python -m pytest`. The conversion tests generate awkward media with
FFmpeg (VP9/Opus, variable frame rate, odd sizes, HDR, interlaced, rotated,
anamorphic, cover art) and verify every output. Set `INGEST_TEST_FFMPEG` and
`INGEST_TEST_FFPROBE` to use a specific FFmpeg.

Design notes, including how the interface avoids the usual generated-UI
look, are in [docs/DESIGN.md](docs/DESIGN.md).

Only download what you have the right to use.

"""Accept Epidemic Sound's UUID music links in the existing yt-dlp extractor."""

from yt_dlp.extractor.epidemicsound import EpidemicSoundIE as _EpidemicSoundIE


class EpidemicSoundMusicIE(_EpidemicSoundIE):
    IE_NAME = "EpidemicSound:music"

    # Upstream's `sfx` group selects /json/track/kosmos-id/<UUID>. Music uses
    # the same API, including its published full-track and individual stem URLs.
    # Keep the upstream extractor's metadata, full-track preference and networking.
    _VALID_URL = (
        r"https?://(?:www\.)?epidemicsound\.com/"
        r"(?P<sfx>music/tracks)/(?P<id>[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"
        r"(?:[/?#]|$)"
    )

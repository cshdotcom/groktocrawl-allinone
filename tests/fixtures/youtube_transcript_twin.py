import sys
import types
from pathlib import Path
from types import SimpleNamespace


class YouTubeTranscriptTwin:
    video_id = "PPM2ODdo2t8"

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.list_calls = 0
        self.ytdlp_calls = 0
        self.ytdlp_options = None

    def install(self):
        twin = self

        class Track:
            language_code = "fi"
            is_translatable = False

            def fetch(self):
                raise AssertionError("untranslated API track fetched")

            def translate(self, language_code):
                raise AssertionError("API translation used")

        api = SimpleNamespace(list=lambda video_id: twin._list(video_id, Track()))
        self.monkeypatch.setitem(
            sys.modules,
            "youtube_transcript_api",
            types.SimpleNamespace(YouTubeTranscriptApi=lambda: api),
        )

        class YoutubeDL:
            def __init__(self, options):
                self.options = options
                twin.ytdlp_options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                assert download is True and url.endswith(f"v={twin.video_id}")
                twin.ytdlp_calls += 1
                output = Path(self.options["outtmpl"].replace("%(ext)s", "vtt"))
                output.write_text(
                    "WEBVTT\n\n00:00.000 --> 00:01.000\n"
                    "Hello from translated caption\n",
                    encoding="utf-8",
                )
                return {
                    "automatic_captions": {
                        "fi": [
                            {
                                "name": "English",
                                "url": "https://youtube.test/timedtext?lang=fi&tlang=en",
                            }
                        ]
                    }
                }

        self.monkeypatch.setitem(
            sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YoutubeDL)
        )

    def _list(self, video_id, track):
        assert video_id == self.video_id
        self.list_calls += 1
        return iter([track])

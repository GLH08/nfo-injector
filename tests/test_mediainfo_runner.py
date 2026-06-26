import json
from backend.mediainfo_runner import _mi_to_ffprobe_dict


MI_JSON = json.dumps({"media": {"track": [
    {"@type": "General", "Duration": "7262.030"},
    {"@type": "Video", "Format": "AVC", "Width": "1280", "Height": "720",
     "FrameRate": "59.940", "DisplayAspectRatio": "1.778"},
    {"@type": "Audio", "Format": "AAC", "Channels": "2", "SamplingRate": "48000",
     "Language": "Japanese"},
]}})


def test_conversion_video():
    d = _mi_to_ffprobe_dict(MI_JSON)
    v = [s for s in d["streams"] if s["codec_type"] == "video"][0]
    assert v["codec_name"] == "h264"
    assert v["width"] == 1280
    assert v["height"] == 720
    assert v["r_frame_rate"] == "59.940"
    assert v["display_aspect_ratio"] == "1.778"
    assert d["format"]["duration"] == "7262.030"


def test_conversion_audio():
    d = _mi_to_ffprobe_dict(MI_JSON)
    a = [s for s in d["streams"] if s["codec_type"] == "audio"][0]
    assert a["codec_name"] == "aac"
    assert a["channels"] == 2
    assert a["sample_rate"] == "48000"
    assert a["tags"]["language"] == "jpn"


def test_conversion_empty():
    d = _mi_to_ffprobe_dict('{"media":{"track":[]}}')
    assert d == {"streams": [], "format": {}}

from backend.openlist_resolver import resolve


def test_basic():
    url = resolve("https://openlist.novaw.de/d/115/Media",
                  "Meta/JP/NO-ZH/ABF-259/ABF-259.strm", "ABF-259.mp4")
    assert url == "https://openlist.novaw.de/d/115/Media/Meta/JP/NO-ZH/ABF-259/ABF-259.mp4"


def test_chinese():
    url = resolve("https://openlist.novaw.de/d/115/Media",
                  "中转/CN/收集/某片/某片.strm", "某片.mp4")
    assert url == ("https://openlist.novaw.de/d/115/Media/"
                   "%E4%B8%AD%E8%BD%AC/CN/%E6%94%B6%E9%9B%86/"
                   "%E6%9F%90%E7%89%87/%E6%9F%90%E7%89%87.mp4")


def test_empty_root():
    assert resolve("", "x/y.strm", "y.mp4") == ""


def test_trailing_slash_root():
    url = resolve("https://openlist.novaw.de/d/115/Media/",
                  "Meta/A.strm", "A.mp4")
    assert url == "https://openlist.novaw.de/d/115/Media/Meta/A.mp4"

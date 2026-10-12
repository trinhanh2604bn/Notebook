"""URL import: parsing, SSRF guards, metadata, policy, bounded downloads, API + pipeline integration.

All network access is mocked (httpx.MockTransport, fake DNS, fake yt-dlp), so
these tests never contact YouTube or Facebook.
"""

import dataclasses
import shutil
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import jobs, main, pipeline
from app.transcribe import Segment, Transcript
from app.url_import import metadata, retrieval, service, urls
from app.url_import.errors import UrlImportError
from tests.conftest import needs_ffmpeg

YT_ID = "dQw4w9WgXcQ"


# ---------------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Resolve every host to a public IP unless a test overrides it."""
    table = {}

    def fake_getaddrinfo(host, *a, **k):
        ip = table.get(host, "142.250.0.1")
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(urls.socket, "getaddrinfo", fake_getaddrinfo)
    service.clear_cache()
    yield table
    service.clear_cache()


@pytest.fixture
def cfg(monkeypatch):
    """Override settings in every module that reads them."""
    def apply(**kw):
        new = dataclasses.replace(main.settings, **kw)
        for mod in (main, service, retrieval, metadata):
            monkeypatch.setattr(mod, "settings", new)
        return new
    return apply


def mock_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def yt_oembed_ok(request):
    if request.url.host == "www.youtube.com" and request.url.path == "/oembed":
        return httpx.Response(200, json={"title": "Bài giảng mẫu", "author_name": "Kênh của tôi",
                                         "thumbnail_url": f"https://i.ytimg.com/vi/{YT_ID}/hqdefault.jpg"})
    return httpx.Response(404)


# ---------------------------------------------------------------------- URL parsing

@pytest.mark.parametrize("raw,platform,vid,canonical", [
    (f"https://www.youtube.com/watch?v={YT_ID}", "youtube", YT_ID, f"https://www.youtube.com/watch?v={YT_ID}"),
    (f"youtube.com/watch?v={YT_ID}&t=42s", "youtube", YT_ID, f"https://www.youtube.com/watch?v={YT_ID}"),
    (f"https://m.youtube.com/watch?v={YT_ID}&list=PL123", "youtube", YT_ID, f"https://www.youtube.com/watch?v={YT_ID}"),
    (f"https://www.youtube.com/shorts/{YT_ID}", "youtube", YT_ID, f"https://www.youtube.com/watch?v={YT_ID}"),
    (f"https://youtu.be/{YT_ID}?si=abc", "youtube", YT_ID, f"https://www.youtube.com/watch?v={YT_ID}"),
    (f"http://youtu.be/{YT_ID}", "youtube", YT_ID, f"https://www.youtube.com/watch?v={YT_ID}"),
    ("https://www.facebook.com/watch/?v=1234567890123", "facebook", "1234567890123",
     "https://www.facebook.com/watch/?v=1234567890123"),
    ("https://www.facebook.com/watch?v=1234567890123", "facebook", "1234567890123",
     "https://www.facebook.com/watch/?v=1234567890123"),
    ("https://www.facebook.com/SomePage/videos/1234567890123/", "facebook", "1234567890123",
     "https://www.facebook.com/watch/?v=1234567890123"),
    ("https://m.facebook.com/SomePage/videos/my-title/1234567890123/", "facebook", "1234567890123",
     "https://www.facebook.com/watch/?v=1234567890123"),
    ("https://www.facebook.com/reel/987654321012345", "facebook", "987654321012345",
     "https://www.facebook.com/reel/987654321012345"),
])
def test_parse_valid_urls(raw, platform, vid, canonical):
    p = urls.parse_video_url(raw)
    assert (p.platform, p.video_id, p.canonical_url, p.needs_resolve) == (platform, vid, canonical, False)


@pytest.mark.parametrize("raw", ["https://fb.watch/abcDEF123/", "https://www.facebook.com/share/v/1AbCdEfGh/",
                                 "https://www.facebook.com/share/r/1AbCdEfGh/"])
def test_short_links_need_resolution(raw):
    p = urls.parse_video_url(raw)
    assert p.platform == "facebook" and p.needs_resolve and p.video_id is None


@pytest.mark.parametrize("raw,code", [
    ("", "invalid_url"),
    ("not a url", "invalid_url"),
    ("javascript:alert(1)", "invalid_url"),
    ("file:///etc/passwd", "invalid_url"),
    ("ftp://youtube.com/watch?v=" + YT_ID, "invalid_url"),
    ("https://user:pass@www.youtube.com/watch?v=" + YT_ID, "invalid_url"),
    ("https://www.youtube.com:8080/watch?v=" + YT_ID, "invalid_url"),
    ("https://www.youtube.com/watch?v=" + YT_ID + "\nHost: internal", "invalid_url"),
    ("https://www.youtube.com/watch\t?v=" + YT_ID, "invalid_url"),
    ("https://example.com/video.mp4", "unsupported_platform"),
    ("http://localhost:8000/api/health", "invalid_url"),
    ("http://127.0.0.1/watch?v=" + YT_ID, "unsupported_platform"),
    ("http://169.254.169.254/latest/meta-data/", "unsupported_platform"),
    ("https://youtube.com.evil.com/watch?v=" + YT_ID, "unsupported_platform"),
    ("https://evilyoutube.com/watch?v=" + YT_ID, "unsupported_platform"),
    ("https://www.yоutube.com/watch?v=" + YT_ID, "unsupported_platform"),  # Cyrillic "о"
    ("https://www.youtube.com/playlist?list=PL1234567890", "unsupported_url"),
    ("https://www.youtube.com/@channel", "unsupported_url"),
    ("https://www.youtube.com/watch?v=short", "unsupported_url"),
    ("https://www.facebook.com/SomePage", "unsupported_url"),
    ("https://www.facebook.com/watch/?v=abc", "unsupported_url"),
    ("https://www.facebook.com/groups/1/permalink/2", "unsupported_url"),
    ("https://" + "a" * 3000 + ".youtube.com/", "invalid_url"),
])
def test_parse_rejects(raw, code):
    with pytest.raises(UrlImportError) as ei:
        urls.parse_video_url(raw)
    assert ei.value.code == code


# ---------------------------------------------------------------------- SSRF

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "::ffff:127.0.0.1", "fe80::1"])
def test_private_dns_answers_blocked(public_dns, ip):
    public_dns["www.youtube.com"] = ip
    with pytest.raises(UrlImportError) as ei:
        urls.check_outbound("https://www.youtube.com/oembed", urls.API_HOSTS["youtube"])
    assert ei.value.code == "blocked_address"


@pytest.mark.parametrize("url", [
    "http://www.youtube.com/oembed",            # not https
    "https://evil.com/x",                       # not allowlisted
    "https://127.0.0.1/x",
    "https://[::1]/x",
    "https://www.youtube.com:8443/x",
    "https://user@www.youtube.com/x",
    "file:///etc/passwd",
])
def test_check_outbound_rejects(url):
    with pytest.raises(UrlImportError):
        urls.check_outbound(url, urls.API_HOSTS["youtube"] + ("127.0.0.1",))


def test_check_outbound_allows_public_allowlisted():
    urls.check_outbound("https://rr1---sn-abc.googlevideo.com/videoplayback?x=1", urls.MEDIA_SUFFIXES["youtube"])


def test_shortlink_redirect_followed_within_allowlist():
    def handler(req):
        if req.url.host == "fb.watch":
            return httpx.Response(302, headers={"location": "https://www.facebook.com/watch/?v=1234567890123"})
        raise AssertionError("unexpected request")

    with mock_client(handler)() as c:
        p = urls.resolve_short_link(urls.parse_video_url("https://fb.watch/abcDEF123/"), c)
    assert p.video_id == "1234567890123" and not p.needs_resolve


@pytest.mark.parametrize("location,code", [
    ("http://169.254.169.254/latest/meta-data/", "unsupported_platform"),
    ("https://evil.example/watch?v=1", "unsupported_platform"),
    ("https://www.facebook.com/login/?next=x", "login_required"),
    ("https://www.facebook.com/SomePage", "unsupported_url"),
])
def test_shortlink_redirect_to_disallowed_target(location, code):
    with mock_client(lambda req: httpx.Response(302, headers={"location": location}))() as c:
        with pytest.raises(UrlImportError) as ei:
            urls.resolve_short_link(urls.parse_video_url("https://fb.watch/abcDEF123/"), c)
    assert ei.value.code == code


def test_shortlink_redirect_loop_bounded():
    with mock_client(lambda req: httpx.Response(302, headers={"location": "https://fb.watch/loopLOOP1/"}))() as c:
        with pytest.raises(UrlImportError) as ei:
            urls.resolve_short_link(urls.parse_video_url("https://fb.watch/abcDEF123/"), c)
    assert ei.value.code == "too_many_redirects"


def test_shortlink_resolving_to_private_ip_blocked(public_dns):
    public_dns["fb.watch"] = "10.1.2.3"
    with mock_client(lambda req: httpx.Response(500))() as c:
        with pytest.raises(UrlImportError) as ei:
            urls.resolve_short_link(urls.parse_video_url("https://fb.watch/abcDEF123/"), c)
    assert ei.value.code == "blocked_address"


def test_thumbnail_allowlist():
    assert urls.safe_thumbnail("https://i.ytimg.com/vi/x/hq.jpg")
    assert urls.safe_thumbnail("https://scontent.xx.fbcdn.net/v/t.jpg")
    assert urls.safe_thumbnail("http://i.ytimg.com/x.jpg") is None
    assert urls.safe_thumbnail("javascript:alert(1)") is None
    assert urls.safe_thumbnail("https://evil.com/x.jpg") is None


def test_guarded_ydl_blocks_requests_outside_allowlist(public_dns):
    cls = retrieval._make_ydl_class()
    cls.allowed_suffixes = urls.MEDIA_SUFFIXES["youtube"]
    with cls({"quiet": True, "logger": retrieval._QuietLogger()}) as ydl:
        for bad in ("https://169.254.169.254/latest/meta-data/", "https://evil.com/",
                    "http://www.youtube.com/", "file:///etc/passwd"):
            with pytest.raises(UrlImportError):
                ydl.urlopen(bad)
        public_dns["www.youtube.com"] = "127.0.0.1"
        with pytest.raises(UrlImportError):
            ydl.urlopen("https://www.youtube.com/watch?v=" + YT_ID)


# ---------------------------------------------------------------------- metadata + policy

def test_youtube_metadata_only_when_retrieval_off(cfg):
    cfg(youtube_retrieval="off")
    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(yt_oembed_ok))
    assert check.title == "Bài giảng mẫu" and check.author == "Kênh của tôi"
    assert check.thumbnail.startswith("https://i.ytimg.com/")
    assert check.status == "metadata_only" and check.retrieval_permitted is False
    assert check.retrieval_method is None and "tải tệp MP4" in check.message


def test_retrieval_permitted_never_true_just_because_extractor_works(cfg, monkeypatch):
    cfg(youtube_retrieval="off")
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"title": "x", "duration": 60})
    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(yt_oembed_ok))
    assert check.retrieval_permitted is False


def test_youtube_ready_when_operator_enabled_ytdlp(cfg, monkeypatch):
    cfg(youtube_retrieval="ytdlp")
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"title": "x", "duration": 125.0})
    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(yt_oembed_ok))
    assert check.status == "ready" and check.retrieval_permitted and check.retrieval_method == "ytdlp"
    assert check.duration_seconds == 125.0 and check.title == "Bài giảng mẫu"


@pytest.mark.parametrize("status,code", [(404, "unavailable"), (400, "unavailable"), (401, "restricted"),
                                         (429, "error")])
def test_youtube_oembed_errors(cfg, status, code):
    cfg(youtube_retrieval="off")
    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(lambda r: httpx.Response(status)))
    assert check.retrieval_permitted is False
    assert check.status == code


def test_youtube_data_api_duration_limit(cfg):
    cfg(youtube_retrieval="ytdlp", youtube_api_key="k")

    def handler(req):
        if req.url.host == "www.googleapis.com":
            assert req.headers["x-goog-api-key"] == "k" and "key=" not in str(req.url)
            return httpx.Response(200, json={"items": [{"snippet": {"title": "Dài"},
                                                        "contentDetails": {"duration": "PT12M5S"}}]})
        return yt_oembed_ok(req)

    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(handler))
    assert check.status == "too_long" and not check.retrieval_permitted
    assert "12.1 phút" in check.message


def test_live_video_rejected(cfg):
    cfg(youtube_retrieval="ytdlp", youtube_api_key="k")

    def handler(req):
        if req.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": [{"snippet": {"liveBroadcastContent": "live"},
                                                        "contentDetails": {"duration": "P0D"}}]})
        return yt_oembed_ok(req)

    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(handler))
    assert check.error_code == "live_not_supported" and not check.retrieval_permitted


@pytest.mark.parametrize("msg,code,status", [
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private", "restricted"),
    ("ERROR: [youtube] x: Sign in to confirm you’re not a bot", "bot_check", "restricted"),
    ("ERROR: [youtube] x: Sign in to confirm your age", "age_restricted", "restricted"),
    ("ERROR: [youtube] x: The uploader has not made this video available in your country", "geo_restricted",
     "restricted"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable", "unavailable"),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited", "error"),
    ("ERROR: [facebook] x: This video is only available for registered users", "login_required", "restricted"),
    ("ERROR: something odd", "extraction_failed", "error"),
])
def test_restricted_content_mapped(cfg, monkeypatch, msg, code, status):
    cfg(youtube_retrieval="ytdlp")

    def boom(parsed):
        raise retrieval.map_error(Exception(msg))

    monkeypatch.setattr(retrieval, "probe_ytdlp", boom)
    check, _ = service.check_url(f"https://youtu.be/{YT_ID}", mock_client(yt_oembed_ok))
    assert (check.error_code, check.status, check.retrieval_permitted) == (code, status, False)
    assert "ERROR" not in check.message and "[youtube]" not in check.message  # no raw library text


def test_facebook_without_token_is_metadata_only(cfg):
    cfg(facebook_retrieval="graph_api", facebook_access_token="")
    check, _ = service.check_url("https://www.facebook.com/reel/987654321012345",
                                 mock_client(lambda r: httpx.Response(500)))
    assert check.status == "metadata_only" and check.error_code == "token_missing"
    assert "FACEBOOK_ACCESS_TOKEN" in check.message


def test_facebook_expired_token_is_reported(cfg):
    cfg(facebook_retrieval="graph_api", facebook_access_token="tok")
    expired = httpx.Response(400, json={"error": {"code": 190, "message": "Session has expired"}})
    check, _ = service.check_url("https://www.facebook.com/reel/987654321012345", mock_client(lambda r: expired))
    assert check.status == "metadata_only" and check.error_code == "token_invalid"
    assert "expired" not in check.message  # raw Graph text never shown


def test_facebook_graph_api_permitted_when_source_readable(cfg):
    cfg(facebook_retrieval="graph_api", facebook_access_token="tok")
    seen = []

    def handler(req):
        seen.append(req)
        assert req.headers["authorization"] == "Bearer tok"
        assert "tok" not in str(req.url)  # never in the URL (logs)
        if req.url.path.endswith("/oembed_video"):
            return httpx.Response(200, json={"author_name": "Trang của tôi"})
        if req.url.path.endswith("/987654321012345"):
            return httpx.Response(200, json={"title": "Reel", "length": 42.5, "source": "https://video.fbcdn.net/x.mp4",
                                             "picture": "https://scontent.fbcdn.net/p.jpg"})
        return httpx.Response(404)

    check, _ = service.check_url("https://www.facebook.com/reel/987654321012345", mock_client(handler))
    assert check.retrieval_permitted and check.retrieval_method == "graph_api"
    assert check.duration_seconds == 42.5 and check.author == "Trang của tôi"
    assert "fbcdn.net/x.mp4" not in str(check.public())  # signed source URL stays server-side


def test_facebook_user_token_falls_back_to_page_token(cfg):
    cfg(facebook_retrieval="graph_api", facebook_access_token="usertok")

    def handler(req):
        auth = req.headers["authorization"]
        if req.url.path.endswith("/oembed_video"):
            return httpx.Response(200, json={"author_name": "Trang của tôi"})
        if req.url.path.endswith("/me/accounts"):
            assert auth == "Bearer usertok"
            return httpx.Response(200, json={"data": [{"access_token": "otherpage"}, {"access_token": "mypage"}]})
        if req.url.path.endswith("/987654321012345"):
            if auth == "Bearer mypage":
                return httpx.Response(200, json={"title": "Reel", "length": 30, "source": "https://video.fbcdn.net/x.mp4"})
            return httpx.Response(200, json={"title": "Reel", "length": 30})  # USER/other token: no source
        return httpx.Response(404)

    check, _ = service.check_url("https://www.facebook.com/reel/987654321012345", mock_client(handler))
    assert check.retrieval_permitted and check.retrieval_method == "graph_api" and check.title == "Reel"
    with mock_client(handler)() as c:
        assert metadata.graph_source_url("987654321012345", c) == "https://video.fbcdn.net/x.mp4"


def test_facebook_oembed_feature_missing_is_not_private(cfg):
    cfg(facebook_retrieval="graph_api", facebook_access_token="tok")
    no_feature = httpx.Response(400, json={"error": {"code": 10, "message": "Meta oEmbed Read"}})
    unreadable = httpx.Response(400, json={"error": {"code": 100, "message": "missing permissions"}})
    check, _ = service.check_url("https://www.facebook.com/reel/987654321012345", mock_client(
        lambda r: no_feature if r.url.path.endswith("/oembed_video") else unreadable))
    assert check.error_code == "not_permitted" and check.status == "metadata_only"


def test_facebook_ytdlp_ignores_expired_token(cfg, monkeypatch):
    cfg(facebook_retrieval="ytdlp", facebook_access_token="tok")
    expired = httpx.Response(400, json={"error": {"code": 190, "message": "Session has expired"}})
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda parsed: {"title": "Reel", "duration": 30})
    check, _ = service.check_url("https://www.facebook.com/reel/987654321012345", mock_client(lambda r: expired))
    assert check.retrieval_permitted and check.retrieval_method == "ytdlp" and check.title == "Reel"


def test_facebook_graph_without_permission(cfg):
    cfg(facebook_retrieval="graph_api", facebook_access_token="tok")

    def handler(req):
        if req.url.path.endswith("/oembed_video"):
            return httpx.Response(200, json={"author_name": "Ai đó"})
        return httpx.Response(400, json={"error": {"code": 100, "message": "missing permissions"}})

    check, _ = service.check_url("https://www.facebook.com/watch/?v=1234567890123", mock_client(handler))
    assert check.error_code == "not_permitted" and not check.retrieval_permitted


# ---------------------------------------------------------------------- bounded downloads

class FakeYDL:
    """Stands in for GuardedYDL: emits progress and writes media into paths.home."""
    info = {"title": "t", "duration": 60, "requested_formats": [{"filesize": 1000}, {"filesize": 500}]}
    chunks = [1000]
    allowed_suffixes = ()

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return dict(self.info)

    def sanitize_info(self, info):
        return info

    def process_ie_result(self, info, download=True):
        home = self.opts["paths"]["home"]
        out = __import__("pathlib").Path(home) / "media.mp4"
        total = 0
        with out.open("wb") as fh:
            for c in self.chunks:
                total += c
                fh.write(b"\0" * c)
                for hook in self.opts["progress_hooks"]:
                    hook({"filename": str(out), "downloaded_bytes": total, "status": "downloading"})
        return info


def _parsed():
    return urls.parse_video_url(f"https://youtu.be/{YT_ID}")


def test_ytdlp_options_are_restrictive():
    opts = retrieval._base_opts("youtube")
    assert opts["noplaylist"] and opts["allowed_extractors"] == ["youtube"]
    assert opts["cookiefile"] is None and "cookiesfrombrowser" not in opts
    assert "height<=480" in opts["format"] and "protocol=https" in opts["format"]
    assert opts["max_filesize"] == 80 * 1024 * 1024


def test_download_success_and_temp_cleanup(cfg, monkeypatch, tmp_path):
    cfg(url_max_download_mb=1)
    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: FakeYDL)
    dest, work = tmp_path / "input.mp4", tmp_path / "download"
    retrieval.download_ytdlp(_parsed(), dest, work)
    assert dest.stat().st_size == 1000
    assert not work.exists()


def test_download_without_ffmpeg_is_reported_clearly(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: FakeYDL)
    monkeypatch.setattr(retrieval.shutil, "which", lambda name: None)
    with pytest.raises(UrlImportError) as ei:
        retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert ei.value.code == "ffmpeg_missing"


def test_ffmpeg_merge_error_mapped():
    err = Exception("ERROR: You have requested merging of multiple formats but ffmpeg is not installed. Aborting due to --abort-on-error")
    assert retrieval.map_error(err, "download_failed").code == "ffmpeg_missing"


def test_download_retries_transient_failure_once(cfg, monkeypatch, tmp_path):
    calls = []

    class Flaky(FakeYDL):
        def extract_info(self, url, download=False):
            calls.append(1)
            if len(calls) == 1:
                raise Exception("ERROR: [facebook] 123: Cannot parse data; please report this issue")
            return dict(self.info)

    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: Flaky)
    monkeypatch.setattr(retrieval, "RETRY_DELAY_SEC", 0)
    retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert len(calls) == 2 and (tmp_path / "input.mp4").exists()


def test_download_does_not_retry_permanent_failure(cfg, monkeypatch, tmp_path):
    calls = []

    class Private(FakeYDL):
        def extract_info(self, url, download=False):
            calls.append(1)
            raise Exception("ERROR: This video is private")

    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: Private)
    monkeypatch.setattr(retrieval, "RETRY_DELAY_SEC", 0)
    with pytest.raises(UrlImportError) as ei:
        retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert ei.value.code == "private" and len(calls) == 1


def test_download_estimated_too_large(cfg, monkeypatch, tmp_path):
    cfg(url_max_download_mb=1)
    big = type("Big", (FakeYDL,), {"info": {"duration": 60, "requested_formats": [{"filesize": 2 * 1048576}]}})
    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: big)
    with pytest.raises(UrlImportError) as ei:
        retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert ei.value.code == "too_large"
    assert not (tmp_path / "download").exists() and not (tmp_path / "input.mp4").exists()


def test_download_actual_bytes_capped(cfg, monkeypatch, tmp_path):
    cfg(url_max_download_mb=1)
    # Size unknown up front, stream keeps going past the cap.
    liar = type("Liar", (FakeYDL,), {"info": {"duration": 60}, "chunks": [600_000, 600_000]})
    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: liar)
    with pytest.raises(UrlImportError) as ei:
        retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert ei.value.code == "too_large"
    assert not (tmp_path / "download").exists()


def test_download_overlong(cfg, monkeypatch, tmp_path):
    cfg(url_max_duration_sec=600)
    long_ = type("Long", (FakeYDL,), {"info": {"duration": 601}})
    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: long_)
    with pytest.raises(UrlImportError) as ei:
        retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert ei.value.code == "too_long"


def test_download_live_and_playlist_rejected(cfg, monkeypatch, tmp_path):
    for info, code in (({"is_live": True}, "live_not_supported"), ({"_type": "playlist"}, "unsupported_url")):
        fake = type("F", (FakeYDL,), {"info": info})
        monkeypatch.setattr(retrieval, "_make_ydl_class", lambda fake=fake: fake)
        with pytest.raises(UrlImportError) as ei:
            retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
        assert ei.value.code == code


def test_download_timeout(cfg, monkeypatch, tmp_path):
    cfg(download_timeout_sec=5)
    monkeypatch.setattr(retrieval, "_make_ydl_class", lambda: FakeYDL)
    clock = iter([0.0, 10.0, 20.0, 30.0])  # deadline computed at t=0, first progress hook at t=10
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: next(clock))
    with pytest.raises(UrlImportError) as ei:
        retrieval.download_ytdlp(_parsed(), tmp_path / "input.mp4", tmp_path / "download")
    assert ei.value.code == "timeout"
    assert not (tmp_path / "download").exists()


def test_http_download_caps_and_cleans(cfg, tmp_path):
    cfg(url_max_download_mb=1)

    def handler(req):
        return httpx.Response(200, content=b"\0" * (2 * 1048576))

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(UrlImportError) as ei:
            retrieval.download_http("https://video.fbcdn.net/x.mp4", tmp_path / "input.mp4", "facebook", c)
    assert ei.value.code == "too_large"
    assert list(tmp_path.iterdir()) == []


def test_http_download_redirect_to_internal_blocked(cfg, tmp_path):
    cfg()

    def handler(req):
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data/"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(UrlImportError) as ei:
            retrieval.download_http("https://video.fbcdn.net/x.mp4", tmp_path / "input.mp4", "facebook", c)
    assert ei.value.code == "blocked_address"


def test_http_download_timeout(cfg, tmp_path):
    cfg()

    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(UrlImportError) as ei:
            retrieval.download_http("https://video.fbcdn.net/x.mp4", tmp_path / "input.mp4", "facebook", c)
    assert ei.value.code == "timeout"


# ---------------------------------------------------------------------- API + pipeline integration

@pytest.fixture
def client(monkeypatch, cfg):
    def fake_transcribe(audio, language=None):
        segs = [Segment(i * 1.0, i * 1.0 + 1, f"Câu thử nghiệm số {i} về kỹ năng thuyết trình.") for i in range(6)]
        return Transcript(segs, "vi", 6.0)

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(service, "http_client", mock_client(yt_oembed_ok))
    monkeypatch.setattr(service, "_executor", None)
    with TestClient(main.app) as c:
        yield c


def _wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_config_exposes_url_import(client, cfg):
    cfg()
    ui = client.get("/api/config").json()["url_import"]
    assert ui["enabled"] and ui["retrieval"]["youtube"] == "off" and ui["max_duration_sec"] == 600


def test_validate_endpoint(client, cfg):
    cfg(youtube_retrieval="off")
    r = client.post("/api/videos/url/validate", json={"url": f"https://www.youtube.com/shorts/{YT_ID}"})
    assert r.status_code == 200
    body = r.json()
    assert body["platform"] == "youtube" and body["title"] == "Bài giảng mẫu"
    assert body["retrieval_permitted"] is False and body["status"] == "metadata_only"


@pytest.mark.parametrize("url,status", [("https://example.com/a.mp4", 400), ("http://127.0.0.1:8000/", 400),
                                        ("file:///etc/passwd", 400), ("x" * 3000, 422)])
def test_validate_endpoint_rejects(client, url, status):
    r = client.post("/api/videos/url/validate", json={"url": url})
    assert r.status_code == status
    assert "Traceback" not in r.text


def test_import_requires_rights_confirmation(client, cfg):
    cfg(youtube_retrieval="ytdlp")
    r = client.post("/api/videos/url/import", json={"url": f"https://youtu.be/{YT_ID}"})
    assert r.status_code == 400 and "xác nhận" in r.json()["detail"]


def test_import_refused_when_retrieval_disabled(client, cfg):
    cfg(youtube_retrieval="off")
    before = set(jobs.settings.jobs_dir.iterdir())
    r = client.post("/api/videos/url/import", json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True})
    assert r.status_code == 403 and "tải tệp MP4" in r.json()["detail"]
    assert set(jobs.settings.jobs_dir.iterdir()) == before  # no job created


def test_url_feature_can_be_disabled(client, cfg):
    cfg(url_import_enabled=False)
    assert client.post("/api/videos/url/validate", json={"url": f"https://youtu.be/{YT_ID}"}).status_code == 404


def test_optional_rate_limit(client, cfg, monkeypatch):
    cfg(url_rate_limit_per_min=2)
    monkeypatch.setattr(main, "_rate_hits", {})
    codes = [client.post("/api/videos/url/validate", json={"url": f"https://youtu.be/{YT_ID}"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


@needs_ffmpeg
def test_url_import_end_to_end_through_existing_pipeline(client, cfg, monkeypatch, sample_mp4):
    """URL -> (mocked authorised retrieval) -> FFmpeg -> transcript -> AI -> PPTX download."""
    cfg(youtube_retrieval="ytdlp")
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"title": "Bài giảng mẫu", "duration": 6})
    fetched = []

    def fake_fetch(source, d):
        fetched.append(source)
        (d / "download").mkdir()
        shutil.copy(sample_mp4, d / "input.mp4")

    monkeypatch.setattr(service, "fetch_media", fake_fetch)
    r = client.post("/api/videos/url/import", json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True,
                                                    "style": "creative", "slide_count": 6})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "done", job["error"]
    assert job["source"]["type"] == "url" and job["source"]["platform"] == "youtube"
    assert fetched[0]["method"] == "ytdlp" and fetched[0]["url"] == f"https://www.youtube.com/watch?v={YT_ID}"
    assert {"downloading", "extracting_audio", "transcribing", "analyzing", "generating"} <= set(job["stage_times"])
    assert job["filename"] == "Bài giảng mẫu.mp4"

    d = jobs.job_dir(job_id)
    assert not (d / "input.mp4").exists() and not (d / "download").exists()  # temp media cleaned up
    dl = client.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"
    tr = client.get(f"/api/jobs/{job_id}/transcript").json()
    assert tr["segments"][0]["text"].startswith("Câu thử nghiệm")


@needs_ffmpeg
def test_downloaded_file_revalidated_with_ffprobe(client, cfg, monkeypatch, tmp_path):
    cfg(youtube_retrieval="ytdlp")
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"duration": 6})
    monkeypatch.setattr(service, "fetch_media", lambda s, d: (d / "input.mp4").write_bytes(b"not a video" * 100))
    job_id = client.post("/api/videos/url/import",
                         json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "tải tệp MP4" in job["error"]
    assert not (jobs.job_dir(job_id) / "input.mp4").exists()


def test_failed_download_reports_vietnamese_and_can_retry(client, cfg, monkeypatch, sample_mp4):
    cfg(youtube_retrieval="ytdlp")
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"duration": 6})
    calls = {"n": 0}

    def flaky(source, d):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UrlImportError("rate_limited")
        shutil.copy(sample_mp4, d / "input.mp4")

    monkeypatch.setattr(service, "fetch_media", flaky)
    job_id = client.post("/api/videos/url/import",
                         json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "giới hạn truy cập" in job["error"]
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    if shutil.which("ffmpeg"):
        assert _wait(client, job_id)["status"] == "done"


def test_retry_rechecks_policy(client, cfg, monkeypatch):
    cfg(youtube_retrieval="ytdlp")
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"duration": 6})
    monkeypatch.setattr(service, "fetch_media", lambda s, d: (_ for _ in ()).throw(UrlImportError("timeout")))
    job_id = client.post("/api/videos/url/import",
                         json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True}).json()["job_id"]
    assert _wait(client, job_id)["status"] == "failed"
    cfg(youtube_retrieval="off")  # operator disabled retrieval meanwhile
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "đang tắt" in job["error"]


def test_concurrent_downloads_limited_to_one(client, cfg, monkeypatch):
    cfg(youtube_retrieval="ytdlp", max_concurrent_downloads=1, max_queue_size=5)
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"duration": 6})
    active, peak, lock = [0], [0], threading.Lock()

    def slow(source, d):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        raise UrlImportError("timeout")

    monkeypatch.setattr(service, "fetch_media", slow)
    ids = [client.post("/api/videos/url/import",
                       json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True}).json()["job_id"]
           for _ in range(3)]
    for i in ids:
        _wait(client, i)
    assert peak[0] == 1


def test_queue_full_returns_429(client, cfg, monkeypatch):
    cfg(youtube_retrieval="ytdlp", max_queue_size=1)
    monkeypatch.setattr(retrieval, "probe_ytdlp", lambda p: {"duration": 6})
    monkeypatch.setattr(service, "pending_downloads", lambda: 1)
    r = client.post("/api/videos/url/import", json={"url": f"https://youtu.be/{YT_ID}", "rights_confirmed": True})
    assert r.status_code == 429 and "bận" in r.json()["detail"]


def test_restart_marks_interrupted_download_failed(cfg):
    job_id, _ = jobs.new_job({"language": "vi", "style": "professional"}, "x.mp4", source={"type": "url"})
    jobs.update(job_id, status="downloading", stage="downloading")
    jobs.recover_after_restart()
    job = jobs.load(job_id)
    assert job["status"] == "failed" and "Thử lại" in job["error"]
    jobs.delete(job_id)

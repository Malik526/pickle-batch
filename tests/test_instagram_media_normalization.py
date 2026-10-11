"""Instagram media normalization (Milestone 4.2.1): the pure policy (sizes,
frame rates, bitrate budget, ffmpeg command), real ffmpeg encodes of small
generated fixtures (iPhone-style rotated portrait 4K, landscape 4K, 60/120
fps, 10-bit HEVC MOV, PCM audio), derivative reuse and invalidation, and
failure handling (ffmpeg failure, timeout with heartbeat, corrupt media,
duration limits). Storage is LocalStorage in tmp_path; nothing touches
Supabase or Meta."""

import shutil
import subprocess
from pathlib import Path

import pytest

from content_automation.media.media_storage import create_video_from_upload
from content_automation.media.stream_probe import StreamDetails, probe_streams
from content_automation.persistence.content_store import ContentStore
from content_automation.publishing.instagram import media_preparation, normalization
from content_automation.publishing.instagram import media_requirements as reqs
from content_automation.publishing.instagram.media_preparation import PreparationError, prepare_publishable_media
from content_automation.storage.local import LocalStorage

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg/ffprobe not on PATH")
NOW = "2026-10-11T00:00:00+00:00"


def _encoder_available(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return f" {name} " in out


def _encode(path: Path, *, size: str, fps: int = 30, seconds: float = 3.5, vcodec=("libx264", "-preset", "ultrafast"),
            pix_fmt: str = "yuv420p", audio=("aac",), rotate: int | None = None) -> Path:
    raw = path.with_name("raw-" + path.name) if rotate is not None else path
    command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={fps}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000"]
    command += ["-c:v", *vcodec, "-pix_fmt", pix_fmt]
    command += ["-c:a", *audio, "-shortest"] if audio else ["-an"]
    subprocess.run([*command, str(raw)], check=True)
    if rotate is not None:  # remux with a rotation flag, as phones write portrait video
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-c", "copy",
                        "-metadata:s:v:0", f"rotate={rotate}", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixtures")
    files = {
        "vertical_1080": _encode(root / "vertical_1080.mp4", size="1080x1920"),
        "iphone_portrait_4k": _encode(root / "iphone_portrait_4k.mov", size="3840x2160", rotate=90),
        "landscape_4k": _encode(root / "landscape_4k.mp4", size="3840x2160"),
        "vertical_60fps": _encode(root / "vertical_60fps.mp4", size="1080x1920", fps=60),
        "fast_120fps": _encode(root / "fast_120fps.mp4", size="720x1280", fps=120),
        "pcm_audio": _encode(root / "pcm_audio.mov", size="720x1280", audio=("pcm_s16le",)),
        "too_short": _encode(root / "too_short.mp4", size="720x1280", seconds=1.5),
    }
    if _encoder_available("libx265"):
        files["hevc_10bit"] = _encode(root / "hevc_10bit.mov", size="1080x1920", vcodec=("libx265", "-preset", "ultrafast", "-x265-params", "log-level=error"),
                                      pix_fmt="yuv420p10le")
    corrupt = root / "corrupt.mp4"
    corrupt.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"not really a video" * 100)
    files["corrupt"] = corrupt
    return files


@pytest.fixture
def store(tmp_path):
    with ContentStore(db_path=tmp_path / "t.db") as s:
        yield s


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(root=tmp_path / "objects")


def _upload(store, storage, tmp_path, source: Path, name="clip"):
    user = store.get_user_by_email("creator@example.com") or store.create_user("creator@example.com", None, NOW)
    upload = tmp_path / f"upload-{name}{source.suffix}"
    shutil.copy(source, upload)
    return create_video_from_upload(store, storage, user.id, local_path=upload, original_filename=source.name,
                                    file_hash=f"hash-{name}", file_size_bytes=upload.stat().st_size, created_at=NOW)


def _details(**overrides) -> StreamDetails:
    base = dict(path=Path("x.mp4"), container="mov", format_names=frozenset({"mov", "mp4"}), video_codec="h264",
                pixel_format="yuv420p", coded_width=1080, coded_height=1920, rotation=0, fps=30.0, video_bitrate=8_000_000,
                audio_codec="aac", audio_sample_rate=48_000, audio_channels=2, color_transfer=None, duration_seconds=10.0,
                file_size_bytes=10_000_000)
    return StreamDetails(**{**base, **overrides})


# --- pure policy -------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("display", "expected"),
    [((1080, 1920), (1080, 1920)), ((2160, 3840), (1080, 1920)), ((3840, 2160), (1920, 1080)),
     ((2160, 2160), (1920, 1920)), ((4096, 1716), (1920, 804)), ((721, 1281), (720, 1280)), ((640, 480), (640, 480))],
)
def test_output_dimensions_downscale_proportionally_and_never_upscale(display, expected):
    assert normalization.output_dimensions(*display) == expected


@pytest.mark.parametrize(("fps", "expected"), [(30, None), (60, None), (23.976, None), (59.94, None),
                                               (120, 60), (240, 60), (15, 30), (None, 30)])
def test_frame_rate_policy(fps, expected):
    assert normalization.target_fps(fps) == expected


def test_bitrate_cap_keeps_long_videos_under_300_mb():
    assert normalization.video_bitrate_cap_kbps(30) == normalization.MAX_VIDEO_KBPS
    fifteen_minutes = normalization.video_bitrate_cap_kbps(15 * 60)
    assert fifteen_minutes < normalization.MAX_VIDEO_KBPS
    assert (fifteen_minutes + normalization.AUDIO_KBPS) * 1000 * 15 * 60 / 8 < reqs.MAX_FILE_SIZE_BYTES


def test_compatible_source_needs_no_plan():
    assert normalization.plan_normalization(_details()) is None
    assert normalization.plan_normalization(_details(fps=60.0)) is None  # 60 fps is supported: kept


def test_plan_for_iphone_portrait_4k():
    plan = normalization.plan_normalization(_details(coded_width=3840, coded_height=2160, rotation=270))
    assert plan.reasons == (reqs.RESOLUTION,)
    assert (plan.width, plan.height, plan.fps, plan.copy_audio, plan.tonemap_hdr) == (1080, 1920, None, True, False)


def test_ffmpeg_command_policy():
    plan = normalization.plan_normalization(_details(coded_width=3840, coded_height=2160, fps=120.0, audio_codec="pcm_s16le"))
    command = normalization.build_ffmpeg_command(Path("in.mov"), Path("out.mp4"), plan)
    vf = command[command.index("-vf") + 1]
    assert vf == "scale=1920:1080,fps=60,format=yuv420p"
    assert command[command.index("-c:v") + 1] == "libx264" and "-crf" in command and "+faststart" in command
    assert command[command.index("-c:a") + 1] == "aac" and command[command.index("-b:a") + 1] == "128k"
    assert command[command.index("-map_metadata") + 1] == "-1"  # e.g. phone GPS location isn't carried over
    assert command[command.index("-maxrate") + 1] == "10000k"

    hdr = normalization.plan_normalization(_details(pixel_format="yuv420p10le", color_transfer="arib-std-b67"))
    hdr_vf = normalization.build_ffmpeg_command(Path("i"), Path("o"), hdr)[normalization.build_ffmpeg_command(Path("i"), Path("o"), hdr).index("-vf") + 1]
    assert "tonemap=tonemap=hable" in hdr_vf and hdr_vf.endswith("format=yuv420p")

    silent = normalization.plan_normalization(_details(audio_codec=None, coded_width=3840, coded_height=2160))
    assert "-c:a" not in normalization.build_ffmpeg_command(Path("i"), Path("o"), silent)


# --- real encodes ------------------------------------------------------------------------

def _prepare(store, storage, video, **kwargs):
    return prepare_publishable_media(store, storage, store.get_video(video.id), **kwargs)


def _stored_details(storage, key) -> StreamDetails:
    with storage.materialize(key) as path:
        return probe_streams(path)


def test_compatible_1080p_original_is_published_untouched(store, storage, tmp_path, media, monkeypatch):
    video = _upload(store, storage, tmp_path, media["vertical_1080"])
    monkeypatch.setattr(normalization, "normalize", lambda *a, **k: pytest.fail("must not transcode a compatible source"))
    prepared = _prepare(store, storage, video)
    assert (prepared.storage_key, prepared.derived) == (video.storage_key, False)


def test_60fps_source_is_preserved_without_transcoding(store, storage, tmp_path, media, monkeypatch):
    video = _upload(store, storage, tmp_path, media["vertical_60fps"])
    monkeypatch.setattr(normalization, "normalize", lambda *a, **k: pytest.fail("60 fps is supported"))
    assert _prepare(store, storage, video).derived is False


def test_iphone_portrait_4k_becomes_a_1080x1920_derivative(store, storage, tmp_path, media, caplog):
    caplog.set_level("INFO")
    source = probe_streams(media["iphone_portrait_4k"])
    assert (source.coded_width, source.coded_height, source.display_width) == (3840, 2160, 2160)  # the real failure mode

    video = _upload(store, storage, tmp_path, media["iphone_portrait_4k"])
    prepared = _prepare(store, storage, video)

    assert prepared.derived and not prepared.reused
    assert prepared.storage_key == f"users/{video.user_id}/videos/{video.id}/derived/instagram/reel-v1-hash-iphone.mp4".replace("hash-iphone", "hash-clip")
    out = _stored_details(storage, prepared.storage_key)
    assert (out.display_width, out.display_height, out.rotation) == (1080, 1920, 0)  # rotation applied to the pixels
    assert (out.video_codec, out.pixel_format, out.audio_codec, out.fps) == ("h264", "yuv420p", "aac", 30.0)
    assert reqs.compatibility_problems(out) == []
    assert abs(out.duration_seconds - source.duration_seconds) < 0.5
    assert storage.exists(video.storage_key)  # the original is untouched
    assert "instagram_media_normalized" in caplog.text and "source_resolution=2160x3840" in caplog.text
    assert "output_resolution=1080x1920" in caplog.text and "encode_seconds=" in caplog.text


def test_landscape_4k_is_scaled_proportionally(store, storage, tmp_path, media):
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    out = _stored_details(storage, _prepare(store, storage, video).storage_key)
    assert (out.display_width, out.display_height) == (1920, 1080)


def test_120fps_is_normalized_to_60(store, storage, tmp_path, media):
    video = _upload(store, storage, tmp_path, media["fast_120fps"])
    out = _stored_details(storage, _prepare(store, storage, video).storage_key)
    assert out.fps == 60.0 and (out.display_width, out.display_height) == (720, 1280)  # size untouched


def test_pcm_audio_mov_becomes_mp4_with_aac(store, storage, tmp_path, media):
    video = _upload(store, storage, tmp_path, media["pcm_audio"])
    out = _stored_details(storage, _prepare(store, storage, video).storage_key)
    assert out.audio_codec == "aac" and "mp4" in out.format_names


def test_10bit_hevc_mov_becomes_8bit_h264(store, storage, tmp_path, media):
    if "hevc_10bit" not in media:
        pytest.skip("libx265 not available to build the fixture")
    video = _upload(store, storage, tmp_path, media["hevc_10bit"])
    out = _stored_details(storage, _prepare(store, storage, video).storage_key)
    assert (out.video_codec, out.pixel_format) == ("h264", "yuv420p")


# --- reuse and invalidation ----------------------------------------------------------------

def test_existing_derivative_is_reused_without_downloading_or_encoding(store, storage, tmp_path, media, monkeypatch):
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    first = _prepare(store, storage, video)

    monkeypatch.setattr(normalization, "normalize", lambda *a, **k: pytest.fail("must reuse"))
    monkeypatch.setattr(media_preparation, "materialize_canonical_media", lambda *a, **k: pytest.fail("must not download"))
    again = _prepare(store, storage, video)
    assert (again.storage_key, again.derived, again.reused) == (first.storage_key, True, True)


def test_changed_source_hash_regenerates_the_derivative(store, storage, tmp_path, media):
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    old_key = _prepare(store, storage, video).storage_key
    store.update_video(video.id, file_hash="hash-replaced-source")
    new = _prepare(store, storage, video)
    assert new.storage_key != old_key and not new.reused and "hash-replaced-source" in new.storage_key
    assert storage.exists(new.storage_key)


def test_policy_version_is_part_of_the_key(store, storage, tmp_path, media, monkeypatch):
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    old_key = _prepare(store, storage, video).storage_key
    monkeypatch.setattr(normalization, "POLICY_VERSION", "v2")
    assert _prepare(store, storage, video).storage_key != old_key


# --- failures ------------------------------------------------------------------------------

def test_ffmpeg_failure_is_a_clear_preparation_error(store, storage, tmp_path, media, monkeypatch, caplog):
    caplog.set_level("INFO")
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    monkeypatch.setattr(normalization, "build_ffmpeg_command",
                        lambda src, dst, plan, **_: ["ffmpeg", "-loglevel", "error", "-i", str(src), "-c:v", "no_such_encoder", str(dst)])
    with pytest.raises(PreparationError) as raised:
        _prepare(store, storage, video)
    assert raised.value.reason_code == "INSTAGRAM_MEDIA_PREPARATION_FAILED"
    assert str(raised.value) == "This video could not be prepared for Instagram."
    assert "instagram_media_normalization_failed" in caplog.text and "stage=encode" in caplog.text and "exit_code=" in caplog.text
    assert not any(key.startswith(f"users/{video.user_id}/videos/{video.id}/derived")
                   for key in (p.relative_to(storage.root).as_posix() for p in storage.root.rglob("*") if p.is_file()))


def test_timeout_kills_the_encode_and_heartbeats_meanwhile(store, storage, tmp_path, media, monkeypatch):
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    monkeypatch.setattr(normalization, "build_ffmpeg_command", lambda src, dst, plan, **_: ["sleep", "12"])
    monkeypatch.setattr(normalization, "HEARTBEAT_SECONDS", 0)
    beats = []
    with pytest.raises(PreparationError) as raised:
        _prepare(store, storage, video, heartbeat=lambda: beats.append(1), timeout_seconds=6)
    assert raised.value.reason_code == "INSTAGRAM_MEDIA_PREPARATION_TIMEOUT"
    assert beats  # the claim was kept fresh while waiting


def test_corrupt_media_fails_clearly(store, storage, tmp_path, media):
    video = _upload(store, storage, tmp_path, media["corrupt"])
    with pytest.raises(PreparationError) as raised:
        _prepare(store, storage, video)
    assert raised.value.reason_code == "CORRUPT_MEDIA"


def test_too_short_video_is_rejected_not_padded(store, storage, tmp_path, media):
    video = _upload(store, storage, tmp_path, media["too_short"])
    with pytest.raises(PreparationError) as raised:
        _prepare(store, storage, video)
    assert raised.value.reason_code == "INSTAGRAM_MEDIA_DURATION"


def test_too_long_video_is_rejected_not_trimmed(store, storage, tmp_path, media, monkeypatch):
    video = _upload(store, storage, tmp_path, media["landscape_4k"])
    real_probe = media_preparation.probe_streams
    monkeypatch.setattr(media_preparation, "probe_streams",
                        lambda path: _replace(real_probe(path), duration_seconds=15 * 60 + 5))
    monkeypatch.setattr(normalization, "normalize", lambda *a, **k: pytest.fail("must not encode a too-long video"))
    with pytest.raises(PreparationError) as raised:
        _prepare(store, storage, video)
    assert raised.value.reason_code == "INSTAGRAM_MEDIA_DURATION"


def _replace(details, **changes):
    from dataclasses import replace

    return replace(details, **changes)


# --- Milestone 4.2.3: bounded resources and failure classification ----------------------------

def test_command_caps_decoder_encoder_and_filter_threads():
    plan = normalization.plan_normalization(_details(coded_width=3840, coded_height=2160, rotation=90))
    command = normalization.build_ffmpeg_command(Path("in.mov"), Path("out.mp4"), plan, threads=2)
    input_at = command.index("-i")
    assert command[input_at - 2:input_at] == ["-threads", "2"]  # decoder (input option)
    encoder_at = command.index("-c:v")
    assert command[encoder_at:encoder_at + 4] == ["-c:v", "libx264", "-threads", "2"]  # encoder
    assert command[command.index("-filter_threads") + 1] == "1"
    assert command[command.index("-vf") + 1] == "scale=1080:1920,format=yuv420p"  # output policy unchanged


@pytest.mark.parametrize(("exit_code", "reason"), [(-9, normalization.KILLED), (-15, normalization.INTERRUPTED),
                                                    (1, normalization.FAILED), (234, normalization.FAILED)])
def test_exit_codes_are_classified(exit_code, reason):
    assert normalization.classify_exit(exit_code) == reason


@pytest.mark.parametrize(("script", "reason"), [("kill -9 $$", normalization.KILLED), ("kill -15 $$", normalization.INTERRUPTED),
                                                ("exit 3", normalization.FAILED)])
def test_real_process_endings_are_classified(tmp_path, monkeypatch, script, reason):
    monkeypatch.setattr(normalization, "build_ffmpeg_command", lambda src, dst, plan, **_: ["bash", "-c", script])
    plan = normalization.plan_normalization(_details(coded_width=3840, coded_height=2160))
    with pytest.raises(normalization.NormalizationError) as raised:
        normalization.normalize(tmp_path / "in.mov", tmp_path / "out.mp4", plan, source_details=_details())
    assert raised.value.reason_code == reason and raised.value.stage == "encode"


def test_peak_memory_of_the_encode_is_measured(tmp_path, monkeypatch):
    allocate = "import time; b = bytearray(80 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); time.sleep(2.5)"
    monkeypatch.setattr(normalization, "build_ffmpeg_command", lambda src, dst, plan, **_: ["python3", "-c", allocate])
    plan = normalization.plan_normalization(_details(coded_width=3840, coded_height=2160))
    with pytest.raises(normalization.NormalizationError) as raised:  # exits 0 without an output file
        normalization.normalize(tmp_path / "in.mov", tmp_path / "out.mp4", plan, source_details=_details())
    assert raised.value.peak_rss_mb is not None and raised.value.peak_rss_mb >= 70


def test_resource_limits_report_values_only():
    limits = normalization.resource_limits()
    assert set(limits) == {"memory_limit_mb", "visible_cpus", "usable_cpus"}
    assert limits["visible_cpus"] is None or limits["visible_cpus"] >= 1


def test_constrained_portrait_4k_encode_stays_within_a_memory_bound(tmp_path, media):
    source = probe_streams(media["iphone_portrait_4k"])
    plan = normalization.plan_normalization(source)
    output, encode = normalization.normalize(media["iphone_portrait_4k"], tmp_path / "reel.mp4", plan,
                                             source_details=source, threads=2)
    assert (output.display_width, output.display_height) == (1080, 1920)
    assert encode.exit_code == 0 and encode.seconds > 0
    # Auto-threaded, the same kind of encode peaked ~1.2 GB on a 16-core machine;
    # capped at 2 threads it stays a few hundred MB. Generous bound for CI noise.
    if encode.peak_rss_mb is not None:
        assert encode.peak_rss_mb < 700

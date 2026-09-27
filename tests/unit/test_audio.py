from corpus_builder import TONE_A, TONE_B

from aerochorus.worker.audio import probe_audio


def test_collector_style_mp3_is_probed():
    probe = probe_audio(TONE_A, ".mp3")
    assert probe.error is None
    assert probe.format == "mp3"
    assert probe.sample_rate == 8000
    assert probe.channels == 1
    assert 2300 <= probe.duration_ms <= 2500
    assert probe_audio(TONE_B, ".MP3").duration_ms > probe.duration_ms


def test_garbage_and_empty_files_report_errors_instead_of_raising():
    for data in (b"", b"definitely not audio"):
        probe = probe_audio(data, ".mp3")
        assert probe.duration_ms is None
        assert probe.error

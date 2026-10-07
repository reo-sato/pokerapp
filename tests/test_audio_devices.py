"""tests/test_audio_devices.py

マイクを名前で選ぶ（config `audio.device_name`, 店舗 2026-10-01）。Bluetooth のマイク・ヘッドセットを
つなぐ・切ると PortAudio の番号の並びが変わり、番号（`audio.device_id`）が別のマイクを指す。名前なら起動のたびに
同じマイクを探せる。どの名前も見つからなければ別のマイクで録らない。
"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from audio.devices import (
    InputDevice,
    choose_device,
    chosen_per_name,
    describe_names,
    list_input_devices,
    parse_device_names,
)
from audio.recorder import AudioThread
from core.event_queue import make_audio_queue

# 店舗 PC の一覧の形（Bluetooth の送信機をつないだとき）。MME の名前は 31 文字で切れる
_STORE = [
    InputDevice(0, "Microsoft サウンド マッパー - Input", "MME", True, False),
    InputDevice(1, "Headset (DJI Mic Mini 2-EBA00C ", "MME", True, True),
    InputDevice(2, "Microphone (Wireless Mic Rx)", "MME", True, False),
    InputDevice(5, "プライマリ サウンド キャプチャ ドライバー", "Windows DirectSound", True, False),
    InputDevice(6, "Microphone (Wireless Mic Rx)", "Windows DirectSound", True, False),
    InputDevice(7, "Headset (DJI Mic Mini 2-EBA00C)", "Windows DirectSound", True, False),
    InputDevice(12, "Microphone (Wireless Mic Rx)", "Windows WASAPI", False, False),
    InputDevice(18, "Microphone (Wireless Mic Rx)", "Windows WDM-KS", False, False),
]


class TestParseNames:
    @pytest.mark.parametrize("value, names", [
        ("", []),
        (None, []),
        (0, []),
        ("Wireless Mic Rx", ["Wireless Mic Rx"]),
        ("Wireless Mic Rx|DJI Mic Mini 2", ["Wireless Mic Rx", "DJI Mic Mini 2"]),
        (" Wireless Mic Rx , DJI Mic Mini 2 ", ["Wireless Mic Rx", "DJI Mic Mini 2"]),
        ("EBA00C、ED8C97", ["EBA00C", "ED8C97"]),
        ("a||b|", ["a", "b"]),
        (["Wireless Mic Rx", " DJI "], ["Wireless Mic Rx", "DJI"]),
    ])
    def test_order_is_kept_and_blanks_dropped(self, value, names):
        assert parse_device_names(value) == names

    def test_describe(self):
        assert describe_names(["Wireless Mic Rx", "DJI Mic Mini 2"]) == "「Wireless Mic Rx」 / 「DJI Mic Mini 2」"


class TestChoose:
    def test_the_first_name_found_wins(self):
        assert choose_device(_STORE, ["Wireless Mic Rx", "DJI Mic Mini 2"]).index == 2
        assert choose_device(_STORE, ["DJI Mic Mini 2", "Wireless Mic Rx"]).index == 1

    def test_falls_back_to_the_next_name(self):
        without_rx = [d for d in _STORE if "Wireless" not in d.name]
        assert choose_device(without_rx, ["Wireless Mic Rx", "DJI Mic Mini 2"]).index == 1

    def test_never_another_mic(self):
        """名前が 1 つも無ければ選ばない（既定のマイク・サウンド マッパーに落とさない）。"""
        assert choose_device(_STORE, ["Yeti"]) is None
        assert choose_device([], ["Wireless Mic Rx"]) is None
        assert choose_device(_STORE, []) is None

    def test_case_and_the_cut_off_name(self):
        assert choose_device(_STORE, ["wireless mic rx"]).index == 2
        assert choose_device(_STORE, ["eba00c"]).index == 1        # 31 文字で切れた MME の名前にも末尾の記号は残る

    def test_a_device_that_opens_16k_before_a_lower_number(self):
        devices = [InputDevice(3, "Mic X", "Windows WASAPI", False, False), InputDevice(9, "Mic X", "MME", True, False)]
        assert choose_device(devices, ["Mic X"]).index == 9
        only_bad = [InputDevice(3, "Mic X", "Windows WASAPI", False, False)]
        assert choose_device(only_bad, ["Mic X"]).index == 3      # 開けないかもしれないが、別のマイクよりよい

    def test_dicts_from_the_corpus_page(self):
        devices = [{"index": 4, "name": "Microphone (Wireless Mic Rx)", "rate_ok": True},
                   {"index": 2, "name": "Headset (DJI Mic Mini 2-EBA00C ", "rate_ok": True}]
        assert choose_device(devices, ["DJI"])["index"] == 2

    def test_one_per_name_for_two_mics(self):
        assert [d.index for d in chosen_per_name(_STORE, ["Wireless Mic Rx", "DJI Mic Mini 2"])] == [2, 1]
        assert [d.index for d in chosen_per_name(_STORE, ["DJI", "Mic Mini 2", "Yeti"])] == [1]   # 同じマイクは 1 回


# ――― AudioThread が名前でマイクを開く ―――

class _FakePA:
    """名前と方式の一覧を持つ PyAudio。open は呼ばれた番号を覚えて OSError（スレッドをそこで止める）。"""

    devices = [
        {"name": "Microsoft サウンド マッパー - Input", "maxInputChannels": 2, "hostApi": 0},
        {"name": "Headset (DJI Mic Mini 2-EBA00C ", "maxInputChannels": 1, "hostApi": 0},
        {"name": "Microphone (Wireless Mic Rx)", "maxInputChannels": 1, "hostApi": 0},
        {"name": "Speakers (Realtek)", "maxInputChannels": 0, "hostApi": 0},
    ]
    opened: list[int] = []

    def get_default_input_device_info(self):
        return {"index": 1}

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        return self.devices[i]

    def get_host_api_info_by_index(self, i):
        return {"name": ["MME", "Windows WDM-KS"][i]}

    def is_format_supported(self, rate, input_device, input_channels, input_format):
        return True

    def open(self, **kwargs):
        _FakePA.opened.append(kwargs["input_device_index"])
        raise OSError(-9996, "Invalid input device (no default output device)")

    def terminate(self):
        pass


class _RdpPA(_FakePA):
    """RDP の音声が接続元に回っているときの一覧（店舗 2026-09-30）: マイクは WDM-KS 方式にしか出ない。"""

    devices = [
        {"name": "Microphone (Wireless Mic Rx)", "maxInputChannels": 1, "hostApi": 1},
        {"name": "Headset (…DJI Mic Mini 2-EBA00C)", "maxInputChannels": 1, "hostApi": 1},
    ]

    def get_default_input_device_info(self):
        raise OSError("no default input")


@pytest.fixture
def fake_pyaudio(monkeypatch):
    _FakePA.opened = []
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=_FakePA, paInt16=8))
    return _FakePA


@pytest.fixture
def rdp_pyaudio(monkeypatch):
    _FakePA.opened = []
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=_RdpPA, paInt16=8))
    return _RdpPA


def _run(names, device_id: int = 0) -> AudioThread:
    """起動のときに 1 回だけ開いてみる（開けなければ開き直しを待つが、止める合図を先に立てておくので待たずに
    返る = 同じスレッドで走らせてよい）。"""
    stop = threading.Event()
    stop.set()
    thread = AudioThread(audio_queue=make_audio_queue(), device_id=device_id, device_names=names,
                         transcriber=SimpleNamespace(ready=True), stop_event=stop)
    thread.run()
    return thread


class TestAudioThreadByName:
    def test_opens_the_named_mic_whatever_its_number(self, fake_pyaudio):
        thread = _run(["Wireless Mic Rx", "DJI Mic Mini 2"], device_id=1)
        assert fake_pyaudio.opened == [2]
        assert thread.health["device_index"] == 2 and "missing_names" not in thread.health

    def test_the_number_is_used_without_names(self, fake_pyaudio):
        thread = _run([], device_id=1)
        assert fake_pyaudio.opened == [1] and thread.health["device_index"] == 1

    def test_missing_names_open_nothing(self, fake_pyaudio):
        thread = _run(["Yeti"], device_id=1)
        assert fake_pyaudio.opened == []
        assert thread.health["state"] == "error" and thread.health["missing_names"]
        assert "「Yeti」" in thread.health["error"]

    def test_a_listing_error_is_reported_as_missing(self, fake_pyaudio, monkeypatch):
        def boom(self):
            raise OSError("device list changed")

        monkeypatch.setattr(_FakePA, "get_device_count", boom)
        thread = _run(["Wireless Mic Rx"])
        assert fake_pyaudio.opened == [] and thread.health["missing_names"]


class TestRemoteDesktopAudio:
    """RDP の音声の再生先は接続元の端末の設定で、PC の側からは変えられない。端末に回っているとマイクは WDM-KS に
    しか出ず開けないので、開けなかったときに原因（`rdp_audio`）を死活表示に載せる（起動時に直し方を出す）。"""

    def test_named_mic_seen_only_through_wdm_ks(self, rdp_pyaudio):
        thread = _run(["Wireless Mic Rx"])
        assert rdp_pyaudio.opened == [0]
        assert thread.health["state"] == "error" and thread.health["rdp_audio"]

    def test_missing_name_under_remote_desktop(self, rdp_pyaudio):
        thread = _run(["Yeti"])
        assert thread.health["missing_names"] and thread.health["rdp_audio"]

    def test_by_number_too(self, rdp_pyaudio):
        thread = _run([], device_id=1)
        assert rdp_pyaudio.opened == [1] and thread.health["rdp_audio"]

    def test_an_ordinary_open_failure_is_not_blamed_on_remote_desktop(self, fake_pyaudio):
        assert _run(["Wireless Mic Rx"]).health["rdp_audio"] is False


def test_list_input_devices_matches_the_fake(fake_pyaudio):
    devices = list_input_devices(_FakePA(), SimpleNamespace(paInt16=8), 16000)
    assert [(d.index, d.default) for d in devices] == [(0, False), (1, True), (2, False)]


# ───────────────── 録音の途中でマイクが切れたとき（店舗 2026-10-01）─────────────────
# 「Unanticipated host error」のあと閉じたマイクを間を空けずに読み続け、9 分で 3470 万行の警告をログに書いた
# （ログが 3.7 GB になり、ログをまとめるのに長い時間がかかった。聞き取りも止まったままだった）。


class _Stream:
    """読むたびに次のチャンクを返す（例外なら投げる）。尽きたら b""（`_capture_loop` の終端）。"""

    def __init__(self, chunks) -> None:
        self.chunks = list(chunks)
        self.reads = 0
        self.closed = False

    def read(self, n, exception_on_overflow=False):
        self.reads += 1
        item = self.chunks.pop(0) if self.chunks else b""
        if isinstance(item, Exception):
            raise item
        return item

    def stop_stream(self):
        raise OSError(-9988, "Stream closed")       # 切れたマイクは止めるのにも失敗する

    def close(self):
        self.closed = True


class _DroppingPA(_FakePA):
    """開くたびに用意したストリームを順に渡す。作り直した回数を数える（一覧を取り直すため）。"""

    created = 0
    streams: list = []

    def __init__(self) -> None:
        type(self).created += 1

    def open(self, **kwargs):
        _FakePA.opened.append(kwargs["input_device_index"])
        if not self.streams:
            raise OSError(-9996, "Invalid input device")
        return self.streams.pop(0)


@pytest.fixture
def dropping_pyaudio(monkeypatch):
    import audio.recorder as recorder

    _FakePA.opened = []
    _DroppingPA.created = 0
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=_DroppingPA, paInt16=8))
    monkeypatch.setattr(recorder, "_READ_RETRY_SEC", 0.0)
    monkeypatch.setattr(recorder, "_REOPEN_FIRST_SEC", 0.0)
    return _DroppingPA


def _closed_errors(n: int) -> list:
    return [OSError(-9999, "Unanticipated host error")] + [OSError(-9988, "Stream closed")] * (n - 1)


class TestMicDrop:
    def test_a_dead_stream_is_not_read_in_a_tight_loop(self, dropping_pyaudio, caplog):
        stream = _Stream(_closed_errors(1000))
        thread = AudioThread(audio_queue=make_audio_queue(), transcriber=SimpleNamespace(ready=True),
                             stop_event=threading.Event())
        with caplog.at_level("WARNING", logger="audio.recorder"):
            thread._capture_loop(lambda: stream.read(1024), 1024)   # noqa: SLF001
        assert stream.reads == 3                                    # 3 回続けて失敗したら抜ける
        assert thread._stream_failed and "Stream closed" in thread._stream_failed   # noqa: SLF001
        assert sum("Audio read error" in r.getMessage() for r in caplog.records) == 1

    def test_one_failed_read_is_retried(self, dropping_pyaudio):
        stream = _Stream([OSError(-9981, "Input overflowed"), b"\x00" * 2048, b"\x00" * 2048])
        thread = AudioThread(audio_queue=make_audio_queue(), transcriber=SimpleNamespace(ready=True),
                             stop_event=threading.Event())
        thread._capture_loop(lambda: stream.read(1024), 1024)       # noqa: SLF001
        assert thread._stream_failed is None and stream.reads == 4  # noqa: SLF001

    def test_the_mic_is_reopened_by_name_after_it_drops(self, dropping_pyaudio):
        first, second = _Stream(_closed_errors(5)), _Stream([b"\x00" * 2048])
        dropping_pyaudio.streams = [first, second]
        notes: list[str] = []
        thread = AudioThread(audio_queue=make_audio_queue(), device_names=["DJI Mic Mini 2"],
                             transcriber=SimpleNamespace(ready=True), stop_event=threading.Event(),
                             on_status=notes.append)
        thread.run()                                                # 2 つ目のストリームが尽きたら止まる
        assert _FakePA.opened == [1, 1] and dropping_pyaudio.created == 2   # PyAudio を作り直して名前で探す
        assert first.closed and first.reads == 3
        assert len(notes) == 2 and "マイクが切れました" in notes[0] and "開き直しました" in notes[1]
        assert thread.health["state"] == "stopped"

    def test_waiting_for_the_mic_stops_when_the_logger_stops(self, dropping_pyaudio):
        dropping_pyaudio.streams = [_Stream(_closed_errors(3))]     # 開き直しはずっと失敗する
        stop = threading.Event()
        notes: list[str] = []
        thread = AudioThread(audio_queue=make_audio_queue(), device_names=["DJI Mic Mini 2"],
                             transcriber=SimpleNamespace(ready=True), stop_event=stop,
                             on_status=notes.append)
        original = thread._open_stream                             # noqa: SLF001
        tries = []

        def open_and_maybe_stop(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("reopening"):
                tries.append(1)
                if len(tries) >= 3:
                    stop.set()
            return result

        thread._open_stream = open_and_maybe_stop                  # noqa: SLF001
        thread.run()
        assert len(tries) == 3 and thread.health["state"] == "stopped"
        assert len(notes) == 1                                      # 切れたことを 1 回だけ知らせる


class TestMicAtStart:
    """店舗 2026-10-07: 起動のときだけ RDP の音声が接続元に回っていてマイクを開けず、そのあと開けるようになっても
    ロガーは最後まで音声なしだった。起動のときに開けなくても開き直しを続け、開けたら聞き取りを始める。"""

    def test_a_mic_that_appears_later_is_opened(self, dropping_pyaudio):
        dropping_pyaudio.streams = []                               # 起動のときは開けない
        notes: list[str] = []
        thread = AudioThread(audio_queue=make_audio_queue(), device_names=["DJI Mic Mini 2"],
                             transcriber=SimpleNamespace(ready=True), stop_event=threading.Event(),
                             on_status=notes.append)
        original = thread._open_stream                             # noqa: SLF001
        reasons = []

        def connect_after_the_first_try(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is None and not kwargs.get("reopening"):
                reasons.append(thread.health.get("error"))          # 起動時の表示に出す理由は残っている
                dropping_pyaudio.streams = [_Stream([b"\x00" * 2048])]   # このあとつながる
            return result

        thread._open_stream = connect_after_the_first_try          # noqa: SLF001
        thread.run()                                                # ストリームが尽きたら止まる
        assert reasons and "Invalid input device" in reasons[0]
        assert _FakePA.opened == [1, 1] and dropping_pyaudio.created == 2   # 作り直して名前で探した
        assert len(notes) == 1 and "マイクを開けました" in notes[0] and "聞き取りを始めます" in notes[0]
        assert thread.health["state"] == "stopped"

    def test_the_reason_at_start_stays_while_waiting(self, rdp_pyaudio, monkeypatch):
        import audio.recorder as recorder

        monkeypatch.setattr(recorder, "_REOPEN_FIRST_SEC", 0.0)
        stop = threading.Event()
        notes: list[str] = []
        thread = AudioThread(audio_queue=make_audio_queue(), device_names=["Wireless Mic Rx"],
                             transcriber=SimpleNamespace(ready=True), stop_event=stop, on_status=notes.append)
        original = thread._open_stream                             # noqa: SLF001
        tries = []

        def open_and_maybe_stop(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("reopening"):
                tries.append(1)
                if len(tries) >= 2:
                    stop.set()
            return result

        thread._open_stream = open_and_maybe_stop                  # noqa: SLF001
        thread.run()
        assert len(tries) == 2                                      # 起動のあとも開き直しを続けた
        assert thread.health["state"] == "error" and thread.health["rdp_audio"]   # 起動時の理由のまま
        assert notes == []                                          # 「切れました」とは知らせない

"""tests/test_read_corpus.py

読み上げ集（`tools/read_corpus.py`, テスト方針 週 1）: 句と正解・マイクの録音と切り出し・読む人の操作・本番と同じ
経路の聞き取り・評価・画面（真のアクション入力のサーバに載せた `/corpus`）。マイクは偽物（合成した音）で試す。
"""
from __future__ import annotations

import json
import math
import struct
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

from audio.recognizer import Recognition
from tools import read_corpus as rc
from tools.ground_truth_ui import make_server

RATE = 16000
CHUNK_SEC = rc.CHUNK / RATE


def tone(sec: float, amp: int = 4000) -> bytes:
    n = int(sec * RATE)
    return struct.pack(f"{n}h", *(int(amp * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(n)))


def silence(sec: float) -> bytes:
    return b"\x00\x00" * int(sec * RATE)


def feed_audio(rec: rc.DeviceRecorder, pcm: bytes, start: float) -> float:
    """pcm を 1024 サンプルずつ、録った時刻（チャンクの終わり）を付けて渡す。終わりの時刻を返す。"""
    step = rc.CHUNK * 2
    t = start
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
        t += len(chunk) / 2 / RATE
        rec.feed(chunk, t)
    return t


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ───────────────────────── 句 ─────────────────────────


class TestPhrases:
    def test_clean_text_reads_as_the_expected_actions(self):
        """画面の言葉そのものは、いまの読み取り（+ エンジンの言い直しの扱い = 評価と同じ比べ方）で正解どおりに読める
        （言い直しの「コール、はい、コール」だけは違うと分かっている）。"""
        for p in rc.PHRASES:
            got = rc.engine_actions(rc.parse_keys(p.text))
            if p.known_gap:
                assert got != rc.engine_actions(list(p.expect)), p
            else:
                assert got == rc.engine_actions(list(p.expect)), p

    def test_the_phrases_are_the_store_speech(self):
        """席番号・ポジション・ベット / レイズの語は言わない（額だけ）。オーナー 2026-09-30。"""
        for p in rc.PHRASES:
            for word in ("シート", "ベット", "レイズ", "ウィナー", "ボタン", "スモール", "ビッグ", "カットオフ",
                         "ハイジャック", "UTG", "ハンド開始", "ハンド終了"):
                assert word not in p.text, p
        kinds = {p.kind for p in rc.PHRASES}
        assert {"amount", "sequence", "street", "hand_name", "restate", "none"} <= kinds
        assert not kinds & {"bet", "raise", "position", "seat", "wording"}

    def test_ids_are_unique_and_every_kind_has_a_label(self):
        ids = [p.id for p in rc.PHRASES]
        assert len(ids) == len(set(ids)) and 120 <= len(ids) <= 200
        assert {p.kind for p in rc.PHRASES} <= set(rc.KIND_LABELS)
        assert any(not p.expect for p in rc.PHRASES)            # アクションではない言葉も読む

    def test_reading_order_is_fixed_per_round_and_never_repeats_a_phrase_back_to_back(self):
        one, two = rc.reading_order(rc.PHRASES, 1), rc.reading_order(rc.PHRASES, 2)
        assert one == rc.reading_order(rc.PHRASES, 1)
        assert sorted(p.id for p in one) == sorted(p.id for p in rc.PHRASES)
        assert [p.id for p in one] != [p.id for p in two]
        for order in (one, two):
            assert all(a.text != b.text for a, b in zip(order, order[1:]))

    @pytest.mark.parametrize("n,text", [(900, "900"), (10000, "1万"), (12000, "1万2千"), (12500, "1万2500"),
                                        (30000, "3万")])
    def test_amount_text(self, n, text):
        assert rc.amount_text(n) == text
        assert rc.parse_keys(f"ベット {text}") == [f"bet {n}"]

    def test_keys(self):
        assert rc.parse_keys("ボタン レイズ 1500") == ["raise 1500 @BTN"]
        assert rc.parse_keys("シート3 ウィナー") == ["winner @3"]
        assert rc.parse_keys("600点コールです") == ["call"]            # コールの額は比べない
        assert rc.parse_keys("コール 3ウェイ") == ["call", "players_left 3"]   # 残りの人数
        assert rc.engine_key("amount 1300") == rc.engine_key("raise 1300") == "wager 1300"
        assert rc.engine_key("call @BTN") == "call @BTN"


# ───────────────────────── 録音と切り出し ─────────────────────────


class TestDeviceRecorder:
    def _numbered(self, rec: rc.DeviceRecorder, chunks: int, start: float = 1000.0, scale: float = 1.0) -> None:
        for i in range(chunks):
            rec.feed(struct.pack(f"{rc.CHUNK}h", *([i] * rc.CHUNK)), start + (i + 1) * CHUNK_SEC * scale)

    def test_cut_by_time(self):
        rec = rc.DeviceRecorder("m1", 1, "mic", RATE)
        self._numbered(rec, 100)
        pcm = rec.cut(1000.0 + 10 * CHUNK_SEC, 1000.0 + 20 * CHUNK_SEC)
        samples = struct.unpack(f"{len(pcm) // 2}h", pcm)
        assert len(samples) == 10 * rc.CHUNK and samples[0] == 10 and samples[-1] == 19

    def test_clock_drift_does_not_accumulate(self):
        """マイクの時計が PC より 0.1% 速くても、いちばん近いチャンクから数えるので 1 分後の切り出しもずれない。"""
        rec = rc.DeviceRecorder("m1", 1, "mic", RATE)
        self._numbered(rec, 1000, scale=1.001)
        t = 1000.0 + 900 * CHUNK_SEC * 1.001
        pcm = rec.cut(t, t + 5 * CHUNK_SEC * 1.001)
        assert struct.unpack("h", pcm[:2])[0] == 900

    def test_old_audio_is_dropped(self):
        rec = rc.DeviceRecorder("m1", 1, "mic", RATE, ring_sec=2.0)
        self._numbered(rec, 200)                                  # 12.8 秒
        assert rec.cut(1000.0, 1000.0 + 5 * CHUNK_SEC) == b""
        assert rec.cut(1000.0 + 190 * CHUNK_SEC, 1000.0 + 195 * CHUNK_SEC)

    def test_level_is_the_recent_peak(self):
        rec = rc.DeviceRecorder("m1", 1, "mic", RATE)
        end = feed_audio(rec, tone(0.2), 1000.0)
        assert rec.level(end) > 1000
        assert rec.level(end + 1.0) == 0.0

    def test_full_recording(self, tmp_path):
        rec = rc.DeviceRecorder("m1", 1, "mic", RATE, full_path=tmp_path / "full_m1.wav")
        feed_audio(rec, tone(0.5), 1000.0)
        rec.close()
        with wave.open(str(tmp_path / "full_m1.wav"), "rb") as w:
            assert w.getframerate() == RATE and w.getnframes() == int(0.5 * RATE)


def _session(tmp_path: Path, clock: Clock, *, quiet: bool = False, n: int = 3, saved=None):
    folder = tmp_path / "corpus" / "20261001_190000_owner"
    folder.mkdir(parents=True)
    recs = [rc.DeviceRecorder("m1", 1, "USB", RATE), rc.DeviceRecorder("m2", 3, "Headset", RATE)]
    phrases = rc.PHRASES[:n]
    meta = {"round": 1, "order": [p.id for p in phrases]}
    rc.write_meta(folder, meta)
    session = rc.CorpusSession(folder, meta, phrases, recs, clock=clock, on_saved=saved)
    return session, folder, recs


def _speak(recs, clock: Clock, sec: float, amp: int = 4000) -> None:
    for rec in recs:
        feed_audio(rec, tone(sec, amp), clock.t)
    clock.t += sec


class TestSession:
    def test_next_saves_each_mic_after_the_tail_arrives(self, tmp_path):
        clock = Clock()
        got = []
        session, folder, recs = _session(tmp_path, clock, saved=lambda f, row, pcms: got.append((row, pcms)))
        _speak(recs, clock, 1.0)
        assert session.next(0) and session.index == 1
        assert session.process_pending(clock()) == []             # 語尾（POST_ROLL）がまだ
        _speak(recs, clock, rc.POST_ROLL + 0.1)
        [row] = session.process_pending(clock())
        assert row["id"] == rc.PHRASES[0].id and row["take"] == 1 and row["position"] == 1
        assert set(row["files"]) == {"m1", "m2"} and not row["quiet"] and row["peak"]["m1"] > 1000
        assert row["mics"] == {"m1": "USB", "m2": "Headset"} and row["expect"] == list(rc.PHRASES[0].expect)
        with wave.open(str(folder / row["files"]["m1"]), "rb") as w:
            # 句を出してから「次へ」まで 1.0 秒 + 余白（前は録り始める前なので無い）
            assert abs(w.getnframes() / RATE - (1.0 + rc.POST_ROLL)) < 0.1
        assert rc.read_labels(folder) == [row]
        assert got and set(got[0][1]) == {"m1", "m2"}

    def test_quiet_take_is_flagged(self, tmp_path):
        clock = Clock()
        session, _, recs = _session(tmp_path, clock)
        _speak(recs, clock, 1.0, amp=50)
        session.next(0)
        _speak(recs, clock, 1.0, amp=50)
        [row] = session.process_pending(clock())
        assert row["quiet"] and session.quiet == 1

    def test_double_tap_does_not_skip_a_phrase(self, tmp_path):
        clock = Clock()
        session, _, recs = _session(tmp_path, clock)
        _speak(recs, clock, 0.5)
        assert session.next(0)
        assert not session.next(0) and session.index == 1        # 古い画面からの 2 回目は無視
        assert not session.skip(0) and session.index == 1

    def test_redo_back_skip_and_resume(self, tmp_path):
        clock = Clock()
        session, folder, recs = _session(tmp_path, clock)
        _speak(recs, clock, 0.5)
        session.redo()                                            # 読み間違えた: ここから録り直し
        t_redo = clock.t
        _speak(recs, clock, 0.5)
        session.next(0)
        assert session.pending[0].t0 == t_redo
        session.skip(1)
        assert session.back() and session.index == 1              # 飛ばした句に戻って読む
        _speak(recs, clock, 0.5)
        session.next(1)
        _speak(recs, clock, 1.0)
        rows = session.process_pending(clock())
        assert [(r["id"], r["take"]) for r in rows] == [(rc.PHRASES[0].id, 1), (rc.PHRASES[1].id, 2)]
        labels = rc.read_labels(folder)
        assert [r.get("skipped", False) for r in labels] == [True, False, False]
        assert rc.latest_labels(folder)[rc.PHRASES[1].id]["take"] == 2
        index, takes = rc.resume_point(folder, rc.PHRASES[:3])
        assert index == 2 and takes == {rc.PHRASES[0].id: 1, rc.PHRASES[1].id: 2}

    def test_finish_saves_the_pending_take_and_closes_meta(self, tmp_path):
        clock = Clock()
        session, folder, recs = _session(tmp_path, clock, n=1)
        _speak(recs, clock, 0.5)
        session.next(0)
        assert session.reading_done
        session.finish(wait_sec=0)
        meta = rc.read_meta(folder)
        assert meta["finished"] and meta["saved"] == 1 and meta["skipped"] == 0 and meta["stopped_at"]
        assert not session.next(0) and not session.back()
        assert all(not r.alive for r in recs)


# ───────────────────────── 聞き取り ─────────────────────────


class FakeTranscriber:
    ready = True
    load_error = None

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def recognize(self, audio_bytes):
        self.calls += 1
        r = self.results.pop(0)
        return r if isinstance(r, Recognition) else Recognition(r, 0.9)


class FakeEarResult:
    def __init__(self, d):
        self.d = d

    def to_dict(self):
        return dict(self.d)


class FakeEar:
    def __init__(self, text, candidate=None):
        self.text, self.candidate = text, candidate or text
        self.calls = 0

    def hear(self, samples):
        self.calls += 1
        return FakeEarResult({"text": self.text, "logp": -2.0,
                              "candidates": [{"text": self.candidate, "logp": -1.0}]})


def _listener(results, ear=None) -> rc.Listener:
    return rc.Listener({"sample_rate": RATE, "speech_rms": 300, "min_speech_sec": 0.15},
                       make_transcriber=lambda: FakeTranscriber(results), make_ear=(lambda: ear))


def _two_words() -> bytes:
    return silence(0.4) + tone(0.5) + silence(0.8) + tone(0.5) + silence(0.6)


class TestTranscribe:
    LABEL = {"id": "rai08", "take": 1, "files": {"m1": "rai08_t1_m1.wav"}}

    def test_same_segmentation_and_reading_as_live(self):
        """間をあけて言った「レイズ」「1300」は本番と同じく 2 つの発話に分かれ、それぞれ読む。"""
        listener = _listener(["レイズ", "1300"], ear=FakeEar("レイズ"))
        row = listener.take_row(self.LABEL, "m1", _two_words())
        assert row["id"] == "rai08" and row["mic"] == "m1" and row["file"] == "rai08_t1_m1.wav"
        assert [s["text"] for s in row["segments"]] == ["レイズ", "1300"]
        assert row["heard"] == ["raise", "amount 1300"]
        assert all(s["ear"] for s in row["segments"])             # 比べるため、読めた発話も第 2 の耳で聞く
        assert listener.hearer._audio_queue.empty()

    def test_second_ear_only_when_whisper_cannot_read(self):
        listener = _listener(["", "これ"], ear=FakeEar("フォールド"))
        row = listener.take_row(self.LABEL, "m1", _two_words())
        assert row["heard"] == ["fold", "fold"]
        assert row["segments"][0]["text"] == "" and row["segments"][1]["whisper"] == []

    def test_no_speech_is_not_heard_again(self):
        ear = FakeEar("フォールド")
        listener = _listener([Recognition("", None, no_speech=True)], ear=ear)
        row = listener.take_row(self.LABEL, "m1", silence(0.3) + tone(0.4) + silence(0.7))
        assert row["heard"] == [] and ear.calls == 0 and row["segments"][0]["no_speech"]

    def test_short_sounds_are_dropped_like_live(self):
        listener = _listener([])
        row = listener.take_row(self.LABEL, "m1", silence(0.3) + tone(0.07, amp=1000) + silence(0.7))
        assert row["segments"] == [] and row["heard"] == []

    def test_worker_writes_transcripts(self, tmp_path):
        worker = rc.AsrWorker({}, listener_factory=lambda: _listener(["チェック"]))
        folder = tmp_path / "f"
        folder.mkdir()
        worker.submit(folder, {"id": "act01", "take": 1, "files": {"m1": "x.wav"}}, {"m1": silence(0.2) + tone(0.4)})
        for _ in range(100):
            if worker.done:
                break
            time.sleep(0.02)
        worker.close()
        assert worker.status()["state"] == "ready"
        [row] = rc._read_jsonl(folder / rc.TRANSCRIPTS)
        assert row["id"] == "act01" and row["heard"] == ["check"]

    def test_worker_without_models_keeps_recording(self, tmp_path):
        class Broken:
            ready = False
            load_error = "no model"

        worker = rc.AsrWorker({}, listener_factory=lambda: rc.Listener(
            {"sample_rate": RATE}, make_transcriber=lambda: Broken(), make_ear=lambda: None))
        worker.submit(tmp_path, {"id": "act01", "take": 1}, {"m1": tone(0.3)})
        for _ in range(100):
            if worker.state == "unavailable" and not worker.backlog():
                break
            time.sleep(0.02)
        worker.close()
        assert worker.state == "unavailable" and "no model" in worker.message
        assert not (tmp_path / rc.TRANSCRIPTS).exists()


# ───────────────────────── 評価 ─────────────────────────


def _corpus_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "logs" / "corpus" / "20261001_190000_owner"
    folder.mkdir(parents=True)
    rc.write_meta(folder, {"speaker": "オーナー", "round": 1, "order": ["rai08", "act01", "neg01"]})
    labels = [
        {"id": "rai08", "text": "レイズ 1300", "kind": "raise", "expect": ["raise 1300"], "take": 1,
         "files": {"m1": "rai08_t1_m1.wav"}, "mics": {"m1": "USB"}},
        {"id": "act01", "text": "チェック", "kind": "action", "expect": ["check"], "take": 1,
         "files": {"m1": "act01_t1_m1.wav"}, "mics": {"m1": "USB"}},
        {"id": "neg01", "text": "ポット 3000", "kind": "none", "expect": [], "take": 1, "skipped": True},
    ]
    transcripts = [
        {"id": "rai08", "take": 1, "mic": "m1", "segments": [{"sec": 1.0, "text": "1300", "ear": None}]},
        {"id": "act01", "take": 1, "mic": "m1", "segments": [
            {"sec": 0.6, "text": "これ", "ear": {"text": "チェック", "logp": -1.0,
                                                "candidates": [{"text": "チェック", "logp": -0.5}]}}]},
    ]
    for name, rows in ((rc.LABELS, labels), (rc.TRANSCRIPTS, transcripts)):
        (folder / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    for label in labels[:2]:
        rc.write_wav(folder / label["files"]["m1"], tone(0.5), RATE)
    return folder


class TestEvaluate:
    def test_routes_and_equivalence(self, tmp_path):
        result = rc.evaluate_folder(_corpus_folder(tmp_path))
        assert result["phrases"] == 2 and result["transcribed"] == 2 and result["speaker"] == "オーナー"
        m1 = result["mics"]["m1"]
        assert m1["name"] == "USB"
        # 「1300」だけでも、レイズかベットかはエンジンが決める = 一致（完全一致ではない）
        assert m1["live"]["ok"] == 2 and m1["live"]["exact"] == 1 and m1["live"]["rate"] == 1.0
        assert m1["whisper"]["ok"] == 1                            # Whisper だけなら「これ」は読めない
        assert m1["ear"]["ok"] == 1                                # 第 2 の耳の候補は「チェック」だけ
        assert m1["by_kind"] == {"action": {"ok": 1, "n": 1}, "raise": {"ok": 1, "n": 1}}
        assert m1["misses"] == []

    def test_report_lists_misses(self, tmp_path):
        folder = _corpus_folder(tmp_path)
        rows = rc._read_jsonl(folder / rc.TRANSCRIPTS)
        rows[1]["segments"][0]["ear"] = None
        (folder / rc.TRANSCRIPTS).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                             encoding="utf-8")
        result = rc.evaluate_folder(folder)
        [miss] = result["mics"]["m1"]["misses"]
        assert miss["id"] == "act01" and miss["got"] == [] and miss["heard"] == "これ"
        text = "\n".join(rc.format_report(result))
        assert "マイク m1（USB）" in text and "act01「チェック」→ 聞こえた「これ」" in text

    def test_eval_command_reads_a_folder(self, tmp_path, capsys):
        folder = _corpus_folder(tmp_path)
        assert rc.main(["eval", str(folder.parent.parent)]) == 0
        assert "本番の経路 2/2" in capsys.readouterr().out

    def test_transcribe_folder_only_does_what_is_missing(self, tmp_path):
        folder = _corpus_folder(tmp_path)
        (folder / rc.TRANSCRIPTS).unlink()
        listener = _listener(["レイズ 1300", "チェック"])
        assert rc.transcribe_folder(folder, listener, log=lambda _: None) == 2
        assert rc.transcribe_folder(folder, listener, log=lambda _: None) == 0
        assert rc.evaluate_folder(folder)["mics"]["m1"]["live"]["exact"] == 2
        listener2 = _listener(["1300", "チェック"])
        assert rc.transcribe_folder(folder, listener2, redo=True, log=lambda _: None) == 2
        assert len(list(folder.glob("transcripts_*.jsonl"))) == 1  # 前の結果は残す


# ───────────────────────── 画面のサーバ ─────────────────────────


class FakeStream:
    def __init__(self, amp: int) -> None:
        self.amp = amp
        self.closed = False
        self._chunk = tone(rc.CHUNK / RATE, amp)

    def read(self, n, exception_on_overflow=False):
        if self.closed:
            return b""
        time.sleep(n / RATE)
        return self._chunk

    def close(self):
        self.closed = True


class FakeBackend:
    DEVICES = [{"index": 1, "name": "USB マイク", "api": "MME", "rate_ok": True, "default": True},
               {"index": 3, "name": "ヘッドセット", "api": "MME", "rate_ok": True, "default": False},
               {"index": 5, "name": "44.1k だけ", "api": "WASAPI", "rate_ok": False, "default": False}]

    def __init__(self):
        self.opened = []
        self.closed = False

    def devices(self, rate):
        return [dict(d) for d in self.DEVICES]

    def open(self, index, rate):
        stream = FakeStream(4000)
        self.opened.append((index, stream))
        return stream

    def close(self):
        self.closed = True


def _app(log_dir: Path) -> rc.CorpusApp:
    return rc.CorpusApp(log_dir, audio_cfg={"sample_rate": RATE, "device_id": 3, "speech_rms": 300},
                        backend_factory=FakeBackend, use_asr=False)


def _wait(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class WdmKsOnlyBackend(FakeBackend):
    """RDP の音声が接続元に回っているときの一覧（店舗 2026-09-30）。Bluetooth のマイクは WDM-KS で開けない。"""
    DEVICES = [{"index": 11, "name": "Headset (…DJI Mic Mini 2-ED8C97)", "api": "Windows WDM-KS", "rate_ok": True,
                "default": False},
               {"index": 13, "name": "Headset (…DJI Mic Mini 2-EBA00C)", "api": "Windows WDM-KS", "rate_ok": True,
                "default": False}]

    def open(self, index, rate):
        raise OSError(-9999, "Unanticipated host error")


class TestApp:
    def test_setup_state_marks_the_configured_mic(self, tmp_path):
        app = _app(tmp_path)
        st = app.state()
        assert not st["recording"] and st["session"] is None and st["recent"] == []
        assert [d["index"] for d in st["devices"] if d["configured"]] == [3]
        assert st["asr"]["state"] == "off" and st["total_phrases"] == len(rc.PHRASES)
        assert st["device_hint"] is None
        app.close()

    def test_named_mics_are_marked_in_the_order_of_the_names(self, tmp_path):
        """config の audio.device_name（優先順）で選んだマイクに印 = 画面は m1・m2 にその順で選ぶ（番号は見ない）。"""
        app = rc.CorpusApp(tmp_path, audio_cfg={"sample_rate": RATE, "device_id": 5,
                                                "device_name": "ヘッドセット|Yeti|USB マイク"},
                           backend_factory=FakeBackend, use_asr=False)
        st = app.state()
        marked = sorted((d["configured_rank"], d["index"]) for d in st["devices"] if d["configured"])
        assert marked == [(0, 3), (1, 1)]
        assert st["device_names"] == ["ヘッドセット", "Yeti", "USB マイク"] and st["device_hint"] is None
        app.close()

    def test_missing_named_mics_are_explained(self, tmp_path):
        """名前のマイクが 1 本も無ければ印を付けず（画面は既定のマイクを選ばない）、つなぎ方を案内する。"""
        app = rc.CorpusApp(tmp_path, audio_cfg={"sample_rate": RATE, "device_id": 1, "device_name": "Wireless Mic Rx"},
                           backend_factory=FakeBackend, use_asr=False)
        st = app.state()
        assert not any(d["configured"] for d in st["devices"])
        assert "「Wireless Mic Rx」" in st["device_hint"] and "Bluetooth" in st["device_hint"]
        status, payload = app.route("GET", "/api/corpus/devices")
        assert status == 200 and payload["device_names"] == ["Wireless Mic Rx"] and "見つかりません" in payload["device_hint"]
        app.close()

    def test_only_wdm_ks_mics_explain_the_remote_desktop_setting(self, tmp_path):
        app = rc.CorpusApp(tmp_path, audio_cfg={"sample_rate": RATE, "device_id": 1}, backend_factory=WdmKsOnlyBackend,
                           use_asr=False)
        st = app.state()
        assert "リモート PC で再生" in st["device_hint"]
        status, payload = app.start({"speaker": "Leo", "mics": [13]})
        assert status == 400 and "Unanticipated host error" in payload["message"]
        assert "MME の番号を選んでください" in payload["message"]
        app.close()

    @pytest.mark.parametrize("body,message", [
        ({"mics": [1]}, "名前"),
        ({"speaker": "x", "mics": []}, "マイク"),
        ({"speaker": "x", "mics": [1, 3, 5]}, "マイク"),
        ({"speaker": "x", "mics": [7]}, "見つかりません"),
        ({"speaker": "x", "mics": [1, 1]}, "マイク"),
    ])
    def test_start_is_validated(self, tmp_path, body, message):
        app = _app(tmp_path)
        status, payload = app.start(body)
        assert status == 400 and message in payload["message"]
        app.close()

    def test_read_finish_and_resume(self, tmp_path):
        app = _app(tmp_path)
        status, st = app.start({"speaker": "reo", "mics": [3, 1], "round": 2})
        assert status == 200 and st["recording"]
        s = st["session"]
        assert s["index"] == 0 and s["round"] == 2 and [m["label"] for m in s["mics"]] == ["m1", "m2"]
        folder = tmp_path / "corpus" / s["folder"]
        meta = rc.read_meta(folder)
        assert meta["speaker"] == "reo" and meta["order"] == [p.id for p in rc.reading_order(rc.PHRASES, 2)]
        assert meta["runs"][0]["mics"][0]["index"] == 3
        assert app.start({"speaker": "reo", "mics": [1]})[0] == 409
        time.sleep(0.3)
        assert app.act("next", {"index": 0})[1]["session"]["index"] == 1
        assert _wait(lambda: len(rc.read_labels(folder)) == 1)
        row = rc.read_labels(folder)[0]
        assert row["id"] == meta["order"][0] and set(row["files"]) == {"m1", "m2"}
        assert row["mics"] == {"m1": "ヘッドセット", "m2": "USB マイク"}
        assert (folder / "full_m1.wav").is_file()
        assert app.act("finish", {})[0] == 200
        st = app.state()
        assert not st["recording"] and st["session"]["finished"]
        [recent] = st["recent"]
        assert recent["folder"] == folder.name and recent["saved"] == 1 and not recent["finished"]
        assert rc.read_meta(folder)["stopped_at"]
        # 続きから: 2 句目から、同じフォルダに
        status, st = app.start({"resume": folder.name, "mics": [1]})
        assert status == 200 and st["session"]["index"] == 1 and st["session"]["folder"] == folder.name
        assert (folder / "full_m1.wav").is_file() and list(folder.glob("full_m1_*.wav"))
        app.act("finish", {})
        assert len(rc.read_meta(folder)["runs"]) == 2
        app.close()

    def test_a_reading_of_the_old_phrase_set_is_not_resumed(self, tmp_path):
        """句の組を作り直すと同じ ID が別の句を指す（2026-09-30: 店の言い方に作り直した）。前の組の続きは読まない。"""
        app = _app(tmp_path)
        status, st = app.start({"speaker": "reo", "mics": [1]})
        folder = tmp_path / "corpus" / st["session"]["folder"]
        app.act("finish", {})
        meta = rc.read_meta(folder)
        meta["phrase_set"] = "2026-09-30"
        (folder / rc.META).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        [recent] = app.state()["recent"]
        assert recent["current_set"] is False
        status, payload = app.start({"resume": folder.name, "mics": [1]})
        assert status == 409 and "新しく始めてください" in payload["message"]
        app.close()

    def test_reading_the_last_phrase_finishes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "PHRASES", rc.PHRASES[:1])
        app = _app(tmp_path)
        app.start({"speaker": "reo", "mics": [1]})
        time.sleep(0.2)
        app.act("next", {"index": 0})
        assert _wait(lambda: app.session.finished)
        assert rc.read_meta(app.session.folder)["finished"]
        app.close()


def _req(base: str, method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


class TestServedFromGroundTruthUi:
    def test_page_state_start_next_audio(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        server = make_server(log_dir, "127.0.0.1", 0, corpus_factory=_app)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            status, body, _ = _req(base, "GET", "/corpus")
            assert status == 200 and "読み上げ集" in body.decode("utf-8")
            status, body, _ = _req(base, "GET", "/")
            assert 'href="/corpus"' in body.decode("utf-8")
            status, body, _ = _req(base, "GET", "/api/corpus/state")
            assert status == 200 and len(json.loads(body)["devices"]) == 3
            status, body, _ = _req(base, "POST", "/api/corpus/start", {"speaker": "reo", "mics": [1]})
            assert status == 200
            folder = json.loads(body)["session"]["folder"]
            time.sleep(0.3)
            assert _req(base, "POST", "/api/corpus/next", {"index": 0})[0] == 200
            assert _wait(lambda: json.loads(_req(base, "GET", "/api/corpus/state")[1])["session"]["last"])
            last = json.loads(_req(base, "GET", "/api/corpus/state")[1])["session"]["last"]
            status, body, headers = _req(base, "GET", f"/api/corpus/audio/{folder}/{last['file']}")
            assert status == 200 and headers["Content-Type"] == "audio/wav" and body[:4] == b"RIFF"
            status, body, _ = _req(base, "GET", f"/api/corpus/audio/{folder}/{last['file']}",
                                   headers={"Range": "bytes=0-3"})
            assert status == 206 and body == b"RIFF"
            assert _req(base, "GET", f"/api/corpus/audio/{folder}/..%2Fmeta.json")[0] == 404
            assert _req(base, "POST", "/api/corpus/nope", {})[0] == 404
            assert _req(base, "POST", "/api/corpus/finish", {})[0] == 200
            assert _req(base, "POST", "/api/corpus/next", {"index": 1})[0] == 409
        finally:
            server.shutdown()
            server.server_close()


def test_the_page_does_not_rebuild_a_field_being_typed_in():
    """2 秒ごとの読み直しで画面を作り直すと、名前の欄のキーボードが消えて打ちかけの文字も消えた（店舗 2026-09-30 の
    画面録画）。文字の欄・ロールダウンを触っている間は作り直さず、離れたら作り直す。"""
    from tools.read_corpus import CORPUS_PAGE

    assert "function editing()" in CORPUS_PAGE and "S.st = await api(\"state\"); renderWhenFree();" in CORPUS_PAGE
    assert 'addEventListener("focusout"' in CORPUS_PAGE
    assert 'oninput="S.speaker=this.value"' in CORPUS_PAGE          # 作り直しても打った名前が残る


def test_restatements_count_once_like_the_engine():
    """続けて同じオールイン・同じ額の賭けは言い直し（エンジンと同じ, オーナー 2026-09-30）。コールは 2 人のことがある。"""
    assert rc.engine_actions(["allin", "allin"]) == ["allin"]
    assert rc.engine_actions(["raise 1500", "amount 1500"]) == ["wager 1500"]
    assert rc.engine_actions(["bet 2000", "bet 2000"]) == ["wager 2000"]
    assert rc.engine_actions(["raise 600", "raise 1800"]) == ["wager 600", "wager 1800"]
    assert rc.engine_actions(["call", "call"]) == ["call", "call"]
    assert rc.engine_actions(["allin", "call", "allin"]) == ["allin", "call", "allin"]

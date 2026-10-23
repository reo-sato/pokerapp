"""tools/voice_style.py: 宣言の声と雑談の声の比べ（オーナーの問い 2026-10-03）。

合成の音で声の量（高さ・大きさ・区切り・録音の区切り方）を、作り物の値で数え方（同じセッションの中の AUC・区間・
並べ替え・割合の補正）を、作り物の書き起こしで発話の分け方を確かめる。CI に faster_whisper は無いので声の区間は
エネルギーだけ（`vad=None`）。
"""
from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np
import pytest

from tools import voice_style as vs

SR = vs.SAMPLE_RATE
CHUNK = vs.CHUNK


def tone(f0: float, sec: float, rms: float = 3000.0, amps=(1.0, 0.5, 0.33, 0.25), glide_to=None,
         first_harmonic: int = 1) -> np.ndarray:
    """倍音つきの音（`glide_to` があれば指数で上り下り = 半音で一定の速さ）。長さは塊の倍数に丸める。"""
    n = int(round(sec * SR / CHUNK)) * CHUNK
    t = np.arange(n) / SR
    if glide_to is None:
        phase = 2 * np.pi * f0 * t
    else:
        k = math.log(glide_to / f0) / (n / SR)
        phase = 2 * np.pi * f0 * (np.exp(k * t) - 1.0) / k
    x = sum(a * np.sin((i + first_harmonic) * phase) for i, a in enumerate(amps))
    return x / np.sqrt(np.mean(x ** 2)) * rms


def noise(n: int, rms: float = 30.0, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, rms, n)


def clip(*parts: np.ndarray, pre: int = 5, tail: int = 7, floor: float = 30.0) -> np.ndarray:
    """録音と同じ形: 前の無音 `pre` 塊 + 声 + 後ろの無音 `tail` 塊。"""
    body = np.concatenate(parts)
    return np.concatenate([noise(pre * CHUNK, floor, 1), body, noise(tail * CHUNK, floor, 2)])


def st(f0: float) -> float:
    return 12.0 * math.log2(f0 / 100.0)


# ───────────────────────── 声の高さ ─────────────────────────

@pytest.mark.parametrize("f0", [100.0, 150.0, 220.0, 330.0])
def test_pitch_of_harmonic_tones(f0):
    feats = vs.clip_features(clip(tone(f0, 0.6)))
    assert "skip" not in feats
    assert feats["f0"] == pytest.approx(st(f0), abs=0.2)


def test_missing_fundamental_reads_the_period():
    """110 Hz の 2〜6 倍音だけ（基本音なし）→ 110 Hz（周期で決まる）。"""
    x = tone(110.0, 0.6, amps=(1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 6), first_harmonic=2)
    assert vs.clip_features(clip(x))["f0"] == pytest.approx(st(110.0), abs=0.2)


def test_strong_second_harmonic_is_not_an_octave_up():
    feats = vs.clip_features(clip(tone(150.0, 0.6, amps=(0.3, 1.0, 0.2, 0.1))))
    assert feats["f0"] == pytest.approx(st(150.0), abs=0.2)


@pytest.mark.parametrize("end, sign", [(240.0, 1), (120.0, -1)])
def test_end_slope_follows_a_glide(end, sign):
    """12 半音を 0.8 秒で上る / 下る → 終わりの傾きは ±15 半音/秒。"""
    start = 120.0 if sign > 0 else 240.0
    feats = vs.clip_features(clip(tone(start, 0.8, glide_to=end)))
    assert feats["end_slope"] == pytest.approx(sign * 15.0, abs=3.0)


def test_noise_has_no_pitch():
    feats = vs.clip_features(clip(noise(10 * CHUNK, 3000.0, 5)))
    assert feats["skip"] == "no_pitch"


def test_pitch_holds_at_10_db_snr():
    x = tone(180.0, 0.6, rms=3000.0)
    x = x + noise(len(x), 3000.0 / math.sqrt(10.0), 7)
    assert vs.clip_features(clip(x))["f0"] == pytest.approx(st(180.0), abs=0.5)


def test_gain_changes_only_the_level():
    """0.1 倍の音量 → 大きさはちょうど −20 dB、高さ・高い音の比は同じ（前の無音も同じ倍率）。"""
    x = clip(tone(200.0, 0.6, rms=6000.0))
    loud, soft = vs.clip_features(x), vs.clip_features(x * 0.1)
    assert soft["level"] - loud["level"] == pytest.approx(-20.0, abs=0.01)
    assert soft["f0"] == pytest.approx(loud["f0"], abs=1e-6)
    assert soft["tilt"] == pytest.approx(loud["tilt"], abs=1e-6)
    assert soft["duration"] == loud["duration"]


# ───────────────────────── 録音の区切り方 ─────────────────────────

def test_gate_reproduces_the_recorder_chunks():
    x = clip(tone(200.0, 3 * CHUNK / SR))
    gate = vs.gate_of(x)
    assert len(gate.chunk_rms) == 15 and gate.voiced_chunks == 3
    assert not gate.starts_voiced and not gate.hard_cut


def test_continuation_hard_cut_and_short_clips_are_skipped():
    assert vs.clip_features(clip(tone(200.0, 1.0), pre=0))["skip"] == "continuation"
    assert vs.clip_features(clip(tone(200.0, 75 * CHUNK / SR), pre=4, tail=0))["skip"] == "hard_cut"
    assert vs.clip_features(clip(tone(200.0, 0.15)))["skip"] == "short"


def test_segments_split_at_pauses():
    x = clip(tone(150.0, 0.4, rms=2000.0), noise(int(0.3 * SR), 30.0, 3), tone(220.0, 0.4, rms=6000.0))
    segs = vs.clip_features(x)["segments"]
    assert len(segs) == 2
    assert segs[0]["f0"] == pytest.approx(st(150.0), abs=0.3)
    assert segs[1]["f0"] == pytest.approx(st(220.0), abs=0.3)
    assert segs[1]["level"] - segs[0]["level"] == pytest.approx(20 * math.log10(3.0), abs=0.5)
    assert segs[0]["t1"] < segs[1]["t0"]


# ───────────────────────── 数え方 ─────────────────────────

def test_rank_auc_matches_brute_force_with_ties():
    rng = np.random.default_rng(3)
    pos, neg = rng.integers(0, 5, 40).astype(float), rng.integers(0, 5, 30).astype(float)
    brute = np.mean([1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg])
    assert vs.auc(pos, neg) == pytest.approx(brute)


def test_within_session_auc_ignores_pairs_across_sessions():
    """セッション 1 は全体が大きく録れている。中では雑談の方が大きいので、中だけで数えると 0。"""
    groups = {"s1": (np.array([10.0]), np.array([11.0])), "s2": (np.array([0.0]), np.array([1.0]))}
    assert vs.within_auc(groups) == (0.0, 2)
    assert vs.auc([10.0, 0.0], [11.0, 1.0]) > 0.0


def test_matched_auc_counts_only_similar_lengths():
    groups = {"s": (np.array([5.0, 5.0]), np.array([0.0]), np.array([0.5, 3.0]), np.array([0.6]))}
    assert vs.matched_auc(groups) == (1.0, 1)


def test_bootstrap_is_seeded_and_keeps_both_classes():
    rng = np.random.default_rng(1)
    groups = {f"s{i}": (rng.normal(1, 1, 20), rng.normal(0, 1, 25)) for i in range(4)}
    assert vs.bootstrap_within(groups, 300) == vs.bootstrap_within(groups, 300)
    lo, hi = vs.bootstrap_auc([3.0], [0.0, 1.0, 2.0] * 10, 200)     # 引き直しで宣言が消えたらやり直す
    assert 0.0 <= lo <= hi <= 1.0 and not math.isnan(lo)


def test_permutation_p_small_when_separated_large_when_not():
    rng = np.random.default_rng(2)
    sep = {f"s{i}": (rng.normal(2, 1, 15), rng.normal(0, 1, 15)) for i in range(3)}
    same = {f"s{i}": (rng.normal(0, 1, 15), rng.normal(0, 1, 15)) for i in range(3)}
    assert vs.permutation_p(sep, 300) < 0.01
    assert vs.permutation_p(same, 300) > 0.05


def test_holm():
    assert vs.holm({"a": 0.01, "b": 0.04, "c": 0.03}) == pytest.approx({"a": 0.03, "b": 0.06, "c": 0.06})


def test_prior_shift_recovers_the_likelihood_ratio():
    """宣言 N(+1, 1)・雑談 N(−1, 1) なら log 尤度比 = 2x。学習側の割合（0.2）を除けば、割合によらず 2x に戻る。"""
    rng = np.random.default_rng(4)
    x = np.concatenate([rng.normal(1, 1, 2000), rng.normal(-1, 1, 8000)])
    y = np.concatenate([np.ones(2000), np.zeros(8000)])
    w = vs.fit_logistic(x[:, None], y, lam=0.0)
    grid = np.array([-1.0, 0.0, 1.0])
    p = 1 / (1 + np.exp(-(w[0] + w[1] * grid)))
    assert vs.llr_from_posterior(p, 0.2) == pytest.approx(2 * grid, abs=0.15)
    assert vs.r_from_llr(0.0) == pytest.approx(0.27)
    assert vs.r_from_llr(50.0) == pytest.approx(0.8) and vs.r_from_llr(-50.0) == pytest.approx(0.05)


def test_fused_compare_takes_the_declaration_by_text_order():
    def c(label, segs):
        return vs.Clip("s", 0.0, "", label, {"segments": [dict(f0=f, level=lv, z_index=f + lv) for f, lv in segs]})

    clips = [c("M_end", [(0.0, 0.0), (1.0, 1.0)]), c("M_start", [(2.0, 1.0), (0.0, 0.0), (0.5, 0.0)]),
             c("M_end", [(1.0, 1.0)])]
    r = vs.fused_compare(clips, n_boot=100)
    assert (r.auc, r.files, r.one_segment) == (1.0, 2, 1)
    assert r.f0_diff == pytest.approx((1.0 + 1.75) / 2) and r.by_position == {"M_end": 1, "M_start": 1}
    assert "数が足りず" in vs.fused_verdict(r)


# ───────────────────────── 発話の分け方 ─────────────────────────

def row(text: str, at: float = 100.0, **extra) -> dict:
    return dict(utterance_start_ts=at, text=text, audio_sec=1.0, confidence=0.9, no_speech=False, **extra)


@pytest.mark.parametrize("text, label", [
    ("コール", "D"), ("フォールドです", "D"), ("レイズ 1200", "D"),
    ("フルハウス", "A"), ("ヘッズアップ", "A"), ("ポット1万2000です", "A"), ("ターンカード", "A"),
    ("いやーきついなこれ", "C"), ("コールですか?", "question"), ("どうもありがとうございました。", "outro"),
    ("ご視聴ありがとうございました", "noise"),
    ("多分良いと思うんすけど、フォールド。", "M_end"), ("コール、いやー強いね", "M_start"),
    ("いや、コールしたらさ、まあいいかって", "mixed"), ("いやーこれは厳しいな、コール", "M_end"),
    ("今日は寒いですね", "question"),
    # 崩れて書き起こされた宣言（片仮名の語）は雑談ではない（混ざったファイルにも数えない）
    ("チェック アウンド", "mixed"), ("コール、チュック。", "mixed"),
    ("6番 レイズ ボタンを動かしてないよね 4番じゃないの", "M_start"),
])
def test_classify(text, label):
    assert vs.classify(row(text)) == label


@pytest.mark.parametrize("text, first, second", [
    ("アクションです。", "C", "A"), ("ターンです。", "C", "A"), ("シート3です。", "C", "A"),
    ("ピアクター", "C", "not_chat"), ("はい", "C", "not_chat"), ("あ、間違った。", "C", "C"),
])
def test_label_rules_2_moves_table_talk_and_garbled_words_out_of_chatter(text, first, second):
    """2 版（数字を見たあとに直した分け方）: 卓の言葉だけの発話は進行の言葉、雑談はひらがな・漢字の文だけ。"""
    assert vs.classify(row(text), rules=1) == first
    assert vs.classify(row(text), rules=2) == second


def test_chatter_far_from_any_betting_word_is_not_counted():
    rows = [row("コール", 100.0), row("いやーきついなこれ", 110.0), row("いやー寒いなあ", 200.0)]
    assert vs.label_session(rows) == {100.0: "D", 110.0: "C", 200.0: "far"}


def test_gt_matched_keeps_the_owner_of_each_word():
    record = {"hands": [{"hand_id": 1, "started_at": "2026-10-01T20:00:00", "ended_at": "2026-10-01T20:01:00"}]}
    from tools.eval_store import _epoch

    t0 = _epoch("2026-10-01T20:00:00")
    gt = {"hands": [{"hand_id": 1, "actions": [{"seat": 3, "action": "call"}, {"seat": 4, "action": "fold"}]}]}
    rows = [row("コール", t0 + 5), row("いやー", t0 + 7), row("チェック", t0 + 8), row("フォールド", t0 + 9)]
    assert vs.gt_matched(rows, record, gt) == {t0 + 5, t0 + 9}


def test_mic_lines_are_read_from_the_log(tmp_path):
    log = tmp_path / "pokerapp.log"
    log.write_text("2026-10-01 20:27:01,328 [AudioThread] INFO audio.recorder: AudioThread started "
                   "(device_id=1 'Headset (DJI Mic Mini 2-ED8C97 ', rate=16000)\n", encoding="utf-8")
    (when, name), = vs.mic_starts(log)
    assert name == "Headset (DJI Mic Mini 2-ED8C97" and when.hour == 20


# ───────────────────────── 通し ─────────────────────────

def write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(np.clip(x, -32768, 32767).astype("<i2").tobytes())


def test_run_end_to_end(tmp_path):
    """宣言は高く大きく、雑談は低く小さい作り物のセッション → 同じセッションの中の AUC 1.00。"""
    sid = "sessA0000"
    folder = tmp_path / "logs" / sid
    rows = []
    t = 1_790_000_000.0
    plan = [("コール", 230.0, 8000.0), ("いやーきついなこれ", 140.0, 2500.0), ("フォールド", 240.0, 9000.0),
            ("今日はよく入りますね", 150.0, 2000.0), ("チェック", 225.0, 8500.0), ("フルハウス", 235.0, 8000.0),
            ("そうなんですよねえ", 145.0, 2200.0)]
    for i, (text, f0, rms) in enumerate(plan):
        at = t + 3.0 * i
        name = f"{int(at * 1000)}.wav"
        write_wav(folder / "audio" / name, clip(tone(f0, 0.6, rms=rms)))
        rows.append(dict(row(text, at), audio_file=name))
    (folder / f"{sid}.transcripts.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                                                      encoding="utf-8")
    lines, clips, results, sessions = vs.run([tmp_path / "logs"], ["sessA"], vad=None, n_boot=50, n_perm=50)
    assert [s.session_id for s in sessions] == [sid]
    assert sorted(c.label for c in clips) == ["A", "C", "C", "C", "D", "D", "D"]
    main = next(iter(results.values()))
    assert main.index_auc == 1.0 and main.pairs == 9 and main.words_auc == 1.0
    assert main.f0_diff > 6 and main.level_diff > 8
    assert any("宣言 対 雑談 AUC 1.00" in line for line in lines)
    page = vs.render_html(lines, clips, ["sessA"])
    assert "<svg" in page and "data:audio/wav;base64," in page and "<script" not in page

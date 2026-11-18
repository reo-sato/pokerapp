"""tests/test_cloud_asr_relay.py

ADR-0072 D1（他の店に配る形, 2026-11-18）: 書き起こしをクラウドが中継する。店舗 PC は端末トークンで 1 発話の WAV を送り、
クラウドが自分の鍵（Secret Manager）で OpenAI を呼ぶ。店舗 PC に API の鍵を置かない。

- 端末トークンが無い・違う → 401。クラウドに鍵が無い → 503。WAV でない → 400。店ごとの 1 日の上限 → 429。
- API の失敗 → 502（店舗 PC は音声を残して後で送り直す = `RemoteAsrError`）。
- 音声・文・鍵はクラウドのログに出ない。
- 店舗 PC の `CloudTranscriber` は `OpenAITranscriber` と同じ約束で、方式の名前はクラウドが返す。
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from cloud.app import CloudSettings, create_cloud_app, wav_seconds  # noqa: E402
from cloud.storage import MemoryStorage  # noqa: E402
from cloud.tokens import hash_token, new_device_token  # noqa: E402
from core.remote_asr import CLOUD_ASR_PATH, CloudTranscriber, RemoteAsrError, wav_bytes  # noqa: E402

SECRET = "sk-test-secret-key-0123456789"
SPOKEN = "コール"


def _wav(sec: float) -> bytes:
    return wav_bytes(b"\x00\x00" * int(16000 * sec), 16000)


class FakeOpenAI:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.fail = fail

    def __call__(self, wav: bytes, *, key: str, model: str) -> str:
        self.calls.append((len(wav), key, model))
        if self.fail:
            raise RemoteAsrError("HTTP 500: upstream")
        return SPOKEN


def _app(**kw):
    storage = MemoryStorage()
    store = storage.create_store("店")
    token = new_device_token()
    storage.add_device(store["store_id"], hash_token(token))
    fake = kw.pop("fake", FakeOpenAI())
    settings = CloudSettings(asr_key=kw.pop("asr_key", SECRET), asr_call=fake, **kw)
    return TestClient(create_cloud_app(storage, settings)), token, fake


def test_the_cloud_transcribes_with_its_own_key(caplog):
    client, token, fake = _app(asr_model="gpt-transcribe")
    with caplog.at_level(logging.INFO, logger="cloud"):
        res = client.post(CLOUD_ASR_PATH, content=_wav(1.5),
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "audio/wav"})
    assert res.status_code == 200
    assert res.json() == {"text": SPOKEN, "asr": "openai-gpt-transcribe", "audio_sec": 1.5}
    assert fake.calls[0][1:] == (SECRET, "gpt-transcribe")
    logs = caplog.text
    assert SECRET not in logs and token not in logs and SPOKEN not in logs and "sec=1.5" in logs


def test_errors():
    client, token, _ = _app()
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post(CLOUD_ASR_PATH, content=_wav(1)).status_code == 401
    assert client.post(CLOUD_ASR_PATH, content=_wav(1), headers={"Authorization": "Bearer nope"}).status_code == 401
    res = client.post(CLOUD_ASR_PATH, content=b"not a wav", headers=auth)
    assert res.status_code == 400 and res.json()["code"] == "invalid_audio"
    no_key, token2, _ = _app(asr_key=None)
    res = no_key.post(CLOUD_ASR_PATH, content=_wav(1), headers={"Authorization": f"Bearer {token2}"})
    assert res.status_code == 503 and res.json()["code"] == "asr_unavailable"
    failing, token3, _ = _app(fake=FakeOpenAI(fail=True))
    res = failing.post(CLOUD_ASR_PATH, content=_wav(1), headers={"Authorization": f"Bearer {token3}"})
    assert res.status_code == 502 and res.json()["code"] == "asr_failed"
    small, token4, _ = _app(asr_max_bytes=1000)
    assert small.post(CLOUD_ASR_PATH, content=_wav(1), headers={"Authorization": f"Bearer {token4}"}).status_code == 413


def test_the_daily_limit_per_store():
    now = [1_800_000_000.0]
    client, token, _ = _app(asr_daily_sec=3.0, wall_clock=lambda: now[0])
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post(CLOUD_ASR_PATH, content=_wav(2), headers=auth).status_code == 200
    res = client.post(CLOUD_ASR_PATH, content=_wav(2), headers=auth)
    assert res.status_code == 429 and res.json()["code"] == "asr_quota"
    now[0] += 86400                                  # 次の日（UTC）は数え直す
    assert client.post(CLOUD_ASR_PATH, content=_wav(2), headers=auth).status_code == 200


def test_wav_seconds():
    assert wav_seconds(_wav(0.5)) == pytest.approx(0.5)
    assert wav_seconds(b"RIFF....") is None


def test_the_store_pc_sends_the_utterance_through_the_cloud():
    """店舗 PC の `CloudTranscriber` → クラウドの受け口 → （偽の）OpenAI。端末トークンで送り、鍵は送らない。"""
    client, token, fake = _app()

    def post_via_client(url, body, headers, timeout=None):
        assert url == "https://cloud.example" + CLOUD_ASR_PATH and "Bearer " + token == headers["Authorization"]
        res = client.post(CLOUD_ASR_PATH, content=body, headers=headers)
        if res.status_code != 200:
            raise RemoteAsrError(f"HTTP {res.status_code}")
        return res.json()

    tr = CloudTranscriber("https://cloud.example/", token, vad_threshold=0.0, post_fn=post_via_client)
    assert tr.ready and tr.remote
    result = tr.recognize(b"\x01\x00" * 16000)
    assert result.text == SPOKEN and result.confidence is None
    assert tr.asr_name == "openai-whisper-1" and fake.calls[0][1] == SECRET
    assert not CloudTranscriber("https://cloud.example", "").ready      # つないでいない


def test_a_failure_is_left_for_resending():
    client, token, _ = _app(fake=FakeOpenAI(fail=True))

    def post_via_client(url, body, headers, timeout=None):
        res = client.post(CLOUD_ASR_PATH, content=body, headers=headers)
        if res.status_code != 200:
            raise RemoteAsrError(f"HTTP {res.status_code}: {res.json()['code']}")
        return res.json()

    tr = CloudTranscriber("https://cloud.example", token, vad_threshold=0.0, post_fn=post_via_client)
    with pytest.raises(RemoteAsrError, match="asr_failed"):
        tr.recognize(b"\x01\x00" * 16000)

# -*- coding: utf-8 -*-
"""
文字起こしの速度を条件ごとに測る（E-2 の待ち時間を詰めるため）。

**.venv-voice の Python で動かすこと。** app.py 側の Python には
faster-whisper が入っていない。

    .venv-voice/Scripts/python.exe Test/bench_stt.py
    .venv-voice/Scripts/python.exe Test/bench_stt.py Test/tts_long.wav

モデルの読み込みは条件ごとに1回だけ行い、**読み込み時間と推論時間を分けて**
出す。常駐（stt.py --serve）では読み込みは起動時の1回きりなので、現場の
待ち時間に効くのは推論の方。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "voice"))

from faster_whisper import WhisperModel
import stt   # 前処理とプロンプトを本番と揃えるため、stt.py のものを使う

WAV = sys.argv[1] if len(sys.argv) > 1 else "Test/tts_sample.wav"

# (ラベル, モデル名, beam_size, cpu_threads)  cpu_threads=0 は ctranslate2 の既定
# Windows では huggingface_hub が symlink を張れず落ちることがある（WinError 1314）。
# 開発者モードか管理者権限が要るので、コピーで持つように切り替える
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

CASES = [
    ("large-v3  beam5 (現行)", "large-v3",       5, 0),
    ("large-v3  beam1",        "large-v3",       1, 0),
    ("large-v3  beam5 6スレッド", "large-v3",     5, 6),
    ("turbo     beam5",        "large-v3-turbo", 5, 0),
    ("turbo     beam1",        "large-v3-turbo", 1, 0),
    ("medium    beam5",        "medium",         5, 0),
]

prepared = stt._prepare(WAV)
audio = prepared["samples"]
print(f"音声: {WAV}  {prepared['audio_sec']}秒  "
      f"ピーク {prepared['peak_dbfs']}dBFS\n")

print(f"{'条件':<26} {'読込':>7} {'推論':>7}  書き起こし")
print("-" * 90)
for label, name, beam, threads in CASES:
    t0 = time.time()
    kw = {"cpu_threads": threads} if threads else {}
    m = WhisperModel(name, device="cpu", compute_type="int8",
                     download_root=stt.CACHE_DIR, **kw)
    load = time.time() - t0

    t0 = time.time()
    segments, _ = m.transcribe(audio, language=stt.LANGUAGE,
                               initial_prompt=stt.PROMPT, beam_size=beam,
                               vad_filter=True, condition_on_previous_text=False)
    text = "".join(s.text.strip() for s in segments)   # ここで実処理が走る
    infer = time.time() - t0

    print(f"{label:<26} {load:6.1f}秒 {infer:6.1f}秒  {text[:40]}")
    del m

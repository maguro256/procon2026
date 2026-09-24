# -*- coding: utf-8 -*-
"""
Whisper の「30秒窓」が実際に効いているかを測る。

短い音声でも30秒ぶんの計算が走っているなら、音声を切り詰めても時間は変わらない。
変わるなら窓は可変で、短く喋れば速くなるということになる。

    .venv-voice/Scripts/python.exe Test/bench_window.py
"""
import os
import sys
import time
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "voice"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from faster_whisper import WhisperModel
import stt

SR = 16000

sig = inspect.signature(WhisperModel.transcribe).parameters
print("transcribe() が受け取る窓まわりの引数:")
for name in ("chunk_length", "clip_timestamps", "without_timestamps"):
    print(f"  {name:<18} {'あり' if name in sig else 'なし'}")
print()

m = WhisperModel("large-v3", device="cpu", compute_type="int8",
                 download_root=stt.CACHE_DIR, cpu_threads=12)
full = stt._prepare("Test/tts_long.wav")["samples"]   # 35秒
print(f"素材 {len(full) / SR:.1f}秒  （先頭から切り出して長さを変える）\n")
print(f"{'音声長':>8} {'推論':>8}  書き起こし")
print("-" * 70)

for sec in (1, 3, 5, 10, 20, 29):
    audio = full[: int(sec * SR)]
    t0 = time.time()
    segments, _ = m.transcribe(audio, language=stt.LANGUAGE,
                               initial_prompt=stt.PROMPT, beam_size=5,
                               vad_filter=True, condition_on_previous_text=False)
    text = "".join(s.text.strip() for s in segments)
    print(f"{sec:>6}秒 {time.time() - t0:>7.1f}秒  {text[:38]}")

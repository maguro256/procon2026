# -*- coding: utf-8 -*-
"""
30秒窓を縮められるか。transcribe(chunk_length=...) を振って、短い音声の
「下駄」（エンコーダぶんの固定費）が下がるかを見る。

    .venv-voice/Scripts/python.exe Test/bench_chunk.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "voice"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from faster_whisper import WhisperModel
import stt

SR = 16000
m = WhisperModel("large-v3", device="cpu", compute_type="int8",
                 download_root=stt.CACHE_DIR, cpu_threads=12)
audio = stt._prepare("Test/tts_sample.wav")["samples"]      # 4.7秒
print(f"音声 {len(audio) / SR:.1f}秒\n")
print(f"{'chunk_length':>13} {'推論':>8}  書き起こし")
print("-" * 70)

for chunk in (None, 20, 10, 5):
    kw = {} if chunk is None else {"chunk_length": chunk}
    try:
        t0 = time.time()
        segments, _ = m.transcribe(audio, language=stt.LANGUAGE,
                                   initial_prompt=stt.PROMPT, beam_size=5,
                                   vad_filter=True,
                                   condition_on_previous_text=False, **kw)
        text = "".join(s.text.strip() for s in segments)
        label = "既定(30)" if chunk is None else str(chunk)
        print(f"{label:>13} {time.time() - t0:>7.1f}秒  {text[:38]}")
    except Exception as e:
        print(f"{chunk!s:>13}      ---  {type(e).__name__}: {str(e)[:60]}")

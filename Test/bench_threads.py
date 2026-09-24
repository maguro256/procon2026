# -*- coding: utf-8 -*-
"""turbo でスレッド数だけを振って測る。bench_stt.py の続き"""
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

p = stt._prepare(sys.argv[1] if len(sys.argv) > 1 else "Test/tts_sample.wav")
for threads in (0, 6, 12):
    t0 = time.time()
    kw = {"cpu_threads": threads} if threads else {}
    m = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8",
                     download_root=stt.CACHE_DIR, **kw)
    load = time.time() - t0
    t0 = time.time()
    segments, _ = m.transcribe(p["samples"], language=stt.LANGUAGE,
                               initial_prompt=stt.PROMPT, beam_size=5,
                               vad_filter=True, condition_on_previous_text=False)
    text = "".join(s.text.strip() for s in segments)
    print("turbo threads=%-4s 読込 %5.1f秒  推論 %5.1f秒  %s"
          % (threads or "既定", load, time.time() - t0, text[:34]))
    del m

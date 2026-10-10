"""
voice/stt.py - 音声ファイル → テキスト（Whisper 文字起こし）

TODO.md の E-2 の前半。処理の分担はこうなっている:

    ラズパイ: ボタンを押している間だけ録音 → 音声をPCへ送信
    PC      : ★ここ（Whisper で文字起こし）→ Gemma 3 で意図分析 → POST /api/tasks

**このファイルは app.py とは別の Python で動く。** app.py は Python 3.8 固定だが
faster-whisper は 3.9 以降しか入らないため、`.venv-voice`（Python 3.11）に分けた。
後段が `POST /api/tasks` を叩くだけの繋がりなので、同じプロセスに同居させる必要はない。

    .venv-voice/Scripts/python voice/stt.py rec.wav
    .venv-voice/Scripts/python voice/stt.py rec.wav --json --model medium
    .venv-voice/Scripts/python voice/stt.py --http 8765     # 別のPC（GPU付き）で常駐させる

openai-whisper ではなく faster-whisper を使う。速度が約4倍でVRAMも半分、そして
**ffmpeg の外部インストールが要らない**（同梱の PyAV が読む）。16kHz の WAV なら
ここで標準ライブラリだけで開いて numpy 配列で渡すので、PyAV すら通らない。

I2Sマイク(INMP441)の癖に合わせた前処理を入れてある。TODO.md の E-2 参照:
  - 録音開始直後の1〜2秒は DC がドリフトする（3Hz以下）→ 80Hz のハイパスで落とす
  - 声のピークが約 -26dBFS と小さい                     → ピークを見てゲインを掛ける
  - L/R を GND に落としてあるので右チャンネルは常に0    → 鳴っている側を選ぶ
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000  # Whisper の入力は 16kHz 固定

# 既定値。環境変数で上書きできる（app.py / raspi.py と同じ GEMMBA_ 接頭辞に揃えた）
MODEL_NAME = os.environ.get("GEMMBA_STT_MODEL", "large-v3")
DEVICE = os.environ.get("GEMMBA_STT_DEVICE", "auto")      # auto / cuda / cpu
COMPUTE = os.environ.get("GEMMBA_STT_COMPUTE", "auto")    # auto / float16 / int8 ...
LANGUAGE = os.environ.get("GEMMBA_STT_LANG", "ja")
CACHE_DIR = os.environ.get("GEMMBA_STT_CACHE") or None

# 語彙のヒント。Whisper は直前の文脈として読むので、工場で出る語を並べておく。
# **これは飾りではなく必須。** 実機録音で外して比べたら、同じ音声が
# 「旋盤の接吻清掃支給を…チグBの新出し…フレが出ています」まで崩れ、句読点も
# 付かなくなった。入れた状態では3文とも完全一致だった（TODO.md の E-2 参照）。
# 文体もここに引きずられるので、指示文らしい書き方にしてある。
DEFAULT_PROMPT = (
    "工場の作業指示です。旋盤、プレス機、レーザー加工機、フォークリフト、"
    "治具の芯出し、面取り、バリ取り、切粉の清掃、外観検査、完成検査、"
    "ロット、公差、至急、定期メンテナンスをお願いします。"
    "振れが出ているので芯出しをやり直します。"
)
PROMPT = os.environ.get("GEMMBA_STT_PROMPT", DEFAULT_PROMPT)

HIGHPASS_HZ = 80.0    # これ以下を落とす。起動直後のDCドリフトが3Hz以下に出る
TARGET_PEAK = 0.5     # ゲイン後のピーク（-6dBFS）
MIN_PEAK = 0.003      # -50dBFS。これ以下は無音とみなして持ち上げない（雑音が育つだけ）
MAX_GAIN = 32.0       # +30dB まで


# ---------------------------------------------------------------- モデル

_lock = threading.Lock()
_model = None
_model_key = None
_dll_dirs = []   # os.add_dll_directory() のハンドル。捨てると登録が消える（下記）


def _add_cuda_dll_dirs() -> None:
    """pip で入れた CUDA ランタイム(nvidia-*-cu12)を ctranslate2 から見えるようにする

    Windows では site-packages/nvidia/<lib>/bin の DLL が既定の探索パスに入らない。
    足しておかないと「Library cublas64_12.dll is not found」で落ちる。Linux では不要。

    **PATH に足すのが本命で、add_dll_directory だけでは効かない。** ctranslate2 は
    素の LoadLibrary で読むので、AddDllDirectory 側の登録を見てくれない
    （SetDefaultDllDirectories を呼んだプロセスでないと参照されない）。実際、
    add_dll_directory だけだとモデルの読み込みは通るのに最初の encode() で
    cublas が見つからずに落ちる、という紛らわしい失敗になった。

    add_dll_directory も併用しておく（ハンドルは GC されると登録が外れるので保持する）。
    """
    if not hasattr(os, "add_dll_directory") or _dll_dirs:
        return
    try:
        import nvidia
    except ImportError:
        return  # CPUで動かす構成。何もしなくてよい
    found = []
    for root in nvidia.__path__:
        for dll_dir in sorted(Path(root).glob("*/bin")):
            found.append(str(dll_dir))
            try:
                _dll_dirs.append(os.add_dll_directory(str(dll_dir)))
            except OSError:
                pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")


def _resolve(device: str, compute_type: str):
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute_type == "auto":
        # float16 はGPU専用。CPUは int8 が実用的な唯一の選択肢（float32は数倍遅い）
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


def load_model(model: str = None, device: str = None, compute_type: str = None):
    """WhisperModel を読む。同じ設定なら使い回す

    large-v3 の読み込みは数秒かかるので、1音声ごとに作り直してはいけない。
    """
    global _model, _model_key
    name = model or MODEL_NAME
    device, compute_type = _resolve(device or DEVICE, compute_type or COMPUTE)
    key = (name, device, compute_type)
    with _lock:
        if _model is not None and _model_key == key:
            return _model
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel  # import自体が重いので遅延させる
        try:
            _model = WhisperModel(name, device=device, compute_type=compute_type,
                                  download_root=CACHE_DIR)
        except Exception as exc:
            if device != "cuda":
                raise
            # cuDNN のDLLが無い・VRAMが足りない等。動かないよりCPUで遅い方がまし
            print(f"[stt] GPUで開けませんでした。CPUに落とします: {exc}", file=sys.stderr)
            device, compute_type = "cpu", "int8"
            key = (name, device, compute_type)
            _model = WhisperModel(name, device=device, compute_type=compute_type,
                                  download_root=CACHE_DIR)
        _model_key = key
        return _model


def model_info() -> dict:
    name, device, compute_type = _model_key or (MODEL_NAME, "?", "?")
    return {"model": name, "device": device, "compute_type": compute_type}


# ---------------------------------------------------------------- 音声の前処理

def _decode_wav(path):
    """標準ライブラリだけで WAV を float32 モノラル(16kHz)に開く

    16kHz でない / WAV でない場合は None を返す。呼び出し側はファイルパスを
    そのまま faster-whisper に渡す（同梱の PyAV が正しくリサンプルする）。
    numpy だけで手抜きリサンプルするとエイリアスが乗って精度が落ちるので、
    ここでは「16kHzならそのまま使う、それ以外は任せる」と割り切る。
    """
    try:
        with wave.open(str(path), "rb") as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError):
        return None
    if sr != SAMPLE_RATE or ch < 1:
        return None

    if width == 1:                                   # 8bit だけ符号なし
        x = (np.frombuffer(raw, "<u1").astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 3:                                 # 24bit は numpy に型が無い
        b = np.frombuffer(raw, "u1")
        b = b[: len(b) // 3 * 3].reshape(-1, 3)
        pad = np.zeros((len(b), 4), "u1")
        pad[:, 1:] = b                               # 上位3バイトに置くと符号が付く
        x = pad.view("<i4").ravel().astype(np.float32) / 2147483648.0
    elif width == 4:                                 # arecord -f S32_LE がこれ
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        return None

    channel = "mono"
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch)
        peaks = np.abs(x).max(axis=0)
        # INMP441 は L/R を GND に落として左だけに出しているので右は常に0。
        # 平均すると振幅が半分になるだけなので、鳴っている側を選ぶ。
        if peaks.max() > 0 and peaks.min() < peaks.max() * 0.05:
            i = int(peaks.argmax())
            x, channel = x[:, i], f"ch{i}"
        else:
            x, channel = x.mean(axis=1), "mix"
    return np.ascontiguousarray(x, dtype=np.float32), channel


def _highpass(x, sr, fc=HIGHPASS_HZ):
    """移動平均を引くだけの簡易ハイパス。fc 以下（＝起動直後のDCドリフト）を落とす

    scipy を足したくないので IIR は使わない。cumsum なので長さに比例した時間で済む。
    """
    n = max(3, int(sr / fc)) | 1            # 奇数にして中心を取る
    if len(x) <= n:
        return (x - x.mean()).astype(np.float32)
    half = n // 2
    pad = np.concatenate([np.full(half, x[0], np.float32), x, np.full(half, x[-1], np.float32)])
    c = np.cumsum(np.concatenate([[0.0], pad]), dtype=np.float64)
    ma = (c[n:] - c[:-n]) / n
    return (x - ma).astype(np.float32)


def _dbfs(peak: float) -> float:
    return -99.0 if peak <= 0 else round(20.0 * float(np.log10(peak)), 1)


def _prepare(path):
    """WAVを開いて前処理する。開けなければ None（呼び出し側がパスを直接渡す）"""
    decoded = _decode_wav(path)
    if decoded is None:
        return None
    x, channel = decoded
    raw_peak = float(np.abs(x).max()) if len(x) else 0.0
    x = _highpass(x, SAMPLE_RATE)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    gain = 1.0
    if peak >= MIN_PEAK:
        gain = min(TARGET_PEAK / peak, MAX_GAIN)
        if gain > 1.0:
            x = (x * gain).astype(np.float32)
        else:
            gain = 1.0
    return {
        "samples": x,
        "channel": channel,
        "audio_sec": round(len(x) / SAMPLE_RATE, 2),
        "peak_dbfs": _dbfs(raw_peak),
        "gain": round(gain, 1),
    }


# ---------------------------------------------------------------- 文字起こし

def transcribe(path, *, model=None, device=None, compute_type=None, language=None,
               prompt=None, vad=True, beam_size=5, raw=False) -> dict:
    """音声ファイルを文字起こしして dict で返す

    raw=True で前処理（ハイパス・ゲイン）を飛ばす。前処理の効きを比べるとき用。
    """
    m = load_model(model, device, compute_type)
    prepared = None if raw else _prepare(path)
    audio = prepared["samples"] if prepared else str(path)

    t0 = time.time()
    segments, info = m.transcribe(
        audio,
        language=language or LANGUAGE,
        initial_prompt=prompt if prompt is not None else PROMPT,
        beam_size=beam_size,
        # 無音を切る。押しボタン録音は前後に無音が付くうえ、Whisper は無音に対して
        # 幻聴（「ご視聴ありがとうございました」等）を出しやすい
        vad_filter=vad,
        # 1発話ごとに独立した内容なので、直前の文脈を引き継がせない。
        # 引き継がせると同じ語を延々繰り返すループに入ることがある
        condition_on_previous_text=False,
    )
    # transcribe() はジェネレータを返すので、ここで消費し切るまでが実処理時間
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments]
    elapsed = time.time() - t0

    result = {
        "text": "".join(s["text"] for s in segs),      # 日本語なので空白で繋がない
        "segments": segs,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "audio_sec": round(info.duration, 2),
        "elapsed_sec": round(elapsed, 2),
        "source": str(path),
    }
    result.update(model_info())
    if prepared:
        result.update({k: prepared[k] for k in ("channel", "peak_dbfs", "gain")})
        result["audio_sec"] = prepared["audio_sec"]   # VAD後ではなく実長を返す
    return result


# ---------------------------------------------------------------- CLI

def _report(r: dict) -> str:
    head = (f"{r['source']}  {r['audio_sec']}秒"
            + (f" / 音量 {r['peak_dbfs']}dBFS → ゲイン x{r['gain']} / {r['channel']}"
               if "peak_dbfs" in r else " / 前処理なし"))
    speed = r["audio_sec"] / r["elapsed_sec"] if r["elapsed_sec"] else 0
    info = (f"  {r['model']} / {r['device']} {r['compute_type']} / "
            f"{r['elapsed_sec']}秒 ({speed:.1f}倍速) / "
            f"言語 {r['language']} {r['language_probability']}")
    body = r["text"] or "(認識できませんでした)"
    return f"{head}\n{info}\n\n  {body}\n"


def _emit(obj) -> None:
    """常駐モードの返事。**1依頼につき必ず1行**。改行を挟むと相手が読めなくなる"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _serve(args) -> int:
    """
    常駐モード。標準入力から音声ファイルのパスを1行ずつ受け取り、結果のJSONを
    1行で返す。**モデルの読み込みは最初の1回だけ**なので、2回目以降の呼び出しから
    30秒台の固定費が消える（CPUのlarge-v3で実測）。

    話し相手は app.py の _transcribe()。どんな失敗でも必ず1行返すこと。
    返さないと向こうが VOICE_TIMEOUT まで待ち続ける。
    """
    load_model(args.model, args.device, args.compute)
    info = model_info()
    print(f"[stt] 常駐を開始しました {info['model']} / {info['device']} "
          f"{info['compute_type']}", file=sys.stderr)
    # 準備ができたことを最初の1行で知らせる。向こうはこれを読み捨ててから依頼する
    _emit(dict(info, ready=True))

    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        if path == "quit":
            break
        try:
            if not Path(path).exists():
                _emit({"error": f"ファイルがありません: {path}"})
                continue
            _emit(transcribe(path, model=args.model, device=args.device,
                             compute_type=args.compute, language=args.language,
                             prompt=args.prompt, vad=not args.no_vad,
                             beam_size=args.beam_size, raw=args.raw))
        except Exception as e:   # 1件の失敗で常駐を落とさない
            _emit({"error": f"{type(e).__name__}: {e}"})
    print("[stt] 常駐を終了します", file=sys.stderr)
    return 0


def _serve_http(args) -> int:
    """
    HTTPで常駐する。GPUの無いPCで app.py を動かすとき、GPU付きの別のPC
    （Tailscale で繋いだ家のデスクトップなど）で文字起こしだけを引き受ける。

        POST /transcribe   body は録音そのもの（WAV）。結果は transcribe() と同じJSON
        GET  /health       モデル名・デバイス。app.py の起動時の確認用

    認証は無い。**Tailscale などの閉じた網の内側にだけ公開すること**（--http に
    Tailscale のIPを付けると、そのアドレスでだけ待ち受ける）。
    1件ずつ順に処理する（GPUで並行させても速くならない）。
    """
    import tempfile
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from uuid import uuid4

    host, _, port = args.http.rpartition(":")
    load_model(args.model, args.device, args.compute)
    info = model_info()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, dict(info, ready=True))
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/transcribe":
                self._reply(404, {"error": "not found"})
                return
            audio = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            tmp = Path(tempfile.gettempdir()) / f"gemmba_stt_{uuid4().hex[:8]}.wav"
            try:
                tmp.write_bytes(audio)
                r = transcribe(tmp, model=args.model, device=args.device,
                               compute_type=args.compute, language=args.language,
                               prompt=args.prompt, vad=not args.no_vad,
                               beam_size=args.beam_size, raw=args.raw)
                print(f"[stt] {r['audio_sec']}秒 → {r['elapsed_sec']}秒: {r['text']}", file=sys.stderr)
                self._reply(200, r)
            except Exception as e:   # 1件の失敗で常駐を落とさない
                self._reply(500, {"error": f"{type(e).__name__}: {e}"})
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass

        def log_message(self, fmt, *a):   # 1依頼ごとのアクセスログは上の1行で足りる
            pass

    server = HTTPServer((host or "0.0.0.0", int(port)), Handler)
    print(f"[stt] HTTPで常駐を開始しました {info['model']} / {info['device']} "
          f"{info['compute_type']} → http://{host or '0.0.0.0'}:{port}/transcribe", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("[stt] 常駐を終了します", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Whisper で音声を文字起こしする")
    p.add_argument("files", nargs="*", help="WAV等の音声ファイル")
    p.add_argument("--model", default=None, help=f"既定 {MODEL_NAME}")
    p.add_argument("--device", default=None, choices=["auto", "cuda", "cpu"])
    p.add_argument("--compute", default=None, help="float16 / int8 など")
    p.add_argument("--language", default=None, help=f"既定 {LANGUAGE}")
    p.add_argument("--prompt", default=None, help="語彙ヒント。'' で無効化")
    p.add_argument("--no-vad", action="store_true", help="無音カットを切る")
    p.add_argument("--raw", action="store_true", help="ハイパス・ゲインを掛けない")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--json", action="store_true", help="結果をJSONで出す")
    p.add_argument("--warmup", action="store_true",
                   help="モデルを用意する（無ければダウンロード）だけで終わる")
    p.add_argument("--serve", action="store_true",
                   help="常駐する。標準入力にパスを1行、結果のJSONが1行返る。"
                        "モデルを読み直さないので app.py はこちらを使う")
    p.add_argument("--http", metavar="[HOST:]PORT",
                   help="HTTPで常駐する（別のPCで文字起こしを引き受ける）。"
                        "app.py 側は GEMMBA_STT_URL=http://<このPC>:<PORT> で使う")
    args = p.parse_args(argv)

    # リダイレクト先では UTF-8 で出す。Windows の Python は既定で cp932 になるので、
    # `--json > out.json` の結果を後段（Gemma側）が読めなくなる。端末に出すときは
    # コンソールのコードページに任せる（ここで UTF-8 にすると逆に文字化けする）。
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")

    if args.serve:
        # 常駐では入力も相手（app.py）が UTF-8 で書いてくる。日本語を含むパスが
        # 来ても壊れないよう、こちらも合わせる
        sys.stdin.reconfigure(encoding="utf-8")
        return _serve(args)

    if args.http:
        return _serve_http(args)

    if args.warmup:
        # 読み込むだけ。モデルが手元に無ければ faster-whisper がここで落としてくる
        t0 = time.time()
        load_model(args.model, args.device, args.compute)
        info = model_info()
        print(f"[stt] 準備完了 {info['model']} / {info['device']} {info['compute_type']}"
              f" ({time.time() - t0:.1f}秒)", file=sys.stderr)
        return 0

    if not args.files:
        p.error("音声ファイルを指定してください（--warmup のときは不要）")
    missing = [f for f in args.files if not Path(f).exists()]
    if missing:
        print(f"ファイルがありません: {', '.join(missing)}", file=sys.stderr)
        return 1

    t0 = time.time()
    load_model(args.model, args.device, args.compute)
    if not args.json:
        info = model_info()
        print(f"[stt] {info['model']} / {info['device']} {info['compute_type']}"
              f" を読み込みました ({time.time() - t0:.1f}秒)", file=sys.stderr)

    results = []
    for f in args.files:
        r = transcribe(f, model=args.model, device=args.device, compute_type=args.compute,
                       language=args.language, prompt=args.prompt,
                       vad=not args.no_vad, beam_size=args.beam_size, raw=args.raw)
        results.append(r)
        if not args.json:
            print(_report(r))
    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0],
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

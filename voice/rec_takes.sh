#!/bin/sh
# rec_takes.sh - stt.py の実機確認用。台本を読み上げて1本録る
#
#   ラズパイに置いて実行する:
#       scp voice/rec_takes.sh pi01:~/
#       ssh pi01 "sh rec_takes.sh [秒数]"
#
#   ~/take.wav ができるので、PC側で文字起こしする:
#       scp pi01:~/take.wav .
#       .venv-voice/Scripts/python voice/stt.py take.wav
#
# **1文ずつ短く区切って録らないこと。** ssh 越しだとカウントダウンが手元に
# リアルタイムで届かないので、「録音開始」に声を合わせられない。実際に7秒×3本で
# 試したら、1本目は話し始める前に録音が終わり、3本目は録音開始時点で既に話して
# いて頭が欠けた。長めに1本録って前後の無音は VAD に切らせる方が確実。
#
# 録音形式は rec.sh と同じ S32_LE / ステレオ / 16kHz のまま（左だけに声が出る）。
# stt.py がこの形をそのまま読めるので、mic_check.py を通す必要はない。
# カード番号は再起動で変わりうるので名前で指定する。

D=${1:-25}
DEV=plughw:CARD=sndrpigooglevoi,DEV=0
OUT=$HOME/take.wav

# 工場の指示文。stt.py の DEFAULT_PROMPT に入れた語彙を一通り踏むようにしてある。
cat <<'EOS'

これから録音します。次の3文を、間に1〜2秒あけて読み上げてください。

  1. 旋盤の切粉清掃を至急お願いします
  2. 製品Cの外形加工を20個、明日までにお願いします
  3. 治具Bの芯出しをやり直してください。振れが出ています

前後に無音があっても構いません（文字起こし側で切ります）。
EOS

printf '録音中（%s秒）... どうぞ\n' "$D"
arecord -D "$DEV" -c 2 -r 16000 -f S32_LE -d "$D" -q "$OUT"
printf '録音終了 -> %s\n' "$OUT"

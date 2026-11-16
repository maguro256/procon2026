# Windows 標準の日本語音声（Haruka/Ayumi/Sayaka/Ichiro）で prompts.csv を読み上げ、WAV にする。
#   powershell -ExecutionPolicy Bypass -File synth_windows.ps1
# 出力: audio/tts_clean/<声>_<速さ>_<文ID>.wav
# VOICEVOX を入れたら、同じ名前の付け方で別スクリプトに差し替えればよい。
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $here "audio\tts_clean"
New-Item -ItemType Directory -Force $out | Out-Null

Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1" } | Select-Object -First 1
function Await($op, [type]$t) {
  $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }

$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$voices = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object Language -eq "ja-JP"
$rates = @{ "r090" = 0.9; "r100" = 1.0; "r115" = 1.15 }
$rows = Import-Csv (Join-Path $here "prompts.csv") -Encoding UTF8

foreach ($v in $voices) {
  $synth.Voice = $v
  $name = ($v.DisplayName -split " ")[1].ToLower()
  foreach ($rk in $rates.Keys) {
    $synth.Options.SpeakingRate = $rates[$rk]
    foreach ($r in $rows) {
      $path = Join-Path $out ("{0}_{1}_{2}.wav" -f $name, $rk, $r.id)
      if (Test-Path $path) { continue }
      $stream = Await ($synth.SynthesizeTextToStreamAsync($r.text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
      $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
      $n = [uint32]$stream.Size
      Await ($reader.LoadAsync($n)) ([uint32]) | Out-Null
      $buf = New-Object byte[] $n; $reader.ReadBytes($buf)
      [IO.File]::WriteAllBytes($path, $buf)
    }
    Write-Host $name $rk "done"
  }
}

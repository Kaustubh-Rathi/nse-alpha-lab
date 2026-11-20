# Generate one WAV per scene with the Windows SAPI voice.
# Requires PowerShell 5+ and System.Speech. Writes audio/<id>.wav and durations.json.
param(
    [string]$Root = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$spec = Get-Content -Raw (Join-Path $Root 'narration.json') | ConvertFrom-Json
$outDir = Join-Path $Root 'audio'
New-Item -ItemType Directory -Force $outDir | Out-Null

$durations = [ordered]@{}
foreach ($scene in $spec.scenes) {
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoice($spec.voice)
    $synth.Rate = [int]$spec.rate
    $synth.Volume = 100
    $path = Join-Path $outDir ($scene.id + '.wav')
    $synth.SetOutputToWaveFile($path)
    $synth.Speak($scene.text)
    $synth.Dispose()
    $duration = [double](& ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $path)
    $durations[$scene.id] = [math]::Round($duration, 3)
    Write-Host ("{0}: {1:N2}s" -f $scene.id, $duration)
}
$durations | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $Root 'durations.json')
Write-Host ("wrote {0} files" -f $spec.scenes.Count)

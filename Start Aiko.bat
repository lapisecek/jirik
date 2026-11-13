@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Prvni spusteni: instaluji knihovny, chvili to potrva...
  py -3 -m venv .venv || goto :err
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e ".[pc]" piper-tts numpy || goto :err
)
if not exist "voices\cs_CZ-jirka-medium.onnx" (
  echo Stahuji hlas Jirka...
  if not exist voices mkdir voices
  curl -fL -o "voices\cs_CZ-jirka-medium.onnx" "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium.onnx"
  curl -fL -o "voices\cs_CZ-jirka-medium.onnx.json" "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium.onnx.json"
)
".venv\Scripts\python.exe" run.py
pause
exit /b
:err
echo Instalace se nepovedla.
pause

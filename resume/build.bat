@echo off
rem Build resume.tex -> resume.pdf with Tectonic (XeLaTeX-based).
rem Double-click this file, or press Ctrl+Shift+B in VS Code.
rem Packages are downloaded on the first run only; later runs work offline.
rem Pass "nopause" to skip the pause at the end (used by the VS Code task).
setlocal
cd /d "%~dp0"

set "TECTONIC=tectonic"
where tectonic >nul 2>nul || set "TECTONIC=%LOCALAPPDATA%\Programs\tectonic\tectonic.exe"
if not "%TECTONIC%"=="tectonic" if not exist "%TECTONIC%" (
  echo Tectonic not found: %TECTONIC%
  echo Download the x86_64-pc-windows-msvc zip from
  echo https://github.com/tectonic-typesetting/tectonic/releases
  echo and put tectonic.exe in the folder above.
  set "RC=1"
  goto :end
)

"%TECTONIC%" -X compile resume.tex
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo OK: resume.pdf updated.
) else (
  echo FAILED: see the "error: resume.tex:LINE" message above.
)

:end
if /i not "%~1"=="nopause" pause
exit /b %RC%

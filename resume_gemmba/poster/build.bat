@echo off
rem poster.html -> poster.pdf (A1) with headless Chrome. Double-click to rebuild.
setlocal
cd /d "%~dp0"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
"%CHROME%" --headless=new --disable-gpu --no-pdf-header-footer --virtual-time-budget=5000 --print-to-pdf="%~dp0poster.pdf" "file:///%~dp0poster.html"
if errorlevel 1 (echo FAILED) else (echo OK: poster.pdf updated.)
if /i not "%~1"=="nopause" pause

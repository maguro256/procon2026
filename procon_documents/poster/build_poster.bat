@echo off
rem Build the 1-page poster (poster.tex -> poster_a0.pdf) and the 2-page poster (poster2_p1.tex, poster2_p2.tex -> poster2_a0.pdf), A0 for printing.
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

rem 1-page version: poster.tex -> poster_a0.pdf
rem 2-page version: poster2_p1.tex + poster2_p2.tex -> poster2_a0.pdf
for %%F in (poster poster_a0 poster2_p1 poster2_p2 poster2_a0) do (
  "%TECTONIC%" -X compile %%F.tex || (set "RC=1" & goto :report)
)
set "RC=0"

:report
echo.
if "%RC%"=="0" (
  echo OK: poster_a0.pdf ^(1 page^) and poster2_a0.pdf ^(2 pages^) updated. Print these on A0.
) else (
  echo FAILED: see the "error: poster.tex:LINE" message above.
)

:end
if /i not "%~1"=="nopause" pause
exit /b %RC%

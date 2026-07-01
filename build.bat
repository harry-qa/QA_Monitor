@echo off
echo [1/2] Starting Nuitka build...

python -m nuitka ^
  --standalone ^
  --onefile ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=ezlab.ico ^
  --include-data-files=ezlab_logo.png=ezlab_logo.png ^
  --include-data-files=ezlab.ico=ezlab.ico ^
  --enable-plugin=tk-inter ^
  --assume-yes-for-downloads ^
--output-filename=ezLabQAMonitor.exe ^
  --output-dir=dist ^
  monitor.py

if errorlevel 1 (
  echo Build failed!
  pause
  exit /b 1
)

echo.
echo [2/2] Build complete: dist\ezLabQAMonitor.exe
echo To create the installer, compile installer.iss with Inno Setup.
pause

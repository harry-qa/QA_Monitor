@echo off
echo [1/3] Publishing DumpAnalyzer (ClrMD dump analysis helper)...
dotnet publish DumpAnalyzer\DumpAnalyzer.csproj -c Release >NUL
if errorlevel 1 (
  echo DumpAnalyzer publish failed!
  pause
  exit /b 1
)
copy /Y "DumpAnalyzer\bin\Release\net8.0\win-x64\publish\DumpAnalyzer.exe" "DumpAnalyzer.exe" >NUL

echo [2/3] Starting Nuitka build...

python -m nuitka ^
  --standalone ^
  --onefile ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=ezlab.ico ^
  --include-data-files=ezlab_logo.png=ezlab_logo.png ^
  --include-data-files=ezlab.ico=ezlab.ico ^
  --include-data-files=DumpAnalyzer.exe=DumpAnalyzer.exe ^
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
echo [3/3] Build complete: dist\ezLabQAMonitor.exe
echo To create the installer, compile installer.iss with Inno Setup.
pause

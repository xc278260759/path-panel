@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PY=C:\Users\SY-XC\AppData\Local\Programs\Python\Python37\python.exe"

"%PY%" -m PyInstaller --noconfirm --onefile --windowed --name 路径面板 ^
  --icon "%ROOT%路径面板.ico" ^
  --distpath "%ROOT%dist" ^
  --workpath "%ROOT%build_路径面板" ^
  --specpath "%ROOT%" ^
  --hidden-import pystray._win32 ^
  "%ROOT%路径面板.pyw"

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo BUILD OK: %ROOT%dist\路径面板.exe
endlocal

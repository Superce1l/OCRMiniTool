@echo off
rem OCRMiniTool one-click launcher (dev mode).
rem NOTE: keep this file ASCII-only -- cmd.exe parses batch files in the
rem legacy ANSI codepage (GBK on zh-CN Windows); UTF-8 text can corrupt lines.
rem
rem Venv resolution: prefers the in-project .venv when present, otherwise
rem %LOCALAPPDATA%\OCRMiniTool\venv (created on first run).
rem CAUTION: inside managed workspaces (e.g. D:\Project) new files inherit a
rem Low mandatory integrity label and .NET refuses to load such assemblies,
rem which breaks pywebview/pythonnet. If the native window fails right after
rem start, relabel the venv once (admin PowerShell):
rem   icacls "D:\Project\OCRMiniTool\.venv" /setintegritylevel M /t
rem (re-run after installing/upgrading packages) -- or delete .venv to use the
rem LOCALAPPDATA copy, which never carries the label.
setlocal
cd /d "%~dp0"

set "VENV=%LOCALAPPDATA%\OCRMiniTool\venv"
if "%VENV%"=="\OCRMiniTool\venv" set "VENV=%USERPROFILE%\AppData\Local\OCRMiniTool\venv"

rem Prefer an in-project .venv when one exists (create it yourself with:
rem   uv venv --python 3.12 .venv
rem   uv pip install --python .venv\Scripts\python.exe -r requirements.txt )
if exist ".venv\Scripts\python.exe" set "VENV=%~dp0.venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo [OCRMiniTool] Creating virtual environment at %VENV% ...
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv venv --python 3.12 "%VENV%" || goto :fail
  ) else (
    echo [OCRMiniTool] uv not found, falling back to python -m venv ...
    py -3.12 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || goto :fail
  )
  echo [OCRMiniTool] Installing dependencies, first run may take a few minutes ...
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv pip install --python "%VENV%\Scripts\python.exe" -r requirements.txt || goto :fail
  ) else (
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
    "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  )
)

echo [OCRMiniTool] Starting ...
"%VENV%\Scripts\python.exe" app_webview.py %*
echo [OCRMiniTool] Exited.
pause
exit /b 0

:fail
echo [OCRMiniTool] Setup failed. Please check the messages above.
pause
exit /b 1

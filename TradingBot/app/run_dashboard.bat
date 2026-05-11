@echo off
REM Pre-merge (through 2026-05-08) this launched the TradingBot Flask dashboard
REM on port 5000. The unified dashboard now lives in the sibling project
REM Trade_Perf (FastAPI on :8000 + Vite/React on :5173) and serves all
REM three pages: Home, Trade Performance, Signal Analysis. This batch file
REM forwards to the new launcher.

title FYF Dashboard (unified)
echo Launching unified dashboard via Trade_Perf...
pwsh -ExecutionPolicy Bypass -File "%~dp0..\..\Trade_Perf\dashboard\run_dev.ps1"

@echo off
rem ===========================================================================
rem  DSH voice full stack - one-click launcher (double-click this file)
rem  Forwards all arguments to start-full-stack.ps1.
rem    -NoDsh      voice stack only (OmniVoice + bridge)
rem    -Rc8        use the old rc.8 harness (profile web) instead of v013
rem    -NoBrowser  do not open the browser
rem    -WithQQ     also start NapCat (QQ bridge)
rem  Docker Desktop must already be running (the script only checks it).
rem ===========================================================================
setlocal
title DSH Voice Full Stack
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-full-stack.ps1" %*
echo.
pause

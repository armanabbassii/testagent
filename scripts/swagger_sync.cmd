@echo off
rem Manual run of the daily Swagger synchronisation (the scheduled task calls pythonw directly).
rem   scripts\swagger_sync.cmd              real run (retries until every source succeeds)
rem   scripts\swagger_sync.cmd --if-due     real run only if no success since the last daily time
rem   scripts\swagger_sync.cmd --check      real GET and validation, no snapshot is changed
rem   scripts\swagger_sync.cmd --dry-run    no network and no writes
setlocal
cd /d "%~dp0.."
"%~dp0..\.venv\Scripts\python.exe" -m src.swagger_sync run %*
exit /b %ERRORLEVEL%

@echo off
REM Drag a folder onto this file to pack it, or a .gma to extract it.
setlocal
if "%~1"=="" (
  call :python "%~dp0gmapack.py" --help
) else (
  call :python "%~dp0gmapack.py" %*
)
set "gmapack_exit=%errorlevel%"
echo.
pause
exit /b %gmapack_exit%

:python
where python >nul 2>nul
if errorlevel 1 goto py_fallback
python %*
exit /b %errorlevel%

:py_fallback
py -3 %*
exit /b %errorlevel%

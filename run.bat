@echo off
rem Launch Location Sound File Manager from its project folder. Optional argument: the library folder.
rem A project venv (made by windows\build_app.ps1, or by hand) is used when there is one.
setlocal
set "here=%~dp0"
set "PYTHONPATH=%here%;%PYTHONPATH%"
if exist "%here%.venv\Scripts\pythonw.exe" (
    start "" "%here%.venv\Scripts\pythonw.exe" -m sound_file_manager %*
) else (
    start "" pythonw -m sound_file_manager %*
)

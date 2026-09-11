@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "REPO_SSH=git@github.com:Sxd55/astrbot_plugin_savagetype.git"
set "REPO_WEB=https://github.com/Sxd55/astrbot_plugin_savagetype"
set "KEY=%USERPROFILE%\.ssh\id_ed25519"

echo.
echo === Savage Type upload ===
echo Working dir: %CD%
echo Target: %REPO_WEB%
echo.

where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] git not found. Install Git for Windows first.
  goto :end
)

if not exist ".git" (
  echo Initializing git repo...
  git init -b main
)

git config user.name >nul 2>&1
if errorlevel 1 git config user.name "Sxd55"
git config user.email >nul 2>&1
if errorlevel 1 git config user.email "Sxd55@users.noreply.github.com"

echo Staging files...
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Update astrbot_plugin_savagetype"
) else (
  git rev-parse --verify HEAD >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Nothing to commit and no previous commit. Add files first.
    goto :end
  )
  echo No new changes to commit.
)

if not exist "%KEY%" (
  echo Generating SSH key: %KEY%
  if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
  ssh-keygen -t ed25519 -C "Sxd55@users.noreply.github.com" -N "" -f "%KEY%"
)

echo.
echo ----- PUBLIC KEY (copy this entire line) -----
type "%KEY%.pub"
echo ----- END PUBLIC KEY -----
echo.
echo 1. Open https://github.com/settings/keys
echo    Click New SSH key, paste the line above, save.
echo 2. Open https://github.com/new
echo    Name: astrbot_plugin_savagetype   Visibility: Public
echo    Do NOT add README / gitignore / license.
echo    Create repository if it does not exist.
echo.
pause

echo Testing GitHub SSH...
ssh -o StrictHostKeyChecking=accept-new -T git@github.com
echo.

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  git remote add origin %REPO_SSH%
) else (
  git remote set-url origin %REPO_SSH%
)

echo Pushing main...
git push -u origin main
if errorlevel 1 (
  echo.
  echo [ERROR] Push failed.
  echo If it says repository not found, create it at:
  echo   %REPO_WEB%
  echo If it says Permission denied, the SSH key is not added to GitHub yet.
  echo If it says Connection was reset, try again or use a proxy.
  goto :end
)

echo.
echo OK. Repo: %REPO_WEB%
echo.

:end
pause
endlocal

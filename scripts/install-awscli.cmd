@echo off
rem scripts/install-awscli.cmd - scripts/install-awscli.sh's Windows twin.
rem
rem Extracts the aws-cli v2 MSI matching the vendored aws-cli submodule into a
rem per-user directory (no admin rights, no registry entries, no PATH edits),
rem for the Windows e2e parity lane. scripts\minio-env.cmd prepends the stable
rem `current` junction to PATH, so the pinned aws.exe shadows any system
rem install for the suite - mirroring how .venv/bin/aws wins on Linux.
rem
rem Idempotent: a matching extraction is reused (no re-download).
rem
rem   scripts\install-awscli.cmd            # match vendor\aws-cli (full checkout)
rem   scripts\install-awscli.cmd 2.35.18    # a specific version (required on the
rem                                         # NTFS test copy, which carries no
rem                                         # vendor\ tree - testing.md section 8)
rem
rem Uses `msiexec /a` (an administrative extraction): it unpacks the MSI's
rem self-contained Amazon\AWSCLIV2 payload and installs nothing.
setlocal

set "root=%LOCALAPPDATA%\boto3-s3\aws-cli"
set "target=%~1"
if defined target goto have_target

rem Derive the version from the vendored submodule when present.
set "initpy=%~dp0..\vendor\aws-cli\awscli\__init__.py"
if not exist "%initpy%" (
    echo No version given and no vendor\aws-cli checkout found.
    echo Usage: scripts\install-awscli.cmd ^<version^>
    exit /b 1
)
for /f "tokens=2 delims='" %%v in ('findstr /c:"__version__" "%initpy%"') do set "target=%%v"
if not defined target (
    echo Could not parse __version__ from %initpy%
    exit /b 1
)

:have_target
set "dest=%root%\%target%"

if exist "%dest%\aws.exe" (
    echo aws-cli %target% already extracted at %dest%
    goto link
)

set "tmpdir=%TEMP%\awscli-extract-%target%-%RANDOM%"
set "msi=%tmpdir%\AWSCLIV2-%target%.msi"
mkdir "%tmpdir%" || exit /b 1
echo Downloading aws-cli %target% ...
curl.exe -fsSL "https://awscli.amazonaws.com/AWSCLIV2-%target%.msi" -o "%msi%" || goto fail
start /wait "" msiexec /a "%msi%" /qn TARGETDIR="%tmpdir%\extract"
if not exist "%tmpdir%\extract\Amazon\AWSCLIV2\aws.exe" (
    echo msiexec extraction failed - no aws.exe under %tmpdir%\extract
    goto fail
)
if not exist "%root%" mkdir "%root%"
move "%tmpdir%\extract\Amazon\AWSCLIV2" "%dest%" >nul || goto fail
rmdir /s /q "%tmpdir%"

:link
rem Stable pointer for PATH (minio-env.cmd): current -> <version>. A junction
rem needs neither admin rights nor Developer Mode (unlike a symlink).
if exist "%root%\current" rmdir "%root%\current"
mklink /j "%root%\current" "%dest%" >nul || exit /b 1
for /f "delims=" %%o in ('"%root%\current\aws.exe" --version') do echo Installed: %%o
exit /b 0

:fail
rmdir /s /q "%tmpdir%" 2>nul
exit /b 1

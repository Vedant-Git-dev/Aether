@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1

REM ===========================================================================
REM  Aether deploy engine (Windows 10/11) - the Render/UptimeRobot step behind
REM  the wizard.
REM
REM  The interactive setup is scripts\setup.bat, which runs the keyboard-driven
REM  wizard (src\aether\setup) and writes its answers to
REM  %USERPROFILE%\.aether-setup\answers.env and its secrets to
REM  %USERPROFILE%\.aether-setup\secrets.env. This engine takes those files and
REM  deploys non-interactively:
REM
REM    scripts\deploy.bat --answers %USERPROFILE%\.aether-setup\answers.env
REM
REM  Run it without --answers only for the legacy terminal prompts; the wizard
REM  is the supported way to answer the questions.
REM ===========================================================================

set "SCRIPT_DIR=%~dp0"
REM Capture the script path before the argument loop: SHIFT moves %0 along
REM with the arguments, so %~f0 later would name the last option, not this file.
set "SELF=%~f0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
set "TMPBASE=%TEMP%/aether-setup"
if not exist "%TMPBASE%" mkdir "%TMPBASE%" >nul 2>&1
set "HELPER=%TMPBASE%/helper.ps1"
REM A per-run name: an older build could leave run.env with a broken ACL,
REM and reusing the name would inherit that denial.
set "RUNENV=%TMPBASE%/run-%RANDOM%%RANDOM%.env"
set "STATE_DIR=%USERPROFILE%/.aether-setup"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
set "STATE_FILE=%STATE_DIR%/state.env"
set "SECRETS_FILE=%STATE_DIR%/secrets.env"

set "DRY_RUN=0"
set "NON_INTERACTIVE=0"
set "FORCE=0"
set "ANSWERS_FILE="
set "SECRETS_INPUT="
set "DATABASE_MODE="
set "DATABASE_URL="
set "LLM_PROVIDER="
set "LLM_MODEL="
set "SERVICES="
set "APPS="
set "CHANNELS="
set "PLAN="
set "SERVICE_NAME="
set "REGION="
set "DEPLOY_SOURCE="
set "REPO_URL="
set "UPTIME_MODE="
set "ALERT_EMAIL="
set "LOCAL_COPY="
set "WORKBENCH_PROVIDER="
set "IMAGE_PROVIDER="
if not defined AETHER_ENCRYPTION_KEY set "AETHER_ENCRYPTION_KEY="
if not defined AETHER_TOKEN set "AETHER_TOKEN="
if not defined TOKEN_ENC_KEY set "TOKEN_ENC_KEY="
if not defined TOKEN_ENC_KEY_ID set "TOKEN_ENC_KEY_ID="
if not defined RENDER_API_KEY set "RENDER_API_KEY="
if not defined TAVILY_KEY set "TAVILY_KEY="
if not defined GROQ_KEY set "GROQ_KEY="
if not defined GEMINI_KEY set "GEMINI_KEY="
if not defined ANTHROPIC_KEY set "ANTHROPIC_KEY="
if not defined OPENAI_KEY set "OPENAI_KEY="
if not defined NVIDIA_KEY set "NVIDIA_KEY="
if not defined TELEGRAM_TOKEN set "TELEGRAM_TOKEN="
if not defined DISCORD_TOKEN set "DISCORD_TOKEN="
if not defined GOOGLE_CLIENT_ID set "GOOGLE_CLIENT_ID="
if not defined GOOGLE_CLIENT_SECRET set "GOOGLE_CLIENT_SECRET="
if not defined GOOGLE_PICKER_KEY set "GOOGLE_PICKER_KEY="
if not defined GOOGLE_PICKER_APP_ID set "GOOGLE_PICKER_APP_ID="
if not defined GITHUB_CLIENT_ID set "GITHUB_CLIENT_ID="
if not defined GITHUB_CLIENT_SECRET set "GITHUB_CLIENT_SECRET="
if not defined LINKEDIN_CLIENT_ID set "LINKEDIN_CLIENT_ID="
if not defined LINKEDIN_CLIENT_SECRET set "LINKEDIN_CLIENT_SECRET="
if not defined MODAL_TOKEN_ID set "MODAL_TOKEN_ID="
if not defined MODAL_TOKEN_SECRET set "MODAL_TOKEN_SECRET="
if not defined VERCEL_TOKEN set "VERCEL_TOKEN="
if not defined UPTIME_API_KEY set "UPTIME_API_KEY="
set "ROTATE_ENCRYPTION_KEY=0"

REM ---------------------------------------------------------------------------
REM Arguments
REM ---------------------------------------------------------------------------
:parse_args
if "%~1"=="" goto args_done
set "_a=%~1"
if "!_a:~0,1!"=="/" set "_a=--!_a:~1!"
if /i "!_a!"=="--dry-run" (
  set "DRY_RUN=1"
  shift
  goto parse_args
)
if /i "!_a!"=="--non-interactive" (
  set "NON_INTERACTIVE=1"
  shift
  goto parse_args
)
if /i "!_a!"=="--force" (
  set "FORCE=1"
  shift
  goto parse_args
)
if /i "!_a!"=="--answers" (
  set "ANSWERS_FILE=%~2"
  set "NON_INTERACTIVE=1"
  shift
  shift
  goto parse_args
)
if /i "!_a!"=="--secrets" (
  set "SECRETS_INPUT=%~2"
  shift
  shift
  goto parse_args
)
if /i "!_a!"=="--set" (
  for /f "tokens=1* delims==" %%A in ("%~2") do set "%%A=%%B"
  set "NON_INTERACTIVE=1"
  shift
  shift
  goto parse_args
)
if /i "!_a!"=="--state-dir" (
  set "STATE_DIR=%~2"
  set "STATE_FILE=%~2/state.env"
  set "SECRETS_FILE=%~2/secrets.env"
  shift
  shift
  goto parse_args
)
if /i "!_a!"=="--rotate-encryption-key" (
  set "ROTATE_ENCRYPTION_KEY=1"
  shift
  goto parse_args
)
if /i "!_a!"=="--help" goto usage
if /i "!_a!"=="-h" goto usage
echo [stop] unknown option: %~1
exit /b 2

:usage
echo Aether deploy engine (Windows)
echo.
echo Deploy what the setup wizard collected. Run scripts\setup.bat first; it
echo writes the answers to %USERPROFILE%\.aether-setup\answers.env and the
echo secrets to %USERPROFILE%\.aether-setup\secrets.env, then:
echo.
echo   scripts\deploy.bat --answers %USERPROFILE%\.aether-setup\answers.env
echo.
echo Usage: deploy.bat [--dry-run] [--answers FILE] [--secrets FILE]
echo                   [--set KEY=VALUE] [--non-interactive] [--state-dir DIR]
echo                   [--force] [--rotate-encryption-key] [--help]
echo.
echo The answers file uses KEY=value lines and is the same format as setup.sh.
echo --force runs even when this machine already has setup state. Without it,
echo a second run exits without changing anything.
echo --rotate-encryption-key replaces the deployed content key on an existing
echo service, making every previously stored chat, memory, and trace row
echo unreadable. It is off by default, and needs --force on an already-run
echo machine.
echo See docs/hosted-setup.md.
exit /b 0

:args_done
if not exist "%REPO_ROOT%/pyproject.toml" (
  echo [stop] pyproject.toml not found above %SCRIPT_DIR%
  exit /b 1
)

REM Check and install the deploy requirements when Python is available. The
REM engine uses PowerShell and the built-in curl.exe, so Python is optional.
set "HAVE_PY="
where python >nul 2>&1 && set "HAVE_PY=1"
if defined HAVE_PY (
  set "PYTHONPATH=%REPO_ROOT%\src;%PYTHONPATH%"
  python -m aether.setup.requirements --mode deploy
  if errorlevel 1 exit /b 1
)

REM ---------------------------------------------------------------------------
REM Extract the embedded PowerShell helper and read the repo catalogs.
REM ---------------------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%SELF%'; $s=[IO.File]::ReadAllText($p); $a=('<#PS'+'HELPER#>'); $b=('<#END'+'PS#>'); $i=$s.IndexOf($a); $j=$s.IndexOf($b); if($i -lt 0 -or $j -lt 0){exit 2}; [IO.File]::WriteAllText('%HELPER%', $s.Substring($i+$a.Length, $j-$i-$a.Length), (New-Object Text.UTF8Encoding($false)))"
if errorlevel 1 (
  echo [stop] could not extract the PowerShell helper
  exit /b 1
)
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -File "%HELPER%""

if defined ANSWERS_FILE call :load_kv "%ANSWERS_FILE%"
if defined SECRETS_INPUT call :load_kv "%SECRETS_INPUT%"
if exist "%STATE_FILE%" call :load_kv "%STATE_FILE%"
if exist "%SECRETS_FILE%" call :load_kv "%SECRETS_FILE%"

REM A machine that already has setup state is not set up again by accident.
REM A dry-run is allowed because it changes nothing; everything else needs the
REM explicit --force.
if exist "%STATE_FILE%" if "%FORCE%"=="0" if "%DRY_RUN%"=="0" (
  echo [ ok ] Aether setup has already run on this machine.
  echo State: %STATE_FILE%
  echo Nothing was changed. Re-run with --force to update the deployment.
  exit /b 0
)

set "N=0"
%PS% catalog providers -RepoRoot "%REPO_ROOT%" > "%TMPBASE%/providers.txt"
%PS% catalog apps -RepoRoot "%REPO_ROOT%" > "%TMPBASE%/apps.txt"
%PS% catalog channels -RepoRoot "%REPO_ROOT%" > "%TMPBASE%/channels.txt"
%PS% catalog services -RepoRoot "%REPO_ROOT%" > "%TMPBASE%/services.txt"
set "DEFAULT_MODEL="
for /f "usebackq delims=" %%A in (`%PS% catalog defaultmodel -RepoRoot "%REPO_ROOT%"`) do set "DEFAULT_MODEL=%%A"
set "UPSTREAM_REPO="
for /f "usebackq delims=" %%A in (`%PS% catalog upstream -RepoRoot "%REPO_ROOT%"`) do set "UPSTREAM_REPO=%%A"
REM Q prefixes question-time echoes; in a non-interactive run they go to nul.
set "Q="
if "%NON_INTERACTIVE%"=="1" set "Q=>nul "

goto questions

REM Load KEY=value lines without executing them.
:load_kv
for /f "usebackq tokens=1* delims==" %%A in ("%~1") do (
  set "_k=%%A"
  set "_v=%%B"
  if not "!_k!"=="" if not "!_k:~0,1!"=="#" set "!_k!=!_v!"
)
exit /b 0

REM ---------------------------------------------------------------------------
REM Question helpers
REM ---------------------------------------------------------------------------
:pick_one
set "_var=%~1"
set "_title=%~2"
set "_def=%~3"
set "_kind=%~4"
set "_cur=!%_var%!"
if not "!_cur!"=="" exit /b 0
if "%NON_INTERACTIVE%"=="1" (
  call :nth_line "%TMPBASE%/!_kind!.txt" !_def! _opt
  set "%_var%=!_opt!"
  exit /b 0
)
echo.
echo !_title!
set /a "_n=0"
for /f "usebackq delims=" %%A in ("%TMPBASE%/!_kind!.txt") do (
  set /a "_n+=1"
  if "!_n!"=="!_def!" ( echo   !_n!^) %%A ^(default^) ) else ( echo   !_n!^) %%A )
)
set /p "_choice=Choose [!_def!]: "
if "!_choice!"=="" set "_choice=!_def!"
call :nth_line "%TMPBASE%/!_kind!.txt" !_choice! _opt
set "%_var%=!_opt!"
exit /b 0

:pick_many
set "_var=%~1"
set "_title=%~2"
set "_def=%~3"
set "_kind=%~4"
set "_cur=!%_var%!"
if not "!_cur!"=="" exit /b 0
if "%NON_INTERACTIVE%"=="1" (
  set "%_var%=!_def!"
  exit /b 0
)
echo.
echo !_title!
echo   comma-separated numbers, or Enter for the default
set /a "_n=0"
for /f "usebackq delims=" %%A in ("%TMPBASE%/!_kind!.txt") do (
  set /a "_n+=1"
  echo   !_n!^) %%A
)
set /p "_choice=Choose [!_def!]: "
if "!_choice!"=="" set "_choice=!_def!"
set "%_var%=!_choice!"
exit /b 0

:nth_line
set /a "_i=0"
set "_line="
for /f "usebackq delims=" %%A in ("%~1") do (
  set /a "_i+=1"
  if "!_i!"=="%~2" set "_line=%%A"
)
set "%~3=!_line!"
exit /b 0

:pick_fixed
set "_var=%~1"
set "_title=%~2"
set "_def=%~3"
set "_opts=%~4"
set "_cur=!%_var%!"
if not "!_cur!"=="" exit /b 0
set /a "_i=0"
for %%O in ("!_opts:|=" "!") do (
  set /a "_i+=1"
  set "_o[!_i!]=%%~O"
)
if "%NON_INTERACTIVE%"=="1" (
  call set "%_var%=%%_o[!_def!]%%"
  exit /b 0
)
echo.
echo !_title!
for /l %%i in (1,1,!_i!) do (
  if "%%i"=="!_def!" ( echo   %%i^) !_o[%%i]! ^(default^) ) else ( echo   %%i^) !_o[%%i]! )
)
set /p "_choice=Choose [!_def!]: "
if "!_choice!"=="" set "_choice=!_def!"
call set "%_var%=%%_o[!_choice!]%%"
exit /b 0

:ask_line
set "_var=%~1"
set "_q=%~2"
set "_def=%~3"
set "_cur=!%_var%!"
if not "!_cur!"=="" exit /b 0
if "%NON_INTERACTIVE%"=="1" (
  set "%_var%=!_def!"
  exit /b 0
)
set /p "_ans=!_q! [!_def!]: "
if "!_ans!"=="" set "_ans=!_def!"
set "%_var%=!_ans!"
exit /b 0

REM ---------------------------------------------------------------------------
REM The questions, in the same order as setup.sh
REM ---------------------------------------------------------------------------
:questions
%Q%echo.
%Q%echo ------------------------------------------------------------
%Q%echo Aether hosted setup
%Q%echo ------------------------------------------------------------

call :pick_fixed DATABASE_MODE "1. Database" 1 "neon|existing|render"
if /i "!DATABASE_MODE!"=="render" (
  echo [warn] Render's free Postgres expires 30 days after creation. Neon is recommended.
)

set "PROV_DEF=1"
set /a "_pn=0"
for /f "usebackq delims=" %%A in ("%TMPBASE%/providers.txt") do (
  set /a "_pn+=1"
  if /i "%%A"=="anthropic" set "PROV_DEF=!_pn!"
)
call :pick_one LLM_PROVIDER "2. Main model provider" !PROV_DEF! providers
set "EX_MODEL="
for /f "usebackq delims=" %%A in (`%PS% catalog examplemodel -Provider "!LLM_PROVIDER!" -RepoRoot "%REPO_ROOT%"`) do set "EX_MODEL=%%A"
if "!EX_MODEL!"=="" set "EX_MODEL=%DEFAULT_MODEL%"
%Q%echo.
%Q%echo   A bare id uses llm.provider; provider/model routes to that provider.
call :ask_line LLM_MODEL "  Main model" "!EX_MODEL!"

call :pick_many SERVICES "3. Optional services (multi-select)" "" services
for /f "usebackq delims=" %%A in (`%PS% map -Kind services -Pick "!SERVICES!" -RepoRoot "%REPO_ROOT%"`) do set "SERVICES=%%A"
echo !SERVICES! | findstr /i /c:"image generation" >nul && (
  call :pick_fixed IMAGE_PROVIDER "  Image provider" 1 "pollinations|gemini|none"
) || set "IMAGE_PROVIDER=none"
echo !SERVICES! | findstr /i /c:"background jobs" >nul && (
  call :pick_fixed WORKBENCH_PROVIDER "  Background-work sandbox" 1 "modal|vercel|github_actions|none"
) || set "WORKBENCH_PROVIDER=none"

call :pick_many APPS "4. Direct apps (multi-select)" "" apps
for /f "usebackq delims=" %%A in (`%PS% map -Kind apps -Pick "!APPS!" -RepoRoot "%REPO_ROOT%"`) do set "APPS=%%A"

call :pick_many CHANNELS "5. Chat channels (multi-select)" "" channels
for /f "usebackq delims=" %%A in (`%PS% map -Kind channels -Pick "!CHANNELS!" -RepoRoot "%REPO_ROOT%"`) do set "CHANNELS=%%A"

%Q%echo.
%Q%echo   free has no cost and spins down when idle; starter keeps the workspace.
call :pick_fixed PLAN "6. Render plan" 1 "free|starter"

call :ask_line SERVICE_NAME "7a. Render service name" "aether"
set "SUBCODE=000"
for /f "usebackq delims=" %%C in (`%PS% status -Url "https://!SERVICE_NAME!.onrender.com"`) do set "SUBCODE=%%C"
if "!SUBCODE!"=="200" echo [warn] https://!SERVICE_NAME!.onrender.com already answers; Render may append a suffix.
if "!SUBCODE!"=="301" echo [warn] https://!SERVICE_NAME!.onrender.com already answers; Render may append a suffix.
if "!SUBCODE!"=="302" echo [warn] https://!SERVICE_NAME!.onrender.com already answers; Render may append a suffix.
if "!SUBCODE!"=="403" echo [warn] https://!SERVICE_NAME!.onrender.com already answers; Render may append a suffix.
if "!SUBCODE!"=="503" echo [warn] https://!SERVICE_NAME!.onrender.com already answers; Render may append a suffix.
call :pick_fixed REGION "7b. Render region" 1 "oregon|ohio|virginia|frankfurt|singapore"
%Q%echo.
%Q%echo   7c. Deploy source
%Q%echo     public - deploy the upstream repo (https://github.com/Vedant-Git-dev/Aether)
%Q%echo     fork   - deploy your own fork (required for full API automation)
call :pick_fixed DEPLOY_SOURCE "  Deploy source" 1 "public|fork"
if /i "!DEPLOY_SOURCE!"=="fork" (
  call :ask_line REPO_URL "  Your fork URL" "https://github.com/CHANGE_ME/Aether"
  %Q%echo   When Render asks, authorize the GitHub app so it can see your fork.
  %Q%echo   Dashboard: New, Blueprint, connect GitHub, pick the fork.
) else (
  set "REPO_URL=https://github.com/Vedant-Git-dev/Aether"
)

%Q%echo.
%Q%echo   8. Uptime monitoring
%Q%echo     api     - create a keyword monitor with your UptimeRobot API key
%Q%echo     email   - no API key: UptimeRobot emails a one-click confirm link
%Q%echo     manual  - print the exact steps and open the page
%Q%echo     skip    - do nothing
call :pick_fixed UPTIME_MODE "  Uptime monitoring" 1 "api|email|manual|skip"
if /i "!UPTIME_MODE!"=="api" call :ask_line ALERT_EMAIL "  Alert email address" ""
if /i "!UPTIME_MODE!"=="email" call :ask_line ALERT_EMAIL "  Alert email address" ""

call :pick_fixed LOCAL_COPY "9. Also set up a local copy for testing?" 1 "no|yes"

%Q%echo.
%Q%echo ------------------------------------------------------------
%Q%echo Choices
%Q%echo ------------------------------------------------------------
%Q%echo   database:        !DATABASE_MODE!
%Q%echo   model:           !LLM_PROVIDER! / !LLM_MODEL!
%Q%echo   services:        !SERVICES!
%Q%echo   direct apps:     !APPS!
%Q%echo   channels:        !CHANNELS!
%Q%echo   plan:            !PLAN!
%Q%echo   service name:    !SERVICE_NAME!
%Q%echo   region:          !REGION!
%Q%echo   deploy source:   !DEPLOY_SOURCE! (!REPO_URL!)
%Q%echo   uptime:          !UPTIME_MODE!
%Q%echo   local copy:      !LOCAL_COPY!
if "%NON_INTERACTIVE%"=="0" if "%DRY_RUN%"=="0" (
  set /p "_go=Proceed? [Y/n]: "
  if /i "!_go!"=="n" ( echo [stop] nothing was changed & exit /b 1 )
)
goto credentials

REM ---------------------------------------------------------------------------
REM Credentials (hidden input via PowerShell)
REM ---------------------------------------------------------------------------
:ask_secret
set "_var=%~1"
set "_q=%~2"
set "_cur=!%_var%!"
if not "!_cur!"=="" (
  if "%NON_INTERACTIVE%"=="0" echo [ok] already set
  exit /b 0
)
if "%NON_INTERACTIVE%"=="1" (
  echo [warn] no value supplied for %_var% ^(non-interactive^)
  exit /b 0
)
for /f "usebackq delims=" %%V in (`%PS% hidden "!_q!"`) do set "%_var%=%%V"
exit /b 0

:open_url
if "%DRY_RUN%"=="1" ( echo   [dry-run] would open: %~1 & exit /b 0 )
if "%NON_INTERACTIVE%"=="1" exit /b 0
start "" "%~1"
exit /b 0

:has_word
echo !%~1! | findstr /i /c:"%~2" >nul
exit /b !errorlevel!

:credentials
%Q%echo.
%Q%echo ------------------------------------------------------------
%Q%echo Credentials
%Q%echo ------------------------------------------------------------
%Q%echo.
%Q%echo A Render account is required. If you do not have one, create it first:
%Q%echo   https://dashboard.render.com/register
%Q%echo   Render dashboard: https://dashboard.render.com
%Q%echo.
%Q%echo A Render API key lets this script create and configure everything.
%Q%echo Without one, a Blueprint and dashboard walkthrough are used.
call :open_url "https://dashboard.render.com/u/settings#api-keys"
%Q%echo Dashboard, top right, Account Settings, API Keys, Create API Key.
call :ask_secret RENDER_API_KEY "Render API key (Enter to skip and use the dashboard)"
if not "!RENDER_API_KEY!"=="" if "%NON_INTERACTIVE%"=="0" echo   Render API key set.

if /i "!DATABASE_MODE!"=="neon" (
  %Q%echo.
  %Q%echo Neon free Postgres: create a project at https://console.neon.tech
  %Q%echo Copy the connection string and keep sslmode=require.
  call :open_url "https://console.neon.tech"
  call :ask_secret DATABASE_URL "Paste the Neon connection string"
)
if /i "!DATABASE_MODE!"=="existing" call :ask_secret DATABASE_URL "Paste your Postgres connection string"
if /i "!DATABASE_MODE!"=="render" (
  %Q%echo Create it in the dashboard: New, Postgres. Copy the External Connection String.
  call :open_url "https://dashboard.render.com/new/database"
  call :ask_secret DATABASE_URL "Paste the Render Postgres external connection string"
)

if /i "!LLM_PROVIDER!"=="anthropic" (
  call :open_url "https://console.anthropic.com/settings/keys"
  call :ask_secret ANTHROPIC_KEY "Anthropic API key"
)
if /i "!LLM_PROVIDER!"=="openai" (
  call :open_url "https://platform.openai.com/api-keys"
  call :ask_secret OPENAI_KEY "OpenAI API key"
)
if /i "!LLM_PROVIDER!"=="gemini" (
  call :open_url "https://aistudio.google.com/apikey"
  call :ask_secret GEMINI_KEY "Gemini API key"
)
if /i "!LLM_PROVIDER!"=="nvidia" (
  call :open_url "https://build.nvidia.com/"
  call :ask_secret NVIDIA_KEY "NVIDIA API key"
)
if /i "!LLM_PROVIDER!"=="ollama" echo [warn] Ollama cannot run on Render; the main model will be unavailable.

call :has_word SERVICES "web search"
if not errorlevel 1 (
  call :open_url "https://app.tavily.com"
  call :ask_secret TAVILY_KEY "Tavily API key"
)
call :has_word SERVICES "voice-note transcription"
if not errorlevel 1 (
  call :open_url "https://console.groq.com/keys"
  call :ask_secret GROQ_KEY "Groq API key"
)
if "!GEMINI_KEY!"=="" if /i not "!SEMANTIC_MEMORY!"=="no" (
  call :open_url "https://aistudio.google.com/apikey"
  call :ask_secret GEMINI_KEY "Gemini API key (memory search)"
)
call :has_word SERVICES "background jobs"
if not errorlevel 1 (
  if /i "!WORKBENCH_PROVIDER!"=="modal" (
    call :open_url "https://modal.com/settings/tokens"
    call :ask_secret MODAL_TOKEN_ID "Modal token ID"
    call :ask_secret MODAL_TOKEN_SECRET "Modal token secret"
  )
  if /i "!WORKBENCH_PROVIDER!"=="vercel" (
    call :open_url "https://vercel.com/account/tokens"
    call :ask_secret VERCEL_TOKEN "Vercel token"
  )
)

call :has_word APPS "gmail"
if not errorlevel 1 goto google_apps
call :has_word APPS "calendar"
if not errorlevel 1 goto google_apps
call :has_word APPS "drive"
if not errorlevel 1 goto google_apps
call :has_word APPS "docs"
if not errorlevel 1 goto google_apps
goto after_google

:google_apps
%Q%echo.
%Q%echo Google OAuth client: https://console.cloud.google.com/apis/credentials
call :open_url "https://console.cloud.google.com/apis/credentials"
call :ask_secret GOOGLE_CLIENT_ID "Google OAuth client ID"
call :ask_secret GOOGLE_CLIENT_SECRET "Google OAuth client secret"
call :has_word APPS "drive"
if not errorlevel 1 (
  call :ask_secret GOOGLE_PICKER_KEY "Google Picker API key"
  call :ask_secret GOOGLE_PICKER_APP_ID "Google Picker app id"
)
call :has_word APPS "docs"
if not errorlevel 1 (
  call :ask_secret GOOGLE_PICKER_KEY "Google Picker API key"
  call :ask_secret GOOGLE_PICKER_APP_ID "Google Picker app id"
)
:after_google
call :has_word APPS "github"
if not errorlevel 1 (
  %Q%echo GitHub can connect with a fine-grained token from /apps after deploy.
  call :open_url "https://github.com/settings/developers"
)
call :has_word APPS "linkedin"
if not errorlevel 1 (
  call :open_url "https://www.linkedin.com/developers/apps"
  call :ask_secret LINKEDIN_CLIENT_ID "LinkedIn client ID"
  call :ask_secret LINKEDIN_CLIENT_SECRET "LinkedIn client secret"
)

call :has_word CHANNELS "telegram"
if not errorlevel 1 (
  call :open_url "https://t.me/BotFather"
  call :ask_secret TELEGRAM_TOKEN "Telegram bot token"
)
call :has_word CHANNELS "discord"
if not errorlevel 1 (
  call :open_url "https://discord.com/developers/applications"
  call :ask_secret DISCORD_TOKEN "Discord bot token"
)
if /i "!UPTIME_MODE!"=="api" (
  call :open_url "https://dashboard.uptimerobot.com/integrations"
  call :ask_secret UPTIME_API_KEY "UptimeRobot main API key"
)

REM --- generated secrets ---
echo.
if "!AETHER_ENCRYPTION_KEY!"=="" (
  for /f "usebackq delims=" %%V in (`%PS% randkey`) do set "AETHER_ENCRYPTION_KEY=%%V"
)
if "!AETHER_TOKEN!"=="" (
  for /f "usebackq delims=" %%V in (`%PS% randtoken`) do set "AETHER_TOKEN=%%V"
)
if "!TOKEN_ENC_KEY!"=="" (
  for /f "usebackq delims=" %%V in (`%PS% randkey`) do set "TOKEN_ENC_KEY=%%V"
)
if "!TOKEN_ENC_KEY_ID!"=="" (
  for /f "usebackq delims=" %%V in (`%PS% yearmonth`) do set "TOKEN_ENC_KEY_ID=%%V"
)
echo [ok] encryption material generated.
goto run_env

:run_env
set "NAMES=%TMPBASE%/names.txt"
> "%NAMES%" echo DATABASE_MODE
>> "%NAMES%" echo DATABASE_URL
>> "%NAMES%" echo LLM_PROVIDER
>> "%NAMES%" echo LLM_MODEL
>> "%NAMES%" echo SERVICES
>> "%NAMES%" echo APPS
>> "%NAMES%" echo CHANNELS
>> "%NAMES%" echo PLAN
>> "%NAMES%" echo SERVICE_NAME
>> "%NAMES%" echo REGION
>> "%NAMES%" echo DEPLOY_SOURCE
>> "%NAMES%" echo REPO_URL
>> "%NAMES%" echo UPTIME_MODE
>> "%NAMES%" echo ALERT_EMAIL
>> "%NAMES%" echo LOCAL_COPY
>> "%NAMES%" echo WORKBENCH_PROVIDER
>> "%NAMES%" echo IMAGE_PROVIDER
>> "%NAMES%" echo AETHER_ENCRYPTION_KEY
>> "%NAMES%" echo AETHER_TOKEN
>> "%NAMES%" echo TOKEN_ENC_KEY
>> "%NAMES%" echo TOKEN_ENC_KEY_ID
>> "%NAMES%" echo ROTATE_ENCRYPTION_KEY
>> "%NAMES%" echo RENDER_API_KEY
>> "%NAMES%" echo TAVILY_KEY
>> "%NAMES%" echo GROQ_KEY
>> "%NAMES%" echo GEMINI_KEY
>> "%NAMES%" echo ANTHROPIC_KEY
>> "%NAMES%" echo OPENAI_KEY
>> "%NAMES%" echo NVIDIA_KEY
>> "%NAMES%" echo TELEGRAM_TOKEN
>> "%NAMES%" echo DISCORD_TOKEN
>> "%NAMES%" echo GOOGLE_CLIENT_ID
>> "%NAMES%" echo GOOGLE_CLIENT_SECRET
>> "%NAMES%" echo GOOGLE_PICKER_KEY
>> "%NAMES%" echo GOOGLE_PICKER_APP_ID
>> "%NAMES%" echo GITHUB_CLIENT_ID
>> "%NAMES%" echo GITHUB_CLIENT_SECRET
>> "%NAMES%" echo LINKEDIN_CLIENT_ID
>> "%NAMES%" echo LINKEDIN_CLIENT_SECRET
>> "%NAMES%" echo MODAL_TOKEN_ID
>> "%NAMES%" echo MODAL_TOKEN_SECRET
>> "%NAMES%" echo VERCEL_TOKEN
>> "%NAMES%" echo UPTIME_API_KEY
>> "%NAMES%" echo SERVICE_ID
>> "%NAMES%" echo SERVICE_URL
>> "%NAMES%" echo NON_INTERACTIVE
%PS% dump -Names "%NAMES%" -Out "%RUNENV%"
if errorlevel 1 (
  echo [stop] could not prepare the run file
  exit /b 1
)
%PS% run -RunEnv "%RUNENV%" -RepoRoot "%REPO_ROOT%" -StateDir "%STATE_DIR%" -DryRun %DRY_RUN%
set "RC=!errorlevel!"
del /q "%RUNENV%" >nul 2>&1
exit /b !RC!

<#PSHELPER#>
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$AllArgs = @($args)
function Get-Arg([string]$name, $default = $null) {
  for ($i = 0; $i -lt $AllArgs.Count; $i++) {
    if ($AllArgs[$i] -eq $name) { if ($i + 1 -lt $AllArgs.Count) { return $AllArgs[$i + 1] } }
  }
  return $default
}

$Command = if ($args.Count -gt 0) { $args[0] } else { "" }
$RepoRoot = Get-Arg "-RepoRoot" "."
$Provider = Get-Arg "-Provider" ""
$Pick = Get-Arg "-Pick" ""
$RunEnv = Get-Arg "-RunEnv" ""
$StateDir = Get-Arg "-StateDir" (Join-Path $env:USERPROFILE ".aether-setup")
$DryRun = (Get-Arg "-DryRun" "0") -eq "1"

function Read-Kv([string]$path) {
  $h = @{}
  if (Test-Path $path) {
    foreach ($line in Get-Content -LiteralPath $path) {
      if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
      $i = $line.IndexOf('=')
      $k = $line.Substring(0, $i).Trim()
      $v = $line.Substring($i + 1)
      if ($k -match '^[A-Za-z_][A-Za-z0-9_]*$') { $h[$k] = $v }
    }
  }
  return $h
}

function Write-Utf8Lines([string]$path, [object[]]$lines) {
  # A file written by an older build can carry an ACL that denies the current
  # user. Clear it before writing so setup never inherits that denial.
  try {
    [IO.File]::WriteAllLines($path, $lines, (New-Object Text.UTF8Encoding($false)))
  } catch {
    try { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue } catch { }
    try { icacls $path /reset | Out-Null } catch { }
    [IO.File]::WriteAllLines($path, $lines, (New-Object Text.UTF8Encoding($false)))
  }
}

function Protect-OwnerOnly([string]$path) {
  # Restrict a secrets file to the current user. Uses the resolved identity,
  # not the bare USERNAME, so the grant cannot silently fail and leave the
  # file with no access control entry.
  try {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = Get-Acl -LiteralPath $path
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($me, "FullControl", "Allow")
    $acl.AddAccessRule($rule)
    $acl.SetAccessRuleProtection($true, $false)
    Set-Acl -LiteralPath $path -AclObject $acl
  } catch {
    # A permission tweak never fails the deploy; the profile is already private.
  }
}

function Write-Kv([string]$path, [hashtable]$data) {
  $dir = Split-Path -Parent $path
  if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $existing = Read-Kv $path
  if ($null -eq $existing) { $existing = @{} }
  foreach ($k in $data.Keys) { $existing[$k] = $data[$k] }
  $lines = foreach ($k in ($existing.Keys | Sort-Object)) { "$k=$($existing[$k])" }
  Write-Utf8Lines $path $lines
}

function Get-Text([string]$rel) {
  $p = Join-Path $RepoRoot $rel
  if (Test-Path $p) { return Get-Content -Raw -LiteralPath $p }
  return ""
}

function Get-Catalog([string]$kind) {
  switch ($kind) {
    "providers" {
      $m = [regex]::Match((Get-Text "src/aether/config.py"), 'provider: Literal\[([^\]]+)\]')
      if ($m.Success) { return @($m.Groups[1].Value -split ',' | ForEach-Object { $_.Trim().Trim('"') }) }
    }
    "apps" {
      $out = @()
      $dir = Join-Path $RepoRoot "src/aether/providers"
      if (Test-Path $dir) {
        foreach ($f in Get-ChildItem $dir -Filter *.py) {
          $t = Get-Content -Raw -LiteralPath $f.FullName
          foreach ($m in [regex]::Matches($t, '=\s*AppDefinition\(([\s\S]*?)\r?\n\)')) {
            $body = $m.Groups[1].Value
            $k = [regex]::Match($body, 'key="([^"]+)"')
            $n = [regex]::Match($body, 'name="([^"]+)"')
            if ($k.Success) {
              if ($n.Success) { $out += "$($k.Groups[1].Value) :: $($n.Groups[1].Value)" } else { $out += $k.Groups[1].Value }
            }
          }
        }
      }
      return @($out | Sort-Object -Unique)
    }
    "channels" {
      $t = Get-Text "src/aether/apps_catalog.py"
      $out = @()
      foreach ($m in [regex]::Matches($t, 'key="([a-z]+)"[\s\S]{0,500}?kind="messaging"')) {
        $out += $m.Groups[1].Value
      }
      return @($out | Sort-Object -Unique)
    }
    "services" {
      $t = Get-Text "docs/quickstart.md"
      $lines = ($t -split "\r?\n")
      $in = $false; $out = @()
      foreach ($line in $lines) {
        if ($line -match '^## Optional services') { $in = $true; continue }
        if ($in -and $line -match '^## ') { break }
        if ($in -and $line -match '^\|') {
          $cell = ($line.Trim('|') -split '\|')[0].Trim()
          if ($cell -and $cell -ne "Capability" -and $cell -notmatch '^-+$' -and $cell -notmatch '^(Screen capture|Semantic memory)$') { $out += $cell }
        }
      }
      return @($out)
    }
    "defaultmodel" {
      $m = [regex]::Match((Get-Text "config.example.yaml"), '(?m)^\s+model:\s*(\S+)')
      if ($m.Success) { return $m.Groups[1].Value }
    }
    "examplemodel" {
      $t = Get-Text "config.example.yaml"
      $m = [regex]::Match($t, [regex]::Escape($Provider) + ',\s*model:\s*([A-Za-z0-9._/-]+)')
      if ($m.Success) { return $m.Groups[1].Value }
      $d = [regex]::Match($t, '(?m)^\s+model:\s*(\S+)')
      if ($d.Success) { return $d.Groups[1].Value }
    }
    "upstream" {
      try { $u = (git -C $RepoRoot config --get remote.origin.url 2>$null) } catch { $u = "" }
      if ($u) { return ($u -replace '^git@github.com:', 'https://github.com/' -replace '\.git$', '') }
      $m = [regex]::Match((Get-Text "docs/quickstart.md"), 'https://github\.com/[^\s)]+/Aether[^\s)]*')
      if ($m.Success) { return $m.Value }
      return "https://github.com/Vedant-Git-dev/Aether"
    }
  }
  return @()
}

function Show-Catalog([string]$kind) {
  foreach ($item in (Get-Catalog $kind)) { $item }
}

function Show-Map([string]$kind) {
  $items = @(Get-Catalog $kind)
  $out = @()
  foreach ($raw in ($Pick -split ',')) {
    $x = $raw.Trim()
    if ($x -match '^\d+$') {
      $idx = [int]$x
      if ($idx -ge 1 -and $idx -le $items.Count) { $out += ($items[$idx - 1] -split '\s*::\s*')[0] }
    } elseif ($x) { $out += $x }
  }
  return ($out -join ',')
}

function New-RandomKey {
  $b = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  return [Convert]::ToBase64String($b)
}

function New-RandomToken {
  $b = New-Object byte[] 64
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  $s = [Convert]::ToBase64String($b) -replace '[=+/]', ''
  return $s.Substring(0, 64)
}


function Invoke-Run {
  $e = Read-Kv $RunEnv
  function G([string]$k, [string]$d = "") { if ($e.ContainsKey($k) -and $e[$k]) { return $e[$k] } return $d }
  function HasList([string]$csv, [string]$needle) {
    $x = "," + ($csv -replace '\s', '') + ","
    return $x.ToLower().Contains("," + $needle.ToLower() + ",")
  }
  function Out([string]$s) { Write-Host $s }
  function Hr { Write-Host "------------------------------------------------------------" }

  $services = G "SERVICES"
  $apps = G "APPS"
  $channels = G "CHANNELS"
  $plan = G "PLAN" "free"
  $region = G "REGION" "oregon"
  $name = G "SERVICE_NAME" "aether"
  $repo = G "REPO_URL"
  $repo = $repo -replace '^git@github.com:', 'https://github.com/' -replace '\.git$', ''
  $uptimeMode = G "UPTIME_MODE" "skip"
  $email = G "ALERT_EMAIL"
  $token = G "AETHER_TOKEN"
  $enc = G "AETHER_ENCRYPTION_KEY"
  $rotateEnc = G "ROTATE_ENCRYPTION_KEY" "0"
  $db = G "DATABASE_URL"
  if (-not $db -and -not $dry) {
    Out "[stop] DATABASE_URL is required. Re-run and paste the Neon, existing, or Render Postgres connection string."
    return
  }
  $tokKey = G "TOKEN_ENC_KEY"
  $tokKeyId = G "TOKEN_ENC_KEY_ID"
  $renderKey = G "RENDER_API_KEY"
  $savedSid = G "SERVICE_ID"
  $urKey = G "UPTIME_API_KEY"
  $dry = $DryRun

  $embed = "gemini-embedding-001"
  $tg = if (HasList $channels "telegram") { "true" } else { "false" }
  $dc = if (HasList $channels "discord") { "true" } else { "false" }
  $img = G "IMAGE_PROVIDER" "none"
  $wb = G "WORKBENCH_PROVIDER" "none"
  $needsPublic = ($apps -ne "") -or ($wb -eq "github_actions")

  $config = @"
# Generated by scripts/setup.bat. No secrets here; secrets live in Render.
llm:
  provider: $($e['LLM_PROVIDER'])
  model: $($e['LLM_MODEL'])
  embedding_model: $embed
messaging:
  telegram:
    enabled: $tg
  discord:
    enabled: $dc
media:
  image_provider: $img
workbench:
  provider: $wb
providers:
  enabled: true
"@
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($config))

  $envp = [ordered]@{}
  if ($db) { $envp["DATABASE_URL"] = $db }
  if ($enc) { $envp["AETHER_ENCRYPTION_KEY"] = $enc }
  if ($token) { $envp["AETHER_TOKEN"] = $token }
  if ($b64) { $envp["AETHER_CONFIG_B64"] = $b64 }
  $envp["PYTHON_VERSION"] = "3.12.11"
  $envp["NODE_VERSION"] = "22.12.0"
  switch (G "LLM_PROVIDER") {
    "anthropic" { if (G "ANTHROPIC_KEY") { $envp["ANTHROPIC_API_KEY"] = G "ANTHROPIC_KEY" } }
    "openai" { if (G "OPENAI_KEY") { $envp["OPENAI_API_KEY"] = G "OPENAI_KEY" } }
    "gemini" { if (G "GEMINI_KEY") { $envp["GEMINI_API_KEY"] = G "GEMINI_KEY" } }
    "nvidia" { if (G "NVIDIA_KEY") { $envp["NVIDIA_API_KEY"] = G "NVIDIA_KEY" } }
  }
  if (HasList $services "web search") { if (G "TAVILY_KEY") { $envp["TAVILY_API_KEY"] = G "TAVILY_KEY" } }
  if (HasList $services "voice-note transcription") { if (G "GROQ_KEY") { $envp["GROQ_API_KEY"] = G "GROQ_KEY" } }
  if (G "GEMINI_KEY") { $envp["GEMINI_API_KEY"] = G "GEMINI_KEY" }
  if (HasList $services "background jobs") {
    if ($wb -eq "modal") {
      if (G "MODAL_TOKEN_ID") { $envp["MODAL_TOKEN_ID"] = G "MODAL_TOKEN_ID" }
      if (G "MODAL_TOKEN_SECRET") { $envp["MODAL_TOKEN_SECRET"] = G "MODAL_TOKEN_SECRET" }
    } elseif ($wb -eq "vercel") {
      if (G "VERCEL_TOKEN") { $envp["VERCEL_TOKEN"] = G "VERCEL_TOKEN" }
    }
  }
  if ($tokKey) { $envp["AETHER_TOKEN_ENCRYPTION_KEY"] = $tokKey }
  if ($tokKeyId) { $envp["AETHER_TOKEN_ENCRYPTION_KEY_ID"] = $tokKeyId }
  foreach ($pair in @(@("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"), @("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"),
                     @("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_ID"), @("GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"),
                     @("GOOGLE_PICKER_API_KEY", "GOOGLE_PICKER_KEY"), @("GOOGLE_PICKER_APP_ID", "GOOGLE_PICKER_APP_ID"),
                     @("GITHUB_CLIENT_ID", "GITHUB_CLIENT_ID"), @("GITHUB_CLIENT_SECRET", "GITHUB_CLIENT_SECRET"),
                     @("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_ID"), @("LINKEDIN_CLIENT_SECRET", "LINKEDIN_CLIENT_SECRET"))) {
    $v = G $pair[1]
    if ($v) { $envp[$pair[0]] = $v }
  }

  function OpenUrl([string]$u) {
    if ($dry) { Out "  [dry-run] would open: $u"; return }
    Out "  Opening: $u"
    if ((G "NON_INTERACTIVE") -eq "1") { return }
    try { Start-Process $u | Out-Null } catch { Out "  open this link: $u" }
  }

  $buildCmd = 'if [ -n "$AETHER_CONFIG_B64" ]; then printf %s "$AETHER_CONFIG_B64" | base64 -d > config.yaml; fi; cd webapp && npm ci && npm run build && cd .. && pip install .'
  $sid = ""; $url = ""; $depId = ""

  function Api([string]$method, [string]$path, $body = $null) {
    $headers = @{ Authorization = "Bearer $renderKey"; Accept = "application/json" }
    $uri = "https://api.render.com/v1$path"
    if ($null -ne $body) {
      return Invoke-RestMethod -Method $method -Uri $uri -Headers $headers -ContentType "application/json" -Body $body
    }
    return Invoke-RestMethod -Method $method -Uri $uri -Headers $headers
  }

  if ($renderKey) {
    Out "Using the Render API (own fork detected)."
    $owner = "tea-dryrun"
    if (-not $dry) {
      $owners = @(Api "GET" "/owners?limit=1")
      $owner = $owners[0].owner.id
      if (-not $owner) { $owner = $owners[0].id }
      if ($savedSid) {
        try { $e0 = Api "GET" "/services/$savedSid"; if ($e0.id) { $sid = $e0.id } } catch { }
      }
      if (-not $sid) {
        $list = @(Api "GET" ("/services?name=" + [uri]::EscapeDataString($name) + "&limit=20"))
        foreach ($row in $list) { if ($row.service.name -eq $name) { $sid = $row.service.id } }
      }
    }

    if ($sid) {
      Out "Found existing service $sid (updating, not creating)."
      if (-not $dry) {
        # the content key is not rotatable in place: keep the deployed one
        # unless the operator explicitly asks to rotate it
        if ($rotateEnc -ne "1") {
          try {
            $cur = Api "GET" "/services/$sid/env-vars/AETHER_ENCRYPTION_KEY"
            if ($cur.value -and $cur.value -ne $enc) {
              Out "[warn] keeping the deployed encryption key so existing data stays readable."
              Out "  Pass --rotate-encryption-key to replace it (old rows become unreadable)."
              $enc = $cur.value
              $envp["AETHER_ENCRYPTION_KEY"] = $enc
            }
          } catch {
            Out "[warn] could not read the deployed encryption key."
            Out "  If this service already has data, stop and restore the original key."
          }
        }
        $svc = Api "GET" "/services/$sid"
        $url = $svc.serviceDetails.url
        foreach ($k in $envp.Keys) {
          Api "PUT" "/services/$sid/env-vars/$k" (@{ value = $envp[$k] } | ConvertTo-Json) | Out-Null
        }
        $dep = Api "POST" "/services/$sid/deploys" (@{ clearCache = "do_not_clear" } | ConvertTo-Json)
        $depId = $dep.id
      } else {
        $url = "https://$name.onrender.com"
      }
    } else {
      $sd = [ordered]@{
        runtime = "python"; plan = $plan; region = $region; healthCheckPath = "/healthz"
        envSpecificDetails = [ordered]@{ buildCommand = $buildCmd; startCommand = "aether" }
      }
      $arr = @()
      foreach ($k in $envp.Keys) { $arr += @{ key = $k; value = $envp[$k] } }
      if ($plan -ne "free") {
        $sd["disk"] = @{ name = "workspace"; mountPath = "/opt/render/project/src/aether-workspace"; sizeGB = 1 }
      }
      if ($dry) {
        Out "  [dry-run] would POST /services to create '$name' on plan '$plan' in $region"
        $sid = "srv-dryrun"
        $url = "https://$name.onrender.com"
      } else {
        $payload = @{ type = "web_service"; name = $name; ownerId = $owner; repo = $repo; autoDeploy = "yes"; envVars = $arr; serviceDetails = $sd } | ConvertTo-Json -Depth 12
        Out "Creating Render service '$name'..."
        try {
          $resp = Api "POST" "/services" $payload
          $sid = $resp.service.id
          OpenUrl "https://dashboard.render.com/web/$sid"
        } catch {
          Out "[stop] service creation failed: $($_.Exception.Message)"
          return
        }
        $svc = Api "GET" "/services/$sid"
        $url = $svc.serviceDetails.url
        if (-not $url) { $url = "https://$name.onrender.com" }
        Api "PUT" "/services/$sid/env-vars/AETHER_PUBLIC_URL" (@{ value = $url } | ConvertTo-Json) | Out-Null
        if ($needsPublic) {
          $dep = Api "POST" "/services/$sid/deploys" (@{ clearCache = "do_not_clear" } | ConvertTo-Json)
          $depId = $dep.id
        } else {
          Out "  No direct apps or GitHub Actions queue: the URL is stored; no second deploy needed."
        }
      }
    }

    if (-not $dry) {
      Out "Waiting for the deploy (the first build takes a few minutes)..."
      $status = ""; $tries = 0
      while ($tries -lt 120) {
        try {
          if ($depId) { $d = Api "GET" "/services/$sid/deploys/$depId"; $status = $d.status }
          else { $d = @(Api "GET" "/services/$sid/deploys?limit=1")[0]; $status = $d.deploy.status }
        } catch { $status = "" }
        if ($status -eq "live") { break }
        if ($status -in @("build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated")) { break }
        if ($status) { Out "  ... $status" }
        Start-Sleep -Seconds 10
        $tries++
      }
      if ($status -eq "live") { Out "[ ok ] deploy is live" }
      else {
        Out "[warn] deploy ended with status: $status"
        try { (Api "GET" "/services/$sid/events?limit=30") | ForEach-Object { if ($_.event) { Out ("  " + $_.event) } } } catch { }
        Out "  Full build log: https://dashboard.render.com/web/$sid/deploys"
      }
    }
  } else {
    Out "[warn] No Render API key: creating the service with a Blueprint."
    $disk = if ($plan -ne "free") { "    disk:`n      name: workspace`n      mountPath: /opt/render/project/src/aether-workspace`n      sizeGB: 1`n" } else { "" }
    $need = @("DATABASE_URL", "AETHER_ENCRYPTION_KEY", "AETHER_TOKEN", "AETHER_CONFIG_B64")
    $keys = @("PYTHON_VERSION", "NODE_VERSION")
    switch (G "LLM_PROVIDER") {
      "anthropic" { $keys += "ANTHROPIC_API_KEY" }
      "openai" { $keys += "OPENAI_API_KEY" }
      "gemini" { $keys += "GEMINI_API_KEY" }
      "nvidia" { $keys += "NVIDIA_API_KEY" }
    }
    if (HasList $services "web search") { $keys += "TAVILY_API_KEY" }
    if (HasList $services "voice-note transcription") { $keys += "GROQ_API_KEY" }
    $keys += "GEMINI_API_KEY"
    if (HasList $services "background jobs") {
      if ($wb -eq "modal") { $keys += @("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") }
      elseif ($wb -eq "vercel") { $keys += @("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID") }
      elseif ($wb -eq "github_actions") { $keys += "GITHUB_ACTIONS_TOKEN" }
    }
    $keys += @("AETHER_TOKEN_ENCRYPTION_KEY", "AETHER_TOKEN_ENCRYPTION_KEY_ID")
    if ($apps -match 'gmail|calendar|drive|docs') { $keys += @("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET") }
    if ($apps -match 'drive|docs') { $keys += @("GOOGLE_PICKER_API_KEY", "GOOGLE_PICKER_APP_ID") }
    if ($apps -match 'github') { $keys += @("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET") }
    if ($apps -match 'linkedin') { $keys += @("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET") }
    if ($channels -match 'telegram') { $keys += "TELEGRAM_BOT_TOKEN" }
    if ($channels -match 'discord') { $keys += "DISCORD_BOT_TOKEN" }
    $all = @($need + $keys) | Select-Object -Unique
    $y = @()
    $y += "# Generated by scripts/setup.bat for Aether. No secrets live here."
    $y += "services:"
    $y += "  - type: web"
    $y += "    name: $name"
    $y += "    runtime: python"
    $y += "    plan: $plan"
    $y += "    region: $region"
    $y += "    buildCommand: '$buildCmd'"
    $y += "    startCommand: 'aether'"
    $y += "    healthCheckPath: /healthz"
    if ($plan -ne "free") { $y += "    disk:"; $y += "      name: workspace"; $y += "      mountPath: /opt/render/project/src/aether-workspace"; $y += "      sizeGB: 1" }
    $y += "    envVars:"
    $y += "      - key: PYTHON_VERSION"
    $y += "        value: '3.12.11'"
    $y += "      - key: NODE_VERSION"
    $y += "        value: '22.12.0'"
    foreach ($k in $all) {
      if ($k -eq "PYTHON_VERSION" -or $k -eq "NODE_VERSION") { continue }
      $y += "      - key: $k"
      $y += "        sync: false"
    }
    $y += "      - key: AETHER_PUBLIC_URL"
    $y += "        fromService:"
    $y += "          name: $name"
    $y += "          type: web"
    $y += "          envVarKey: RENDER_EXTERNAL_URL"
    $bp = Join-Path $StateDir "render.yaml"
    [IO.File]::WriteAllLines($bp, $y, (New-Object Text.UTF8Encoding($false)))
    Out "[ ok ] Blueprint written to $bp"
    Out "  1. Push $bp as render.yaml to the repo ($repo)."
    Out "  2. Open https://dashboard.render.com/blueprints and connect it."
    Out "  3. Render prompts only for the variables this plan uses."
    OpenUrl "https://dashboard.render.com/blueprints"
    Out "  AETHER_CONFIG_B64 (not secret): $b64"
    $url = "https://$name.onrender.com"
  }

  function Solve-Pow([string]$nonce, [int]$difficulty) {
    $counter = 0
    $sha = [Security.Cryptography.SHA256]::Create()
    while ($true) {
      $h = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$nonce|$counter"))
      $z = 0
      foreach ($b in $h) {
        if ($b -eq 0) { $z += 8 } else { $z += 8 - [Convert]::ToString($b, 2).Length; break }
      }
      if ($z -ge $difficulty) { return $counter }
      $counter++
    }
  }

  function Ur([string]$endpoint, [hashtable]$params) {
    $params["api_key"] = $urKey
    $params["format"] = "json"
    return Invoke-RestMethod -Method Post -Uri "https://api.uptimerobot.com/v2/$endpoint" -Body $params
  }

  $target = "$url/healthz"
  $monitorId = ""
  Out ""
  Hr
  Out "Uptime monitoring"
  Hr
  switch ($uptimeMode) {
    "api" {
      if (-not $urKey) { Out "[warn] no UptimeRobot API key; skipping."; break }
      Out "  Alerts go to the email on your UptimeRobot account; make sure it is the address you want."
      if ($dry) { Out "  [dry-run] would create or update an UptimeRobot keyword monitor on $target"; break }
      try {
        $mons = @((Ur "getMonitors" @{ limit = 50 }).monitors)
        $found = $mons | Where-Object { $_.url -eq $target } | Select-Object -First 1
        if ($found) {
          $monitorId = $found.id
          Out "  Updating existing monitor $monitorId"
          Ur "editMonitor" @{ id = $monitorId; friendly_name = "aether $name"; url = $target; interval = 300 } | Out-Null
        } else {
          Out "  Creating keyword monitor on $target"
          $r = Ur "newMonitor" @{ friendly_name = "aether $name"; url = $target; type = 2; keyword_type = 1; keyword_value = "ok"; interval = 300 }
          $monitorId = $r.monitor.id
        }
        Out "[ ok ] monitor ready (id $monitorId)"
      } catch { Out "[warn] UptimeRobot: $($_.Exception.Message)" }
    }
    "email" {
      if ($dry) { Out "  [dry-run] would submit the no-API-key monitor request"; break }
      $chUrl = "https://api.uptimerobot.com/agentic/agent-monitor/challenge?email=" + [uri]::EscapeDataString($email) + "&url=" + [uri]::EscapeDataString($target)
      try {
        $ch = Invoke-RestMethod -Uri $chUrl -TimeoutSec 30
        $counter = Solve-Pow $ch.nonce ([int]$ch.difficulty)
        $body = @{ email = $email; url = $target; nonce = $ch.nonce; timestamp = $ch.timestamp; counter = $counter; signature = $ch.signature } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "https://api.uptimerobot.com/agentic/agent-monitor" -ContentType "application/json" -Body $body -TimeoutSec 30 | Out-Null
        Out "[ ok ] request accepted; check $email for the activation link"
      } catch {
        Out "[warn] $($_.Exception.Message); opening the browser flow"
        OpenUrl ("https://uptimerobot.com/quick-start?url=" + [uri]::EscapeDataString($target) + "&email=" + [uri]::EscapeDataString($email))
      }
      Out "  Click Activate in the UptimeRobot email (the link expires in 24h)."
    }
    "manual" {
      Out "  Manual monitoring steps:"
      Out "    1. Open https://uptimerobot.com and create a free account."
      Out "    2. New Monitor, type Keyword."
      Out "    3. URL: $target"
      Out "    4. Keyword type: exists; keyword: ok"
      Out "    5. Interval: 5 minutes."
      Out "    6. Save. The 5-minute check keeps the free instance from spinning down."
      OpenUrl "https://dashboard.uptimerobot.com/integrations"
    }
    default { Out "  Uptime monitoring skipped at your request." }
  }

  # OAuth redirect URIs (the code path is /api/oauth/<provider>/callback)
  if ($apps) {
    Out ""
    Out "OAuth redirect URIs to register:"
    if ($apps -match 'gmail|calendar|drive|docs') {
      Out "  Google:   $url/api/oauth/google/callback"
      Out "  Origin:   $url  (authorized JavaScript origin for the Drive picker)"
      OpenUrl "https://console.cloud.google.com/apis/credentials"
    }
    if ($apps -match 'github') {
      Out "  GitHub:   $url/api/oauth/github/callback"
      OpenUrl "https://github.com/settings/developers"
    }
    if ($apps -match 'linkedin') {
      Out "  LinkedIn: $url/api/oauth/linkedin/callback"
      OpenUrl "https://www.linkedin.com/developers/apps"
    }
    if (-not $dry -and (G "NON_INTERACTIVE") -ne "1") {
      Read-Host "Press Enter once the redirect URIs are registered" | Out-Null
    }
  }

  Out ""
  Hr
  Out "Verification"
  Hr
  if (-not $dry) {
    $healthy = $false
    for ($i = 0; $i -lt 40; $i++) {
      try {
        $r = Invoke-WebRequest -UseBasicParsing "$url/healthz" -TimeoutSec 25
        if ($r.Content -match '"ok"\s*:\s*true') { $healthy = $true; break }
      } catch { }
      Start-Sleep -Seconds 10
    }
    if ($healthy) { Out '[ ok ] healthz returned {"ok":true}' } else { Out "[warn] healthz did not return ok in time" }

    if ($sid -and $renderKey) {
      try {
        $evtxt = (Api "GET" "/services/$sid/events?limit=40" | Out-String)
        if ($evtxt -match 'database ready' -or $evtxt -match 'migrations applied') {
          Out "[ ok ] migrations ran (the boot log shows the database ready line)"
        } else {
          Out "  Migrations run on boot; confirm in the Render logs: https://dashboard.render.com/web/$sid/logs"
        }
        if (-not $healthy -and $evtxt -match 'DATABASE_URL is not set') {
          Out "[warn] The app is failing because DATABASE_URL is not set on the Render service."
          Out "  Fix: Render Dashboard, the service, Environment: add DATABASE_URL,"
          Out "  AETHER_ENCRYPTION_KEY, AETHER_TOKEN, AETHER_CONFIG_B64, PYTHON_VERSION, and NODE_VERSION."
        }
      } catch { }
    }

    try {
      $p = Invoke-WebRequest -UseBasicParsing "$url/" -TimeoutSec 25
      Out "[ ok ] panel is served (HTTP $($p.StatusCode))"
    } catch {
      $code = 0
      try { $code = $_.Exception.Response.StatusCode.value__ } catch { }
      if ($code -eq 503) { Out "[warn] panel build is missing (HTTP 503); the npm build step likely failed" }
      else { Out "[warn] panel returned HTTP $code" }
    }

    try {
      Invoke-RestMethod "$url/api/config" -Headers @{ "x-aether-token" = $token } -TimeoutSec 25 | Out-Null
      Out "[ ok ] AETHER_TOKEN is accepted by the API"
    } catch { Out "[warn] could not confirm the token against /api/config" }

    if ($monitorId -and $urKey) {
      try {
        $st = ((Ur "getMonitors" @{ monitors = $monitorId }).monitors | Select-Object -First 1).status
        if ($st -eq 2) { Out "[ ok ] monitor reports UP" }
        elseif ($st -in @(8, 9)) { Out "[warn] monitor reports DOWN; the service may still be starting" }
        else { Out "  Monitor created; its first check (up to 5 minutes) is pending." }
      } catch { }
    }
  } else {
    Out "  [dry-run] would verify healthz, the panel, the token, migrations, and the monitor"
  }

  $masked = if ($token -and $token.Length -gt 8) { $token.Substring(0, 4) + "..." + $token.Substring($token.Length - 4) } else { "********" }
  Out ""
  Hr
  Out "Done"
  Hr
  Out "  Live URL:   $url"
  Out "  Panel:      $url  (paste the token under Settings > Access)"
  Out "  Health:     $url/healthz"
  if ($sid) { Out "  Render:     https://dashboard.render.com/web/$sid" }
  Out "  AETHER_TOKEN: $masked"
  Out "  Back up AETHER_ENCRYPTION_KEY outside Render now (password manager)."
  Out "  Losing it makes every stored message, memory, and token unrecoverable."
  if (-not $dry -and (G "NON_INTERACTIVE") -ne "1") {
    $rv = Read-Host "Reveal the token now? [y/N]"
    if ($rv -match '^[Yy]') { Out "  AETHER_TOKEN=$token" }
  }
  Out ""
  Out "Created or updated:"
  Out "  - Render web service '$name' (plan $plan, region $region)"
  Out "  - config delivered as AETHER_CONFIG_B64 (no secrets, not in the repo)"
  if ($uptimeMode -eq "api" -or $uptimeMode -eq "email") { Out "  - UptimeRobot monitor on $target (5-minute checks)" }
  elseif ($uptimeMode -eq "manual") { Out "  - manual UptimeRobot steps printed above" }
  else { Out "  - no uptime monitor" }
  Out ""
  Out "Skipped:"
  if (-not $services) { Out "  - optional services (none chosen)" }
  if (-not $apps) { Out "  - direct apps (none chosen)" }
  if (-not $channels) { Out "  - chat channels (none chosen)" }
  if ($uptimeMode -eq "skip") { Out "  - uptime monitoring" }
  Out ""
  Out "Plan limits to remember:"
  if ($plan -eq "free") {
    Out "  - Render free spins down after 15 idle minutes (the monitor prevents that),"
    Out "    is capped at 750 instance-hours/month, and has an ephemeral disk."
    Out "    The workspace files (IDENTITY.md, SOUL.md, MEMORY.md, daily notes) live"
    Out "    only on that disk, so they reset to defaults on any restart. Everything"
    Out "    in Postgres (chat, memory events, settings, tasks, approvals) survives."
  } else {
    Out "  - Paid plan: no spin-down and a 1 GB persistent disk keeps the workspace."
  }
  if ((G "DATABASE_MODE") -eq "render") { Out "  - Render free Postgres expires 30 days after creation; move to Neon before then." }
  if ((G "DATABASE_MODE") -eq "neon") { Out "  - Neon free: 0.5 GB storage and 100 compute-hours/month, no expiry." }
  Out ""
  Out "What monitoring does not do: it prevents idle spin-down and alerts on downtime."
  Out "It does not stop Render from restarting the service and it does not preserve"
  Out "files stored only on the instance disk."
  Out ""
  Out "Next three things to try:"
  Out "  1. Open the panel, Settings > Access, paste the token, send /status."
  Out "  2. Send /apps and connect one direct app."
  Out "  3. Ask a real question and watch the Audit Log and Decision Traces pages."

  if (-not $dry) {
    Write-Kv (Join-Path $StateDir "state.env") @{ SERVICE_ID = $sid; SERVICE_URL = $url; MONITOR_ID = $monitorId }
    $secPath = Join-Path $StateDir "secrets.env"
    $sec = @{ AETHER_ENCRYPTION_KEY = $enc; AETHER_TOKEN = $token; DATABASE_URL = $db }
    if ($tokKey) { $sec["AETHER_TOKEN_ENCRYPTION_KEY"] = $tokKey }
    if ($tokKeyId) { $sec["AETHER_TOKEN_ENCRYPTION_KEY_ID"] = $tokKeyId }
    Write-Kv $secPath $sec
    Protect-OwnerOnly $secPath
    if ((G "NON_INTERACTIVE") -ne "1") {
      $ans = Read-Host "Save an owner-only backup of the secrets to $env:USERPROFILE\aether-secrets.txt? [Y/n]"
      if ($ans -notmatch '^[Nn]') {
        $bp2 = Join-Path $env:USERPROFILE "aether-secrets.txt"
        $lines = @("# Aether deploy secrets. Keep this private.", "AETHER_ENCRYPTION_KEY=$enc", "AETHER_TOKEN=$token", "DATABASE_URL=$db", "SERVICE_URL=$url")
        Write-Utf8Lines $bp2 $lines
        Protect-OwnerOnly $bp2
        Out "[ ok ] wrote owner-only backup to $bp2"
      }
    }
    Out ""
    Out "Resume state lives in $StateDir (secrets.env is owner-only)."
  }
}
switch ($Command) {
  "catalog" { Show-Catalog $AllArgs[1]; exit 0 }
  "map" { Show-Map (Get-Arg "-Kind" ""); exit 0 }
  "hidden" {
    $p = if ($AllArgs.Count -gt 1) { $AllArgs[1] } else { "Secret" }
    $sec = Read-Host -Prompt $p -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
    exit 0
  }
  "randkey" { New-RandomKey; exit 0 }
  "randtoken" { New-RandomToken; exit 0 }
  "yearmonth" { Get-Date -Format "yyyy-MM"; exit 0 }
  "dump" {
    $names = Get-Arg "-Names" ""
    $out = Get-Arg "-Out" ""
    $lines = @()
    foreach ($n in (Get-Content -LiteralPath $names)) {
      $n = $n.Trim()
      if (-not $n) { continue }
      $v = [Environment]::GetEnvironmentVariable($n)
      if ($v) { $lines += "$n=$v" }
    }
    Write-Utf8Lines $out $lines
    exit 0
  }
  "status" {
    $u = Get-Arg "-Url" ""
    try {
      $r = Invoke-WebRequest -Method Head -UseBasicParsing -Uri $u -TimeoutSec 8
      [Console]::Out.WriteLine([int]$r.StatusCode)
    } catch {
      $code = 0
      try { $code = [int]$_.Exception.Response.StatusCode } catch { }
      if ($code -gt 0) { [Console]::Out.WriteLine($code) } else { [Console]::Out.WriteLine("000") }
    }
    exit 0
  }
  "run" { Invoke-Run; exit 0 }
  default {
    Write-Output "unknown helper command: $Command"
    exit 2
  }
}

<#ENDPS#>

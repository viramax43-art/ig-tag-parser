@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo === IG Tag Parser: build exe ===

if exist "%~dp0venv\Scripts\python.exe" (
  set "PY=%~dp0venv\Scripts\python.exe"
) else if exist "%~dp0..\venv\Scripts\python.exe" (
  set "PY=%~dp0..\venv\Scripts\python.exe"
) else (
  echo Creating venv...
  py -3 -m venv venv
  if errorlevel 1 (
    echo ERROR: failed to create venv
    exit /b 1
  )
  set "PY=%~dp0venv\Scripts\python.exe"
)

if not exist "%PY%" (
  echo ERROR: venv python not found
  exit /b 1
)

echo Building React UI...
pushd ui
call npm install
if errorlevel 1 (
  echo ERROR: npm install failed
  popd
  exit /b 1
)
call npm run build
if errorlevel 1 (
  echo ERROR: UI build failed
  popd
  exit /b 1
)
popd

echo Installing dependencies...
"%PY%" -m pip install -q -U pip
"%PY%" -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 (
  echo ERROR: pip install failed
  exit /b 1
)

echo Building...
"%PY%" -m PyInstaller --noconfirm --clean IGTagParser.spec
if errorlevel 1 (
  echo ERROR: PyInstaller failed
  exit /b 1
)

set "OUT=%~dp0dist\IGTagParser"
if not exist "%OUT%\data" mkdir "%OUT%\data"
if not exist "%OUT%\data\tags" mkdir "%OUT%\data\tags"
if not exist "%OUT%\data\exports" mkdir "%OUT%\data\exports"
if not exist "%OUT%\data\sessions" mkdir "%OUT%\data\sessions"

if exist "data\tags.txt" copy /Y "data\tags.txt" "%OUT%\data\tags.txt" >nul
if exist "data\accounts.txt" copy /Y "data\accounts.txt" "%OUT%\data\accounts.txt" >nul
if exist "data\accounts.txt.example" copy /Y "data\accounts.txt.example" "%OUT%\data\accounts.txt.example" >nul
if exist "data\req.sh" copy /Y "data\req.sh" "%OUT%\data\req.sh" >nul
if exist "proxies.txt" copy /Y "proxies.txt" "%OUT%\proxies.txt" >nul

if not exist "%OUT%\data\tags.txt" (
  >"%OUT%\data\tags.txt" echo // one tag per line
)
if not exist "%OUT%\data\accounts.txt" (
  >"%OUT%\data\accounts.txt" echo # username;password;2FA;ip:port:user:pass
)
if not exist "%OUT%\proxies.txt" (
  >"%OUT%\proxies.txt" echo # host:port:user:pass
)

echo.
echo Done: %OUT%\IGTagParser.exe
echo Keep the whole IGTagParser folder together with data\ and proxies.txt
echo.
explorer "%OUT%"
endlocal

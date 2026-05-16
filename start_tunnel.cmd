@echo off
REM Start cloudflared tunnel for ONA MCP server
REM This script is launched by Startup folder shortcut at system startup
REM IMPORTANT: Only targets the ONA tunnel (port 8000). Does NOT touch other cloudflared processes.

set CF_PATH=C:\Users\lkmot\cloudflared.exe
set LOG_PATH=C:\Users\lkmot\ona-mcp-server\tunnel-service.log
set ERR_PATH=C:\Users\lkmot\ona-mcp-server\tunnel-service-err.log
set ENV_PATH=C:\Users\lkmot\ona-mcp-server\.env

if exist "%ENV_PATH%" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_PATH%") do (
        if /I "%%A"=="NEON_DATABASE_URL" set "NEON_DATABASE_URL=%%B"
        if /I "%%A"=="DATABASE_URL" set "DATABASE_URL=%%B"
        if /I "%%A"=="DATABASE_URL" if "%NEON_DATABASE_URL%"=="" set "NEON_DATABASE_URL=%%B"
    )
)

REM Start MCP server if nothing is already listening on port 8000
netstat -ano 2>nul | findstr ":8000 " >nul
if errorlevel 1 (
    echo Starting ONA MCP server on port 8000...
    start "" /B C:\Python314\python.exe -m mcp_server.server 1>> "C:\Users\lkmot\ona-mcp-server\server.log" 2>> "C:\Users\lkmot\ona-mcp-server\server.err"
    timeout /t 8 /nobreak >nul
) else (
    echo MCP server already running on port 8000, skipping start.
)

REM Only kill existing cloudflared processes that target port 8000 (ONA tunnel)
REM Leaves factory-perplexity-mcp and other cloudflared tunnels untouched
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq cloudflared.exe" /fo table /nh ^| findstr "cloudflared"') do (
    wmic process where "processid=%%a" get commandline 2>nul | findstr "8000" >nul
    if not errorlevel 1 (
        echo Killing existing ONA tunnel (PID %%a)...
        taskkill /F /PID %%a 2>nul
    )
)

REM Wait a moment for network to be ready
timeout /t 10 /nobreak >nul

REM Start the tunnel
start "" /B "%CF_PATH%" tunnel --url http://localhost:8000 --no-autoupdate > "%LOG_PATH%" 2> "%ERR_PATH%"

REM Wait for tunnel to establish
timeout /t 15 /nobreak >nul

REM Update KV with new tunnel URL
python C:\Users\lkmot\ona-mcp-server\update_tunnel_url.py

REM Extra buffer for Worker KV cache consistency
timeout /t 8 /nobreak >nul

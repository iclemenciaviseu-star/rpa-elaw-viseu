@echo off
title ELAW RPA - Viseu Advogados (Local)
cd /d "%~dp0"

echo =========================================
echo   ELAW RPA - Execucao Local
echo   Interface: http://localhost:8000
echo =========================================
echo.

REM --- Detecta Python ---
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python

if not defined PY goto sem_python

echo Verificando Python...
%PY% --version
if errorlevel 1 goto python_quebrado
echo.

REM --- Primeira execucao: instala dependencias ---
if exist ".instalado_local" goto rodar

echo [1/3] Atualizando pip...
%PY% -m pip install --upgrade pip
if errorlevel 1 goto erro_pip
echo.

echo [2/3] Instalando dependencias...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto erro_deps
echo.

echo [3/3] Instalando navegador Chromium...
%PY% -m playwright install chromium
if errorlevel 1 goto erro_browser
echo.

echo Instalacao OK> .instalado_local
echo.
echo =========================================
echo   Instalacao concluida!
echo =========================================
echo.

:rodar
REM --- Configuracoes locais ---
REM   HEADLESS=1  -> Chromium invisivel (mais rapido, igual ao Render)
REM   HEADLESS=0  -> Chromium visivel na tela (para ver o robo funcionar)
set HEADLESS=1
set PORT=8000

echo Iniciando servidor em http://localhost:%PORT% ...
echo (mantenha esta janela aberta enquanto o RPA estiver rodando)
echo.

REM Abre o navegador automaticamente apos 3 segundos
start "" cmd /c "ping 127.0.0.1 -n 4 >nul && start http://localhost:%PORT%"

REM Inicia o servidor
%PY% server.py

echo.
echo Servidor encerrado.
goto fim

:sem_python
echo [ERRO] Python nao encontrado.
echo.
echo Baixe em https://www.python.org/downloads/
echo IMPORTANTE: marque "Add Python to PATH" na instalacao.
echo Depois reinicie o computador e rode esse arquivo de novo.
goto fim

:python_quebrado
echo [ERRO] Python esta no PATH mas nao funciona.
echo Instale o Python real em https://www.python.org/downloads/
goto fim

:erro_pip
echo [ERRO] Falha ao atualizar pip.
goto fim

:erro_deps
echo [ERRO] Falha ao instalar dependencias.
echo Verifique sua conexao com a internet.
goto fim

:erro_browser
echo [ERRO] Falha ao instalar o Chromium.
goto fim

:fim
echo.
echo ===== Pressione qualquer tecla para fechar =====
pause >nul

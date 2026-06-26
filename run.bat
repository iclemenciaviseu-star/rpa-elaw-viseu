@echo off
title ELAW RPA - Viseu Advogados
cd /d "%~dp0"

echo =========================================
echo   ELAW RPA - Automacao de Processos
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

REM --- Primeira execucao: instala ---
if exist ".instalado" goto rodar

echo [1/3] Atualizando pip...
%PY% -m pip install --upgrade pip
if errorlevel 1 goto erro_pip
echo.

echo [2/3] Instalando dependencias...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto erro_deps
echo.

echo [3/3] Instalando navegador (Chromium)...
%PY% -m playwright install chromium
if errorlevel 1 goto erro_browser
echo.

echo Instalacao OK> .instalado
echo.
echo =========================================
echo   Instalacao concluida!
echo =========================================
echo.

:rodar
echo Abrindo a janela do RPA...
echo (mantenha o CMD aberto enquanto a RPA roda)
echo.
%PY% main.py
echo.
echo RPA encerrada. Codigo de saida: %ERRORLEVEL%
goto fim

:sem_python
echo [ERRO] Python nao encontrado.
echo.
echo Baixe em https://www.python.org/downloads/
echo IMPORTANTE: marque "Add Python to PATH" durante a instalacao.
echo Depois reinicie o computador e rode esse arquivo de novo.
goto fim

:python_quebrado
echo [ERRO] Python esta no PATH mas nao funciona.
echo Provavelmente eh o stub da Microsoft Store.
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
echo [ERRO] Falha ao instalar Chromium.
goto fim

:fim
echo.
echo ===== Pressione qualquer tecla para fechar =====
pause >nul

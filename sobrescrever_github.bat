@echo off
chcp 65001 >nul
title Sobrescrever GitHub com esta versao - rpa-elaw-viseu
cd /d "%~dp0"

echo =========================================
echo   SOBRESCREVER GITHUB COM ESTA VERSAO
echo   Repositorio: rpa-elaw-viseu
echo =========================================
echo.
echo ATENCAO: Este script vai SOBRESCREVER completamente
echo o repositorio remoto no GitHub com os arquivos desta pasta.
echo Toda a historia anterior do GitHub sera apagada.
echo.
set /p RESP="Digite SOBRESCREVER para confirmar: "
if /i not "%RESP%"=="SOBRESCREVER" (
    echo Operacao cancelada.
    pause
    exit /b 0
)
echo.

REM --- Verifica se git esta instalado ---
where git >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Git nao encontrado no PATH.
    echo Instale o Git em https://git-scm.com/download/win
    pause
    exit /b 1
)

REM --- Limpa qualquer .git existente ---
if exist ".git" (
    echo Removendo .git antigo...
    attrib -h -s ".git" /s /d >nul 2>nul
    rmdir /s /q ".git"
)
echo.

echo [1/6] Inicializando repositorio Git...
git init -b main
if errorlevel 1 (
    echo [ERRO] Falha ao inicializar repositorio.
    pause
    exit /b 1
)
echo.

echo [2/6] Configurando remote origin...
git remote add origin https://github.com/iclemenciaviseu-star/rpa-elaw-viseu.git
echo.

echo [3/6] Configurando identidade local (apenas para este repo)...
git config user.email "iclemencia@viseu.com.br"
git config user.name "Ingrid Clemencia"
echo.

echo [4/6] Adicionando todos os arquivos (respeitando .gitignore)...
git add -A
echo.
echo Arquivos que serao commitados:
git status -s
echo.

echo [5/6] Criando commit inicial...
git commit -m "chore: reset repository to local version (rpa-elaw-viseu-main)"
if errorlevel 1 (
    echo [ERRO] Falha ao criar commit.
    pause
    exit /b 1
)
echo.

echo [6/6] Force push para o GitHub...
git push origin main --force
if errorlevel 1 (
    echo.
    echo [ERRO] Push falhou. Possiveis causas:
    echo - Credenciais do GitHub nao configuradas
    echo - Sem acesso ao repositorio
    echo.
    echo Se aparecer janela de login, autorize com sua conta GitHub.
    pause
    exit /b 1
)
echo.
echo =========================================
echo   GitHub sobrescrito com sucesso!
echo   https://github.com/iclemenciaviseu-star/rpa-elaw-viseu
echo =========================================
echo.
echo Estado atual:
git log --oneline -5
echo.
pause

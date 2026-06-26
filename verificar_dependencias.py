import sys
import subprocess
import importlib.util
import platform
import os

# Pacotes de requirements.txt + requirements-local.txt (sem duplicados)
REQUIRED_PACKAGES = [
    ("fastapi",     "fastapi>=0.110.0"),
    ("uvicorn",     "uvicorn[standard]>=0.27.0"),
    ("multipart",   "python-multipart>=0.0.9"),
    ("websockets",  "websockets>=12.0"),
    ("playwright",  "playwright>=1.45.0"),
    ("openpyxl",    "openpyxl>=3.1.2"),
    ("psutil",      "psutil>=5.9.0"),
    ("requests",    "requests>=2.31.0"),
    ("eel",         "eel>=0.16.0"),
]

MARKER_FILE = os.path.join(os.path.dirname(__file__), ".instalado_local")

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def print_header():
    print(f"\n{BOLD}{'='*55}")
    print("   VERIFICADOR DE DEPENDENCIAS - RPA eLaw Viseu")
    print(f"{'='*55}{RESET}\n")


def check_python_version():
    print(f"{BOLD}[PYTHON]{RESET}")
    v = sys.version_info
    print(f"  Sistema     : {platform.system()} {platform.release()}")
    print(f"  Versao      : Python {v.major}.{v.minor}.{v.micro}")
    print(f"  Executavel  : {sys.executable}")

    if v.major < 3 or (v.major == 3 and v.minor < 9):
        print(f"  {RED}AVISO: Recomendado Python 3.9 ou superior.{RESET}")
    else:
        print(f"  {GREEN}OK - versao compativel.{RESET}")
    print()


def upgrade_pip():
    print(f"{BOLD}[PIP]{RESET}")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"  {GREEN}pip atualizado.{RESET}")
    else:
        print(f"  {YELLOW}Nao foi possivel atualizar o pip (nao critico).{RESET}")
    print()


def is_installed(import_name):
    return importlib.util.find_spec(import_name) is not None


def install_package(pip_spec):
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", pip_spec, "--quiet"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def check_and_install_packages():
    print(f"{BOLD}[PACOTES]{RESET}")
    all_ok = True
    playwright_needs_browsers = False

    for import_name, pip_spec in REQUIRED_PACKAGES:
        if is_installed(import_name):
            print(f"  {GREEN}[OK]{RESET}     {pip_spec}")
        else:
            print(f"  {YELLOW}[FALTA]{RESET}  {pip_spec}  ->  a instalar...")
            all_ok = False
            ok = install_package(pip_spec)
            if ok:
                print(f"           {GREEN}Instalado com sucesso.{RESET}")
                if import_name == "playwright":
                    playwright_needs_browsers = True
            else:
                print(f"           {RED}ERRO ao instalar {pip_spec}.{RESET}")

    print()
    return playwright_needs_browsers


def install_playwright_browsers():
    print(f"{BOLD}[PLAYWRIGHT BROWSERS]{RESET}")
    print(f"  {YELLOW}A instalar navegadores do Playwright (pode demorar)...{RESET}")
    r = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"  {GREEN}Navegadores instalados com sucesso.{RESET}")
    else:
        print(f"  {RED}Falha ao instalar navegadores.{RESET}")
        if r.stderr:
            print(f"  {r.stderr.strip()}")
    print()


def write_marker():
    with open(MARKER_FILE, "w", encoding="utf-8") as f:
        f.write("Instalacao OK\n")
    print(f"  {GREEN}Ficheiro marcador criado: .instalado_local{RESET}")


def main():
    print_header()

    # Se o marcador ja existe, instalacao foi feita antes
    if os.path.exists(MARKER_FILE):
        print(f"{GREEN}Instalacao ja foi realizada anteriormente (.instalado_local encontrado).{RESET}")
        print(f"A verificar pacotes na mesma para garantir integridade...\n")

    check_python_version()
    upgrade_pip()
    playwright_new = check_and_install_packages()

    # Playwright browsers — instala sempre se o pacote acabou de ser instalado
    if playwright_new:
        install_playwright_browsers()
    else:
        # Garante que os browsers estao presentes mesmo que o pacote ja existisse
        r = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"{BOLD}[PLAYWRIGHT BROWSERS]{RESET}")
            print(f"  {RED}Problema ao verificar browsers do Playwright.{RESET}")
            print()

    # Cria/actualiza ficheiro marcador
    write_marker()

    print(f"\n{BOLD}{'='*55}")
    print("   VERIFICACAO CONCLUIDA")
    print(f"{'='*55}{RESET}")
    print(f"\n{GREEN}Tudo pronto! Pode iniciar o robo (python main.py).{RESET}\n")


if __name__ == "__main__":
    main()

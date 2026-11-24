"""Cria o .venv se faltar e abre um terminal com ele ativado."""
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"


def python_do_venv() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def garantir_venv() -> None:
    if not python_do_venv().exists():
        venv.create(VENV_DIR, with_pip=True)


def main() -> None:
    garantir_venv()
    if sys.platform == "win32":
        activate = VENV_DIR / "Scripts" / "activate.bat"
        subprocess.call(f'cmd /K "{activate}"', cwd=ROOT)
        return
    shell = os.environ.get("SHELL", "/bin/bash")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV_DIR)
    subprocess.call([shell, "-i"], cwd=ROOT, env=env)


if __name__ == "__main__":
    main()

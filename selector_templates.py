import argparse
import importlib
import sys
from pathlib import Path

import ronda_eliminatoria as ronda

search = importlib.import_module("seach template")


def choose_from_menu() -> list[str]:
    print("Selector de templates\n")
    print("   1. Buscar y revisar templates")
    print("   2. Ronda eliminatoria\n")
    while (choice := input("Elige una opción (1/2): ").strip()) not in ("1", "2"):
        pass
    if choice == "2":
        return ["ronda"]
    folder = input(f"Carpeta a revisar [{search.DEFAULT_BASE_PATH}]: ").strip().strip('"')
    return ["buscar", folder] if folder else ["buscar"]


def pause_before_exit() -> None:
    import msvcrt

    # Descarta lo tecleado en la consola durante la revisión para que no cierre la ventana.
    while msvcrt.kbhit():
        msvcrt.getwch()
    print("\nPulsa ESC para salir...")
    while msvcrt.getwch() != "\x1b":
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="selector_templates",
        description="Busca templates HTML y los elige en rondas eliminatorias. Sin argumentos muestra un menú.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    buscar = commands.add_parser("buscar", help="Revisa y selecciona archivos HTML en Chrome.")
    buscar.add_argument(
        "base_dir",
        nargs="?",
        type=Path,
        default=search.DEFAULT_BASE_PATH,
        help=f"Carpeta a revisar (predeterminada: {search.DEFAULT_BASE_PATH})",
    )
    commands.add_parser("ronda", help="Rondas eliminatorias sobre seleccionados.txt.")

    from_menu = len(sys.argv) == 1
    args = parser.parse_args(choose_from_menu() if from_menu else None)
    exit_code = search.main(args.base_dir) if args.command == "buscar" else ronda.main()
    if from_menu:
        pause_before_exit()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

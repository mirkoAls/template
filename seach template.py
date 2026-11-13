import argparse
import ctypes
import os
import queue
import shutil
import sys
from ctypes import wintypes
from pathlib import Path
from subprocess import DEVNULL, Popen
from time import monotonic, sleep
from pynput import keyboard
from pynput.keyboard import Key

CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
CHROME_PROCESS_NAME = "chrome.exe"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010

LOAD_WAIT = 0.5
WINDOW_DETECT_TIMEOUT = 3.0
KEY_REPEAT_WINDOW = 1.5
SPECIAL_KEYS = {Key.left: "back", Key.esc: "esc"}
DECISION_KEYS = frozenset({"y", "n", "back", "esc"})
EXIT_KEYS = frozenset({"g", "f", "esc"})

ENTRY_FILE_PRIORITY = ("index.html", "home.html", "default.html", "main.html")
ENTRY_FILES = frozenset(ENTRY_FILE_PRIORITY)
ENTRY_FILE_RANK = {name: position for position, name in enumerate(ENTRY_FILE_PRIORITY)}

SKIP_FOLDERS = {
    "doc", "docs", "documentation", "help-documentation", "examples", "demo",
    "node_modules", "dist", "build", "vendor", "plugins",
    "assets", "images", "fonts", "css", "js", "scss", "sass",
    "src", "layout",
    ".git",
    "bootstrap-icons"
}

DEFAULT_BASE_PATH = Path(r"D:\Work web-Plantillas\plantilla")
PROGRESS_FILE = Path(".ultimo_template")
OUTPUT_FILE = Path("seleccionados.txt")


def find_chrome() -> Path | None:
    if chrome_path := shutil.which("chrome"):
        return Path(chrome_path)
    for raw_path in CHROME_PATHS:
        candidate = Path(os.path.expandvars(raw_path))
        if candidate.is_file():
            return candidate
    return None


def normalize_file_url(filepath: Path) -> str:
    return filepath.resolve().as_uri()


def should_skip_directory(directory_name: str) -> bool:
    return directory_name.casefold() in SKIP_FOLDERS


def get_html_candidates(base_dir: Path) -> list[Path]:
    candidates = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [directory for directory in dirs if not should_skip_directory(directory)]
        matched_files = [f for f in files if f.lower() in ENTRY_FILES]
        if matched_files:
            best_match = min(
                matched_files,
                key=lambda filename: ENTRY_FILE_RANK[filename.casefold()],
            )
            candidates.append(Path(root) / best_match)
    return sorted(candidates)


def load_template_paths(filepath: Path) -> list[Path]:
    if not filepath.is_file():
        return []
    paths = []
    for line in filepath.read_text(encoding="utf-8").splitlines():
        candidate = Path(line.strip())
        if line.strip() and candidate.is_file() and candidate not in paths:
            paths.append(candidate)
    return paths


def save_template_paths(filepath: Path, paths: list[Path]) -> None:
    filepath.write_text(
        "\n".join(str(path) for path in paths) + ("\n" if paths else ""),
        encoding="utf-8",
    )


def load_progress_path() -> Path | None:
    if not PROGRESS_FILE.is_file():
        return None
    saved_path = PROGRESS_FILE.read_text(encoding="utf-8").strip()
    candidate = Path(saved_path) if saved_path else None
    return candidate if candidate and candidate.is_file() else None


def update_selection(selected_templates: list[Path], path: Path, decision: str) -> None:
    if decision == "y" and path not in selected_templates:
        selected_templates.append(path)
    elif decision == "n" and path in selected_templates:
        selected_templates.remove(path)


def save_progress(selected_templates: list[Path], current_path: Path) -> None:
    save_template_paths(OUTPUT_FILE, selected_templates)
    PROGRESS_FILE.write_text(str(current_path), encoding="utf-8")


class KeyReader:
    """Escucha el teclado durante toda la sesión e ignora la autorrepetición de una tecla mantenida."""

    def __init__(self):
        self._pressed: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._last_press: dict[object, float] = {}
        self._listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)

    def __enter__(self):
        self._listener.start()
        return self

    def __exit__(self, *exc_info):
        self._listener.stop()

    def on_press(self, key) -> None:
        key_id = getattr(key, "vk", None) or key
        now = monotonic()
        previous = self._last_press.get(key_id)
        self._last_press[key_id] = now
        # El margen evita que una tecla quede bloqueada si se pierde su evento de soltar.
        if previous is not None and now - previous < KEY_REPEAT_WINDOW:
            return
        self._pressed.put(SPECIAL_KEYS.get(key) or (getattr(key, "char", None) or "").lower())

    def on_release(self, key) -> None:
        self._last_press.pop(getattr(key, "vk", None) or key, None)

    def clear(self) -> None:
        while not self._pressed.empty():
            self._pressed.get_nowait()

    def read(self, valid_keys=None) -> str:
        while True:
            try:
                name = self._pressed.get(timeout=0.05)
            except queue.Empty:
                continue
            if valid_keys is None or name in valid_keys:
                return name


def read_decision(keys: KeyReader) -> str:
    """Devuelve y/n/back, o g/f solo si antes se confirmó con ESC."""
    while True:
        decision = keys.read(DECISION_KEYS)
        if decision != "esc":
            return decision
        print("⏸️ G → guardar y salir · F → finalizar · ESC → seguir revisando")
        decision = keys.read(EXIT_KEYS)
        if decision != "esc":
            return decision
        print("▶️ Sigues revisando. Esperando decisión... (Y/N/←/ESC)")


class ChromeViewer:
    """Abre cada template en una ventana de Chrome y cierra la del template anterior."""

    def __init__(self, chrome_exe: Path, load_wait: float):
        self.chrome_exe = chrome_exe
        self.load_wait = load_wait
        self.windows: set[int] = set()
        self.user32 = ctypes.WinDLL("user32")
        self.kernel32 = ctypes.WinDLL("kernel32")
        self.kernel32.OpenProcess.restype = wintypes.HANDLE

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _is_chrome_process(self, pid: int) -> bool:
        process = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        image = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(image))
        found = self.kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(process), 0, image, ctypes.byref(size))
        self.kernel32.CloseHandle(wintypes.HANDLE(process))
        return bool(found) and Path(image.value).name.casefold() == CHROME_PROCESS_NAME

    def _chrome_windows(self) -> set[int]:
        handles: set[int] = set()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd, _lparam):
            window = wintypes.HWND(hwnd)
            # Los tooltips y menús de Chrome también son ventanas, pero sin título.
            if self.user32.IsWindowVisible(window) and self.user32.GetWindowTextLengthW(window):
                pid = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
                if self._is_chrome_process(pid.value):
                    handles.add(hwnd)
            return True

        self.user32.EnumWindows(collect, 0)
        return handles

    def _close(self, handles: set[int]) -> None:
        for hwnd in handles:
            self.user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)

    def show(self, template: Path) -> None:
        before = self._chrome_windows()
        Popen(
            [str(self.chrome_exe), "--incognito", "--new-window", normalize_file_url(template)],
            stdout=DEVNULL,
            stderr=DEVNULL,
        )
        previous, self.windows = self.windows, set()
        started = monotonic()
        while not self.windows and monotonic() - started < WINDOW_DETECT_TIMEOUT:
            sleep(0.05)
            self.windows = self._chrome_windows() - before
        # Se cierra la anterior solo cuando la nueva ya existe, para que Chrome no llegue a salir.
        self._close(previous)
        sleep(max(0.0, self.load_wait - (monotonic() - started)))

    def close(self) -> None:
        self._close(self.windows)
        self.windows = set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Revisa y selecciona archivos HTML en Chrome.")
    parser.add_argument(
        "base_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_BASE_PATH,
        help=f"Carpeta a revisar (predeterminada: {DEFAULT_BASE_PATH})",
    )
    return parser.parse_args()


def main(base_path: Path) -> int:
    if sys.platform != "win32":
        print("❌ Este script está diseñado solo para Windows.")
        return 1

    if not base_path.is_dir():
        print(f"❌ La ruta base no existe: {base_path}")
        return 1

    chrome_exe = find_chrome()
    if not chrome_exe:
        print("⚠️ Google Chrome no encontrado en las rutas habituales.")
        return 1

    start_from = load_progress_path()
    selected_templates = load_template_paths(OUTPUT_FILE) if start_from else []

    print("🟢 Controles:")
    print("   Y → Aprobar template")
    print("   N → Rechazar")
    print("   ← (flecha izquierda) → Volver al template anterior")
    print("   ESC → Salir o finalizar (después G guarda y sale, F finaliza)\n")

    all_candidates = get_html_candidates(base_path)
    if not all_candidates:
        print("❌ No se encontraron templates válidos.")
        return 1

    start_index = 0
    if start_from:
        try:
            start_index = all_candidates.index(start_from)
        except ValueError:
            print("⚠️ El punto de inicio guardado ya no existe. Comenzando desde el principio.")

    current_index = start_index

    try:
        with KeyReader() as keys, ChromeViewer(chrome_exe, LOAD_WAIT) as viewer:
            while current_index < len(all_candidates):
                current_path = all_candidates[current_index]

                print(f"\n📁 ({current_index - start_index + 1}/{len(all_candidates) - start_index}) Abriendo: {current_path}")

                viewer.show(current_path)
                keys.clear()

                print("Esperando decisión... (Y/N/←/ESC)")

                decision = read_decision(keys)

                if decision == "f":
                    print("\n⏹️ Finalización anticipada por el usuario.")
                    break

                elif decision == "g":
                    save_progress(selected_templates, current_path)
                    print("💾 Progreso y selección guardados.")
                    return 0

                elif decision == "y":
                    update_selection(selected_templates, current_path, decision)
                    print("✅ Añadido.")
                    current_index += 1

                elif decision == "n":
                    update_selection(selected_templates, current_path, decision)
                    print("❌ Descartado.")
                    current_index += 1

                elif decision == "back":
                    if current_index > start_index:
                        print("↩️ Volviendo al template anterior...")
                        current_index -= 1
                    else:
                        print("⚠️ Ya estás en el primer template.")
    except KeyboardInterrupt:
        if current_index < len(all_candidates):
            save_progress(selected_templates, all_candidates[current_index])
            print("\n💾 Interrumpido: progreso y selección guardados.")
            return 130

    save_template_paths(OUTPUT_FILE, selected_templates)
    PROGRESS_FILE.unlink(missing_ok=True)

    print(f"\n✔️ Proceso terminado. {len(selected_templates)} templates seleccionados.")
    print(f"📄 Lista guardada en '{OUTPUT_FILE}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args().base_dir))

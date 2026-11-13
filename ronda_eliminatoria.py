#!/usr/bin/env python3

import ctypes
import json
import logging
import os
import queue
import shutil
import sys
import time
from ctypes import wintypes
from pathlib import Path
from subprocess import Popen, DEVNULL
from typing import Final

from pynput import keyboard
from pynput.keyboard import Key

CHROME_CANDIDATES: Final[tuple[str, ...]] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
CHROME_PROCESS_NAME: Final[str] = "chrome.exe"
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
WM_CLOSE: Final[int] = 0x0010

INPUT_FILE: Final[Path] = Path("seleccionados.txt")
OUTPUT_FILE: Final[Path] = Path("resultado_final.txt")
STATE_FILE: Final[Path] = Path(".ronda_en_curso.json")
CHROME_LOAD_TIMEOUT: Final[float] = 1.5
WINDOW_DETECT_TIMEOUT: Final[float] = 3.0
KEYBOARD_POLL_INTERVAL: Final[float] = 0.05
KEY_REPEAT_WINDOW: Final[float] = 1.5
KEY_BACK: Final[str] = "back"
KEY_ESC: Final[str] = "esc"
SPECIAL_KEYS: Final[dict[Key, str]] = {Key.left: KEY_BACK, Key.esc: KEY_ESC}
DECISION_KEYS: Final[set[str]] = {"y", "n", "b", KEY_BACK, KEY_ESC}
EXIT_KEYS: Final[set[str]] = {"g", "f", KEY_ESC}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def find_chrome_executable() -> Path:
    if chrome_path := shutil.which("chrome"):
        return Path(chrome_path)
    for candidate in CHROME_CANDIDATES:
        expanded = Path(os.path.expandvars(candidate)).expanduser()
        if expanded.is_file():
            return expanded
    raise FileNotFoundError("Google Chrome no encontrado.")


def normalize_file_url(filepath: Path) -> str:
    return filepath.resolve().as_uri()


def load_template_paths(filepath: Path) -> list[Path]:
    if not filepath.is_file():
        return []
    valid_paths = []
    with filepath.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped == "-":
                continue
            candidate = Path(stripped)
            if candidate.is_file():
                valid_paths.append(candidate)
    return valid_paths


def save_selection_history(filepath: Path, history: list[list[Path]], current_selection: list[Path]) -> None:
    groups = [round_selections for round_selections in history if round_selections]
    if current_selection:
        groups.append(current_selection)
    lines = []
    for group_index, round_selections in enumerate(groups):
        lines.extend(str(p) for p in round_selections)
        if group_index < len(groups) - 1:
            lines.append("-")
    filepath.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def round_participants(candidates: list[Path], history: list[list[Path]]) -> list[Path]:
    return history[-1] if history else candidates


def selected_templates(participants: list[Path], decisions: list[str]) -> list[Path]:
    return [template for template, decision in zip(participants, decisions) if decision == "y"]


def save_state(filepath: Path, candidates: list[Path], history: list[list[Path]], decisions: list[str]) -> None:
    state = {
        "candidates": [str(p) for p in candidates],
        "history": [[str(p) for p in round_selections] for round_selections in history],
        "decisions": decisions,
    }
    filepath.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(filepath: Path, candidates: list[Path]) -> tuple[list[list[Path]], list[str]] | None:
    """Devuelve (historial, decisiones de la ronda en curso) si el progreso guardado es de estos candidatos."""
    try:
        state = json.loads(filepath.read_text(encoding="utf-8"))
        if state["candidates"] != [str(p) for p in candidates]:
            return None
        history = [[Path(p) for p in round_selections] for round_selections in state["history"]]
        decisions = list(state["decisions"])
        if (
            any(len(round_selections) < 2 for round_selections in history)
            or len(decisions) > len(round_participants(candidates, history))
            or set(decisions) - {"y", "n"}
        ):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return history, decisions


def save_progress(candidates: list[Path], history: list[list[Path]], decisions: list[str]) -> None:
    partial_selection = selected_templates(round_participants(candidates, history), decisions)
    save_selection_history(OUTPUT_FILE, history, partial_selection)
    save_state(STATE_FILE, candidates, history, decisions)


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
        now = time.monotonic()
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

    def read(self, valid_keys: set[str] | None = None) -> str:
        while True:
            try:
                name = self._pressed.get(timeout=KEYBOARD_POLL_INTERVAL)
            except queue.Empty:
                continue
            if valid_keys is None or name in valid_keys:
                return name


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
        started = time.monotonic()
        while not self.windows and time.monotonic() - started < WINDOW_DETECT_TIMEOUT:
            time.sleep(0.05)
            self.windows = self._chrome_windows() - before
        # Se cierra la anterior solo cuando la nueva ya existe, para que Chrome no llegue a salir.
        self._close(previous)
        time.sleep(max(0.0, self.load_wait - (time.monotonic() - started)))

    def close(self) -> None:
        self._close(self.windows)
        self.windows = set()


def process_single_template(template_path: Path, viewer: ChromeViewer, keys: KeyReader) -> str:
    logger.info(f"[{template_path.name}] Abriendo en Chrome...")
    viewer.show(template_path)
    keys.clear()
    while True:
        print("   👉 Esperando decisión (Y/N/B/←/ESC)...", end="\r", flush=True)
        decision = keys.read(DECISION_KEYS)
        if decision == KEY_ESC:
            print("   ⏸️ G = guardar y salir · F = finalizar ronda · ESC = seguir", end="\r", flush=True)
            decision = keys.read(EXIT_KEYS)
        print(" " * 70, end="\r", flush=True)
        if decision != KEY_ESC:
            return KEY_BACK if decision == "b" else decision


def run_selection_round(
    candidates: list[Path], decisions: list[str], viewer: ChromeViewer, keys: KeyReader, round_num: int
) -> str:
    """Completa `decisions` (una por template, en orden) y devuelve "completed" o "save_exit"."""
    logger.info(f"🔄 RONDA {round_num} | Participantes: {len(candidates)}")
    while len(decisions) < len(candidates):
        decision = process_single_template(candidates[len(decisions)], viewer, keys)
        if decision == KEY_BACK:
            if decisions:
                logger.info("↩️ Retrocediendo...")
                decisions.pop()
                logger.info(f"   🔄 Re-evaluando: {candidates[len(decisions)].name}")
            else:
                logger.info("   ⚠️ Primer elemento, no hay hacia dónde retroceder")
            continue
        if decision == "g":
            logger.info(f"💾 Guardando progreso ({decisions.count('y')} seleccionados)")
            return "save_exit"
        if decision == "f":
            logger.info(f"⏹️ Ronda {round_num} finalizada manualmente")
            break
        decisions.append(decision)
        if decision == "y":
            logger.info(f"   ✅ Mantenido ({decisions.count('y')} seleccionados)")
        else:
            logger.info("   ❌ Descartado")
    return "completed"


def wait_any_key(keys: KeyReader, prompt: str = "\nPresiona cualquier tecla para continuar...") -> None:
    print(prompt, end="", flush=True)
    keys.read()
    print("\r" + " " * 60 + "\r", end="", flush=True)


def display_controls() -> None:
    print("\n🟢 CONTROLES DISPONIBLES:")
    controls = [
        ("Y", "✅ MANTENER: Pasa a la siguiente ronda"),
        ("N", "❌ DESCARTAR: Elimina definitivamente"),
        ("← / B", "↩️ VOLVER: Retrocede al template anterior"),
        ("ESC", "⏸️ SALIR O FINALIZAR: después G guarda y sale, F cierra la ronda con los elegidos"),
    ]
    for key, desc in controls:
        print(f"   [{key:>6}] → {desc}")
    print(f"\n📝 Historial guardado en: '{OUTPUT_FILE}' (separador: '-')")
    print("-" * 70)


def main() -> int:
    if sys.platform != "win32":
        logger.error("❌ Este script está diseñado exclusivamente para Windows.")
        return 1
    candidates = load_template_paths(INPUT_FILE)
    if not candidates:
        logger.error(f"❌ No se encontraron templates válidos en '{INPUT_FILE}'")
        logger.info("💡 Ejecuta primero el script de búsqueda de templates.")
        return 1
    if len(candidates) == 1:
        save_selection_history(OUTPUT_FILE, [], candidates)
        logger.info(f"🏆 Template ganador: {candidates[0]}")
        return 0
    try:
        chrome_exe = find_chrome_executable()
        logger.info(f"✓ Chrome encontrado: {chrome_exe}")
    except FileNotFoundError as e:
        logger.error(f"⚠️ {e}")
        return 1
    print(f"\n🚀 Iniciando proceso de selección | {len(candidates)} templates cargados")
    display_controls()
    selection_history, decisions = load_state(STATE_FILE, candidates) or ([], [])
    current_round = round_participants(candidates, selection_history)
    if selection_history or decisions:
        logger.info(
            f"↪️ Reanudando la ronda {len(selection_history) + 1} "
            f"desde el template {len(decisions) + 1} de {len(current_round)}"
        )
    try:
        with KeyReader() as keys, ChromeViewer(chrome_exe, CHROME_LOAD_TIMEOUT) as viewer:
            while len(current_round) > 1:
                round_num = len(selection_history) + 1
                exit_reason = run_selection_round(current_round, decisions, viewer, keys, round_num)
                if exit_reason == "save_exit":
                    save_progress(candidates, selection_history, decisions)
                    return 0
                selected = selected_templates(current_round, decisions)
                decisions.clear()
                if not selected:
                    logger.warning("⚠️ No se seleccionó ningún template en esta ronda.")
                    print("   ¿Repetir ronda con los mismos elementos? (S = repetir, ESC = cancelar): ", end="", flush=True)
                    retry = keys.read({"s", KEY_ESC})
                    print()
                    if retry != "s":
                        STATE_FILE.unlink(missing_ok=True)
                        logger.error("❌ Proceso cancelado por falta de selecciones.")
                        return 1
                    continue
                selection_history.append(selected)
                save_selection_history(OUTPUT_FILE, selection_history, [])
                logger.info(f"📊 Ronda {round_num}: {len(selected)} templates avanzan")
                current_round = selected
                if len(current_round) > 1:
                    wait_any_key(keys, f"\nPresiona cualquier tecla para comenzar la Ronda {round_num + 1}...")
    except KeyboardInterrupt:
        logger.info("\n⚠️ Proceso interrumpido por el usuario.")
        save_progress(candidates, selection_history, decisions)
        return 130
    except Exception as e:
        logger.exception(f"❌ Error inesperado: {e}")
        save_progress(candidates, selection_history, decisions)
        return 1
    STATE_FILE.unlink(missing_ok=True)
    logger.info("\n✔️ Proceso finalizado correctamente.")
    logger.info(f"📄 Historial guardado en: '{OUTPUT_FILE}'")
    logger.info(f"🏆 Template ganador: {current_round[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

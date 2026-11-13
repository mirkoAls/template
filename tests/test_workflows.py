from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEARCH = load_module("seach template.py", "template_search")
ROUND = load_module("ronda_eliminatoria.py", "elimination_round")


def key_event(char: str, vk: int):
    return type("KeyEvent", (), {"char": char, "vk": vk})()


class ScriptedKeys:
    def __init__(self, *keys: str):
        self.keys = list(keys)

    def clear(self):
        pass

    def read(self, valid_keys=None):
        while True:
            key = self.keys.pop(0)
            if valid_keys is None or key in valid_keys:
                return key


class TemplateSearchTests(unittest.TestCase):
    def test_candidate_discovery_prunes_folders_and_uses_fixed_priority(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "template").mkdir()
            (root / "template" / "home.html").write_text("", encoding="utf-8")
            (root / "template" / "index.html").write_text("", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "index.html").write_text("", encoding="utf-8")

            self.assertEqual(
                SEARCH.get_html_candidates(root),
                [root / "template" / "index.html"],
            )

    def test_selection_can_be_changed_without_duplicates(self):
        path = Path("template/index.html")
        selected = []

        SEARCH.update_selection(selected, path, "y")
        SEARCH.update_selection(selected, path, "y")
        SEARCH.update_selection(selected, path, "n")

        self.assertEqual(selected, [])

    def test_progress_loads_the_saved_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selected = root / "index.html"
            selected.write_text("", encoding="utf-8")
            progress = root / ".ultimo_template"
            progress.write_text(str(selected), encoding="utf-8")

            with mock.patch.object(SEARCH, "PROGRESS_FILE", progress):
                self.assertEqual(SEARCH.load_progress_path(), selected)

    def test_file_url_escapes_special_characters(self):
        self.assertEqual(
            SEARCH.normalize_file_url(Path("C:/plantillas/a b#1/index.html")),
            "file:///C:/plantillas/a%20b%231/index.html",
        )

    def test_viewer_closes_previous_window_once_the_next_one_appears(self):
        viewer = SEARCH.ChromeViewer(Path("chrome.exe"), load_wait=0)
        viewer.user32 = mock.Mock()

        with (
            mock.patch.object(SEARCH, "Popen"),
            mock.patch.object(viewer, "_chrome_windows", side_effect=[{1}, {1, 2}, {1, 2}, {1, 2, 3}]),
        ):
            viewer.show(Path("first/index.html"))
            self.assertEqual(viewer.windows, {2})
            viewer.user32.PostMessageW.assert_not_called()

            viewer.show(Path("second/index.html"))
            self.assertEqual(viewer.windows, {3})

        closed = [call.args[0].value for call in viewer.user32.PostMessageW.call_args_list]
        self.assertEqual(closed, [2])

    def test_exit_keys_only_work_after_escape(self):
        keys = ScriptedKeys("g", "f", "esc", "esc", "esc", "g")

        self.assertEqual(SEARCH.read_decision(keys), "g")
        self.assertEqual(keys.keys, [])


class EliminationRoundTests(unittest.TestCase):
    def test_key_reader_skips_keys_that_are_not_valid(self):
        reader = ROUND.KeyReader()
        reader.on_press(key_event("x", 88))
        reader.on_press(key_event("S", 83))

        self.assertEqual(reader.read({"s", "n"}), "s")

    def test_key_reader_ignores_auto_repeat_until_release(self):
        reader = ROUND.KeyReader()
        held = key_event("y", 89)
        reader.on_press(held)
        reader.on_press(held)
        reader.on_press(ROUND.Key.left)
        reader.on_release(held)
        reader.on_press(held)
        reader.on_press(ROUND.Key.esc)

        self.assertEqual([reader.read() for _ in range(4)], ["y", "back", "y", "esc"])

    def test_round_can_be_saved_and_resumed(self):
        candidates = [Path("a.html"), Path("b.html"), Path("c.html")]
        decisions = []
        viewer = mock.Mock()

        keys = ScriptedKeys("g", "f", "y", "y", "back", "n", "esc", "esc", "esc", "g")
        reason = ROUND.run_selection_round(candidates, decisions, viewer, keys, 1)
        self.assertEqual((reason, decisions), ("save_exit", ["y", "n"]))

        reason = ROUND.run_selection_round(candidates, decisions, viewer, ScriptedKeys("y"), 1)
        self.assertEqual((reason, decisions), ("completed", ["y", "n", "y"]))
        self.assertEqual(viewer.show.call_args.args[0], candidates[2])
        self.assertEqual(ROUND.selected_templates(candidates, decisions), [candidates[0], candidates[2]])

    def test_state_is_restored_only_for_the_same_candidates(self):
        candidates = [Path("a.html"), Path("b.html"), Path("c.html")]
        history = [[Path("a.html"), Path("c.html")]]

        with tempfile.TemporaryDirectory() as temporary_directory:
            state = Path(temporary_directory) / "state.json"
            ROUND.save_state(state, candidates, history, ["y"])

            self.assertEqual(ROUND.load_state(state, candidates), (history, ["y"]))
            self.assertIsNone(ROUND.load_state(state, candidates[:2]))

            state.write_text("{", encoding="utf-8")
            self.assertIsNone(ROUND.load_state(state, candidates))

    def test_history_has_one_separator_between_rounds(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "history.txt"
            ROUND.save_selection_history(
                output,
                [[Path("first.html"), Path("second.html")]],
                [Path("winner.html")],
            )

            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "first.html\nsecond.html\n-\nwinner.html\n",
            )

    def test_chrome_path_expands_localappdata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            local_app_data = Path(temporary_directory)
            expected = local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe"
            candidates = (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",)

            with (
                mock.patch.object(ROUND, "CHROME_CANDIDATES", candidates),
                mock.patch.object(ROUND.shutil, "which", return_value=None),
                mock.patch.dict(ROUND.os.environ, {"LOCALAPPDATA": str(local_app_data)}),
                mock.patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == expected),
            ):
                self.assertEqual(ROUND.find_chrome_executable(), expected)


if __name__ == "__main__":
    unittest.main()

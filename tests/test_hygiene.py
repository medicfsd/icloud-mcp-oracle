"""Keep secrets and long digit runs out of this tree. A secret scan blocks the auto-push."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGIT_RUN = re.compile(r"\d{13,}")
BAD_NAME = re.compile(r"token|secret|password", re.I)


class HygieneTest(unittest.TestCase):
    def test_tree_has_no_long_digit_runs_or_secret_filenames(self):
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [name for name in dirnames if name not in {".venv", "__pycache__"}]
            for name in filenames:
                self.assertIsNone(BAD_NAME.search(name), name)
                path = os.path.join(dirpath, name)
                if name.endswith((".pyc", ".tar")):
                    continue
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
                match = DIGIT_RUN.search(text)
                self.assertIsNone(match, f"{path} contains a {match.group(0) if match else ''} digit run")


if __name__ == "__main__":
    unittest.main()

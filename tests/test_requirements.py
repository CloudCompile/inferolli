"""requirements.txt — the image must ship uvicorn's WS transport (noVNC)."""

import unittest
from pathlib import Path

REQ = Path(__file__).parent.parent / "requirements.txt"


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.lines = [ln.strip() for ln in REQ.read_text().splitlines()]

    def _pkgs(self):
        return [ln.split("=")[0].split(">")[0].split("<")[0].strip().lower()
                for ln in self.lines if ln and not ln.startswith("#")]

    def test_websockets_transport_present(self):
        # without it uvicorn answers the /websockify upgrade with 426 and
        # noVNC shows "failed to connect to server"
        self.assertIn("websockets", self._pkgs())

    def test_uvicorn_present(self):
        self.assertIn("uvicorn", self._pkgs())

    def test_fastapi_present(self):
        self.assertIn("fastapi", self._pkgs())


if __name__ == "__main__":
    unittest.main()

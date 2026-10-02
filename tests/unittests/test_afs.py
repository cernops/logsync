"""Linux keyring integration."""

# Test setup is intentionally repeated across thematic modules.
# pylint: disable=duplicate-code

import os
import unittest
import subprocess

from logsync.afs import HTCondorKeyringCache

class AfsTests(unittest.TestCase):
    def test_uses_synthetic_htcondor_keyring(self) -> None:
        """Link a synthetic user anchor into its session and remove it by selecting the neutral session."""

        uid = os.getuid()
        description = f"htcondor_uid{uid}"
        serial = subprocess.run(
            ["keyctl", "newring", description, "@s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        try:
            subprocess.run(
                ["keyctl", "setperm", serial, "0x3f3f0000"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["keyctl", "link", serial, "@u"],
                check=True,
                capture_output=True,
            )
            cache = HTCondorKeyringCache()
            cache.use(uid)
            self.assertEqual(
                subprocess.run(
                    ["keyctl", "search", "@s", "keyring", description],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                serial,
            )
            cache.use_neutral()
            self.assertNotEqual(
                subprocess.run(
                    ["keyctl", "search", "@s", "keyring", description],
                    check=False,
                    capture_output=True,
                ).returncode,
                0,
            )
        finally:
            subprocess.run(
                ["keyctl", "unlink", serial, "@s"],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["keyctl", "unlink", serial, "@u"],
                check=False,
                capture_output=True,
            )

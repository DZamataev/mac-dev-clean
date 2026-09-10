from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "mac_dev_clean"
PROJECT_SPEC = REPOSITORY_ROOT / "macos" / "project.yml"
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts" / "build_macos_app.sh"


class MacOSProjectManifestTests(unittest.TestCase):
    def test_every_python_module_is_an_xcode_input_and_output(self) -> None:
        spec = PROJECT_SPEC.read_text(encoding="utf-8")
        modules = sorted(path.relative_to(PACKAGE_ROOT).as_posix() for path in PACKAGE_ROOT.rglob("*.py"))

        for module in modules:
            with self.subTest(module=module, manifest="input"):
                source = "$(SRCROOT)/../src/mac_dev_clean/{0}".format(module)
                self.assertEqual(spec.count(source), 1)
            with self.subTest(module=module, manifest="output"):
                output = (
                    "$(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)"
                    "/python/mac_dev_clean/{0}"
                ).format(module)
                self.assertEqual(spec.count(output), 1)

    def test_xcode_copy_phase_preserves_the_tools_package(self) -> None:
        spec = PROJECT_SPEC.read_text(encoding="utf-8")

        self.assertIn('/bin/mkdir -p "${ENGINE_DEST}/tools"', spec)
        self.assertIn('/bin/cp "${ENGINE_SOURCE}"/tools/*.py "${ENGINE_DEST}/tools/"', spec)

    def test_release_packaging_preserves_the_tools_package(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('"$RESOURCES/python/mac_dev_clean/tools"', script)
        self.assertIn(
            'cp "$ROOT"/src/mac_dev_clean/tools/*.py '
            '"$RESOURCES/python/mac_dev_clean/tools/"',
            script,
        )


if __name__ == "__main__":
    unittest.main()

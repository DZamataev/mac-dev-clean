from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "mac_dev_clean"
PROJECT_SPEC = REPOSITORY_ROOT / "macos" / "project.yml"
PROJECT_FILE = REPOSITORY_ROOT / "macos" / "MacDevClean.xcodeproj" / "project.pbxproj"
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

    def test_xcode_copy_phase_replaces_the_controlled_python_package(self) -> None:
        spec = PROJECT_SPEC.read_text(encoding="utf-8")

        self.assertIn('case "${ENGINE_DEST}" in', spec)
        self.assertIn('"${TARGET_BUILD_DIR}"/*/python/mac_dev_clean)', spec)
        self.assertIn('/bin/rm -rf "${ENGINE_DEST}"', spec)
        self.assertIn('/bin/mkdir -p "${ENGINE_DEST}/tools"', spec)
        self.assertIn('/bin/cp "${ENGINE_SOURCE}"/tools/*.py "${ENGINE_DEST}/tools/"', spec)

    def test_checked_in_xcode_project_matches_the_python_manifest(self) -> None:
        project = PROJECT_FILE.read_text(encoding="utf-8")
        modules = sorted(path.relative_to(PACKAGE_ROOT).as_posix() for path in PACKAGE_ROOT.rglob("*.py"))

        for module in modules:
            with self.subTest(module=module, manifest="input"):
                source = '"$(SRCROOT)/../src/mac_dev_clean/{0}",'.format(module)
                self.assertEqual(project.count(source), 1)
            with self.subTest(module=module, manifest="output"):
                output = (
                    '"$(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)'
                    '/python/mac_dev_clean/{0}",'
                ).format(module)
                self.assertEqual(project.count(output), 1)

        self.assertIn('case \\"${ENGINE_DEST}\\" in', project)
        self.assertIn('/bin/rm -rf \\"${ENGINE_DEST}\\"', project)
        self.assertIn(
            '/bin/cp \\"${ENGINE_SOURCE}\\"/tools/*.py '
            '\\"${ENGINE_DEST}/tools/\\"',
            project,
        )

    def test_release_packaging_replaces_the_controlled_python_package(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('PYTHON_DEST="$RESOURCES/python/mac_dev_clean"', script)
        self.assertIn(
            'if [ "$PYTHON_DEST" != "$APP/Contents/Resources/python/mac_dev_clean" ]; then',
            script,
        )
        self.assertIn('rm -rf "$PYTHON_DEST"', script)
        self.assertIn('"$PYTHON_DEST/tools"', script)
        self.assertIn(
            'cp "$ROOT"/src/mac_dev_clean/tools/*.py '
            '"$PYTHON_DEST/tools/"',
            script,
        )


if __name__ == "__main__":
    unittest.main()

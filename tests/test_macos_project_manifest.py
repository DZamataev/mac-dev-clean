from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "mac_dev_clean"
PROJECT_SPEC = REPOSITORY_ROOT / "macos" / "project.yml"
PROJECT_FILE = REPOSITORY_ROOT / "macos" / "MacDevClean.xcodeproj" / "project.pbxproj"
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts" / "build_macos_app.sh"


def xcode_copy_script() -> str:
    spec = PROJECT_SPEC.read_text(encoding="utf-8")
    body = spec.split("        script: |\n", 1)[1].split("    scheme:\n", 1)[0]
    return textwrap.dedent(body)


def pbx_array(project: str, name: str) -> str:
    body = project.split("\t\t\t{0} = (\n".format(name), 1)[1]
    return body.split("\t\t\t);\n", 1)[0]


def pbx_shell_script(project: str) -> str:
    prefix = "\t\t\tshellScript = "
    line = next(line for line in project.splitlines() if line.startswith(prefix))
    return ast.literal_eval(line[len(prefix) : -1])


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
        script = xcode_copy_script()
        required_in_order = (
            'case "${WRAPPER_NAME}" in',
            'if [ -z "${TARGET_BUILD_DIR}" ]; then',
            'EXPECTED_RESOURCES_PATH="${WRAPPER_NAME}/Contents/Resources"',
            'TARGET_BUILD_REAL=$(CDPATH=\'\' cd -P -- "${TARGET_BUILD_DIR}" && pwd -P)',
            'RESOURCES_REAL=$(CDPATH=\'\' cd -P -- "${RESOURCES_DEST}" && pwd -P)',
            'if [ "${RESOURCES_REAL}" != "${TARGET_BUILD_REAL}/${EXPECTED_RESOURCES_PATH}" ]; then',
            'if [ -L "${PYTHON_PARENT}" ]; then',
            '/bin/mkdir -p "${PYTHON_PARENT}"',
            'PYTHON_PARENT_REAL=$(CDPATH=\'\' cd -P -- "${PYTHON_PARENT}" && pwd -P)',
            'if [ "${PYTHON_PARENT_REAL}" != "${RESOURCES_REAL}/python" ]; then',
            'ENGINE_DEST="${PYTHON_PARENT}/mac_dev_clean"',
            '/bin/rm -rf "${ENGINE_DEST}"',
            '/bin/mkdir -p "${ENGINE_DEST}/tools"',
            '/bin/cp "${ENGINE_SOURCE}"/*.py "${ENGINE_DEST}/"',
            '/bin/cp "${ENGINE_SOURCE}"/tools/*.py "${ENGINE_DEST}/tools/"',
        )
        positions = [script.find(fragment) for fragment in required_in_order]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_xcode_copy_phase_refuses_unconfined_destinations(self) -> None:
        for attack in ("empty", "whitespace", "malformed", "wrapper", "traversal", "symlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                checkout = root / "checkout"
                source = checkout / "src" / "mac_dev_clean"
                (source / "tools").mkdir(parents=True)
                (source / "__init__.py").write_text("# root\n", encoding="utf-8")
                (source / "tools" / "__init__.py").write_text("# tools\n", encoding="utf-8")
                source_root = checkout / "macos"
                source_root.mkdir()
                build = root / "work" / "build"
                resources = build / "Product.app" / "Contents" / "Resources"
                resources.mkdir(parents=True)

                target_build = str(build)
                wrapper_name = "Product.app"
                if attack == "empty":
                    destination = root / "escape" / "python" / "mac_dev_clean"
                    destination.mkdir(parents=True)
                    resource_path = "Product.app/Contents/Resources"
                    target_build = ""
                elif attack == "whitespace":
                    destination = root / "escape" / "python" / "mac_dev_clean"
                    destination.mkdir(parents=True)
                    resource_path = "Product.app/Contents/Resources"
                    target_build = "   "
                elif attack == "malformed":
                    destination = build / "tmp" / "escape" / "python" / "mac_dev_clean"
                    destination.mkdir(parents=True)
                    resource_path = "tmp/escape"
                elif attack == "wrapper":
                    resources = build / "Product" / "Contents" / "Resources"
                    destination = resources / "python" / "mac_dev_clean"
                    destination.mkdir(parents=True)
                    resource_path = "Product/Contents/Resources"
                    wrapper_name = "Product"
                elif attack == "traversal":
                    escaped = root / "escape" / "python" / "mac_dev_clean"
                    escaped.mkdir(parents=True)
                    destination = escaped
                    resource_path = "../../escape"
                else:
                    outside = root / "outside" / "python"
                    destination = outside / "mac_dev_clean"
                    destination.mkdir(parents=True)
                    (resources / "python").symlink_to(outside, target_is_directory=True)
                    resource_path = "Product.app/Contents/Resources"

                sentinel = destination / "SENTINEL"
                sentinel.write_text("keep\n", encoding="utf-8")
                environment = dict(os.environ)
                environment.update(
                    {
                        "SRCROOT": str(source_root),
                        "TARGET_BUILD_DIR": target_build,
                        "UNLOCALIZED_RESOURCES_FOLDER_PATH": resource_path,
                        "WRAPPER_NAME": wrapper_name,
                    }
                )

                completed = subprocess.run(
                    ["/bin/sh", "-c", xcode_copy_script()],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(sentinel.exists())

    def test_checked_in_xcode_project_matches_the_python_manifest(self) -> None:
        project = PROJECT_FILE.read_text(encoding="utf-8")
        inputs = pbx_array(project, "inputPaths")
        outputs = pbx_array(project, "outputPaths")
        modules = sorted(path.relative_to(PACKAGE_ROOT).as_posix() for path in PACKAGE_ROOT.rglob("*.py"))

        for module in modules:
            with self.subTest(module=module, manifest="input"):
                source = '"$(SRCROOT)/../src/mac_dev_clean/{0}",'.format(module)
                self.assertEqual(inputs.count(source), 1)
            with self.subTest(module=module, manifest="output"):
                output = (
                    '"$(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)'
                    '/python/mac_dev_clean/{0}",'
                ).format(module)
                self.assertEqual(outputs.count(output), 1)

        self.assertEqual(pbx_shell_script(project), xcode_copy_script())

    def test_release_packaging_replaces_the_controlled_python_package(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        required_in_order = (
            "ROOT=$(CDPATH='' cd -P -- \"$(dirname -- \"$0\")/..\" && pwd -P)",
            'DIST="$ROOT/dist"',
            'if [ -L "$DIST" ]; then',
            'mkdir -p "$DIST"',
            "DIST_REAL=$(CDPATH='' cd -P -- \"$DIST\" && pwd -P) || exit 1",
            'if [ "$DIST_REAL" != "$DIST" ]; then',
            '/bin/rm -rf "$APP"',
            'mkdir -p "$CONTENTS/MacOS" "$PYTHON_DEST/tools"',
            'cp "$ROOT"/src/mac_dev_clean/*.py "$PYTHON_DEST/"',
            'cp "$ROOT"/src/mac_dev_clean/tools/*.py "$PYTHON_DEST/tools/"',
        )
        positions = [script.find(fragment) for fragment in required_in_order]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        self.assertIn('"$PYTHON_DEST/tools"', script)
        self.assertIn(
            'cp "$ROOT"/src/mac_dev_clean/tools/*.py '
            '"$PYTHON_DEST/tools/"',
            script,
        )


if __name__ == "__main__":
    unittest.main()

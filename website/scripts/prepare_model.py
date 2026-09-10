"""Prepare the repository's already-expanded Panda URDF for static hosting (no ROS).

An optional --xacro input is expanded once using the standalone Python xacro package.
--asset-root must contain the meshes/ directory referenced by the input description.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET


SITE = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = SITE.parent / "genesis/assets/urdf/panda_bullet"


def prepare(asset_root, xacro_path=None):
    asset_root = asset_root.resolve()
    destination = SITE / "assets/panda"
    destination.mkdir(parents=True, exist_ok=True)
    if xacro_path:
        import xacro  # Preparation-only dependency; never used by the viewer.

        xml = xacro.process_file(str(xacro_path)).toxml()
    else:
        xml = (asset_root / "panda.urdf").read_text()
    root = ET.fromstring(xml)
    if any("xacro" in node.tag for node in root.iter()):
        raise ValueError("Unexpanded Xacro found. Use --xacro to expand it during preparation.")

    copied = set()
    adjustments = []

    def package_file(source):
        source = source.resolve()
        relative = source.relative_to(asset_root)
        target = destination / relative
        if source in copied:
            return relative.as_posix()
        if not source.is_file():
            raise FileNotFoundError(f"Missing model asset: {source}")
        copied.add(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".obj":
            lines = []
            removed_lines = 0
            for line in source.read_text().splitlines():
                # The Panda link5/link6 exports contain loose CAD edges alongside
                # triangle faces. OBJLoader classifies an entire mixed object as
                # LineSegments, losing its surface shading and shadow casting.
                # These assets are surface meshes; retain faces, omit loose edges.
                if line.split(maxsplit=1)[:1] == ["l"]:
                    removed_lines += 1
                    continue
                if line.startswith("mtllib "):
                    material = source.parent / line.split(maxsplit=1)[1]
                    if not material.exists():
                        # The repository's coarse collision OBJs reference absent MTLs.
                        # Their URDF material is the intended fallback in the browser.
                        adjustments.append(f"{relative}: removed absent {material.name}; uses URDF material")
                        continue
                    package_file(material)
                lines.append(line)
            if removed_lines:
                adjustments.append(f"{relative}: removed {removed_lines} loose OBJ lines; all surface faces retained")
            target.write_text(
                "# Prepared for web: material references validated; loose CAD lines omitted.\n"
                + "\n".join(lines)
                + "\n"
            )
        elif source.suffix.lower() == ".mtl":
            lines = []
            for line in source.read_text().splitlines():
                if re.match(r"\s*(map_\w+|bump)\s", line):
                    key, texture = line.strip().split(maxsplit=1)
                    # This model uses simple texture references without map options.
                    if texture.startswith("-"):
                        raise ValueError(f"Texture options need explicit handling: {line}")
                    texture_source = source.parent / texture
                    if not texture_source.is_file():
                        # One upstream MTL contains a Windows developer's absolute path.
                        candidates = list(asset_root.rglob(texture.replace("\\", "/").split("/")[-1]))
                        if len(candidates) != 1:
                            raise FileNotFoundError(f"Cannot uniquely resolve texture: {texture}")
                        texture_source = candidates[0]
                        replacement = Path(os.path.relpath(texture_source, source.parent)).as_posix()
                        adjustments.append(f"{relative}: replaced {texture} with {replacement}")
                        texture = replacement
                    package_file(texture_source)
                    line = f"{key} {texture}"
                lines.append(line)
            target.write_text("# Prepared for web: texture references validated.\n" + "\n".join(lines) + "\n")
        else:
            shutil.copy2(source, target)
        return relative.as_posix()

    for node in root.iter():
        if node.tag not in ("mesh", "texture"):
            continue
        reference = node.attrib["filename"]
        # This repository uses package://meshes/... (no package-name component).
        local = reference.removeprefix("package://")
        if "://" in local or Path(local).is_absolute():
            raise ValueError(f"Asset must resolve inside --asset-root: {reference}")
        node.set("filename", package_file(asset_root / local))
    ET.indent(root, space="  ")
    output = ET.tostring(root, encoding="unicode")
    if "package://" in output or "${" in output or "$(" in output:
        raise ValueError("Unresolved ROS or Xacro references remain")
    (destination / "panda.urdf").write_text(
        '<?xml version="1.0"?>\n'
        "<!-- Prepared from the Genesis Panda description; asset paths changed for static hosting. -->\n" + output
    )
    shutil.copy2(asset_root / "LICENSE.txt", destination / "LICENSE.txt")
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "source": str(xacro_path) if xacro_path else "genesis/assets/urdf/panda_bullet/panda.urdf",
                "preparation": "Xacro expanded once"
                if xacro_path
                else "Reused repository URDF already expanded by Xacro",
                "files": sorted(str(p.relative_to(asset_root)) for p in copied),
                "adjustments": adjustments,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Prepared Panda URDF and {len(copied)} mesh/material/texture assets in {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--xacro", type=Path, help="Optional Xacro source; requires the standalone Python xacro package"
    )
    args = parser.parse_args()
    prepare(args.asset_root, args.xacro)

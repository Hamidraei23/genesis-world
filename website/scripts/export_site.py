"""Export only browser runtime files. This reduces source exposure, not inspectability."""

import argparse
from pathlib import Path
import shutil


SITE = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "index.html",
    "assets/panda/panda.urdf",
    "assets/panda/LICENSE.txt",
    "assets/vendor/viewer.js",
    "assets/vendor/viewer.js.LEGAL.txt",
    "assets/vendor/THREE-LICENSE.txt",
    "assets/vendor/URDF-LOADER-LICENSE.txt",
    "assets/vendor/URDF-LOADER-NOTICE.md",
)


def export_static(destination):
    destination = destination.resolve()
    if destination == SITE or SITE.is_relative_to(destination):
        raise ValueError("Export must not overwrite the development directory or its parents")
    bundle = (SITE / "assets/vendor/viewer.js").read_text()
    if "sourceMappingURL=" in bundle:
        raise ValueError("Public bundle must not expose a source map")
    files = [Path(name) for name in RUNTIME_FILES]
    files.extend(
        path.relative_to(SITE)
        for path in sorted((SITE / "assets/panda/meshes").rglob("*"))
        if path.suffix.lower() in {".obj", ".mtl", ".png", ".jpg", ".jpeg"}
    )
    # Validate the complete allowlist before changing a deployment.
    for relative in files:
        if not (SITE / relative).is_file():
            raise FileNotFoundError(SITE / relative)
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SITE / relative, target)
    # Remove the two development documents shipped by the initial viewer export.
    for relative in ("README.md", "assets/panda/manifest.json"):
        (destination / relative).unlink(missing_ok=True)
    print(f"Exported {len(files)} runtime files to {destination}; source and build metadata excluded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SITE / "public")
    args = parser.parse_args()
    export_static(args.output)

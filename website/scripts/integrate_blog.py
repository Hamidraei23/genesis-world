"""Stage a reviewable blog integration, then install only the prepared changes."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from export_site import export_static

SITE = Path(__file__).resolve().parents[1]
PREVIEW = SITE / "preview"
STATE = SITE / "integration-state.json"
SECTION = """<!-- Franka Playground: local static viewer -->
<section id="robot-playground" style="scroll-margin-top:90px">
  <div class="section-header">
    <span class="section-tag">&lt;interactive_robotics&gt;</span>
    <h2 class="section-title">Franka Playground</h2>
    <div class="section-divider"></div>
  </div>
  <p style="color:var(--text-secondary);margin-bottom:24px;line-height:1.8">
    Explore the Franka Panda from my Genesis workspace. Rotate the view, adjust all
    seven joints, and try the gripper directly in your browser.
  </p>
  <style>
    #franka-playground-frame { width:100%;height:830px;border:1px solid var(--border);border-radius:12px;background:#171a23; }
    @media(max-width:650px) { #franka-playground-frame { height:1330px; } }
  </style>
  <iframe id="franka-playground-frame" src="blog/franka-playground/?embed"
    title="Interactive Franka Panda robot" loading="lazy" allow="fullscreen"></iframe>
  <p style="margin-top:14px"><a class="project-link" href="blog/franka-playground/">Open the full robot playground →</a></p>
</section>

"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage(blog):
    original = blog / "index.html"
    text = original.read_text()
    if 'id="robot-playground"' in text:
        raise ValueError("Blog already contains the playground; inspect it before restaging")
    if text.count('<section id="projects">') != 1:
        raise ValueError("Cannot identify the Projects section uniquely")
    nav = '    <li><a href="#projects">Projects</a></li>'
    if text.count(nav) != 1:
        raise ValueError("Cannot identify the Projects navigation entry uniquely")
    shutil.copytree(blog, PREVIEW, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "node_modules"))
    text = text.replace(nav, nav + '\n    <li><a href="#robot-playground">Robot lab</a></li>')
    text = text.replace('<section id="projects">', SECTION + '<section id="projects">')
    (PREVIEW / "index.html").write_text(text)
    viewer = PREVIEW / "blog/franka-playground"
    viewer.mkdir(parents=True, exist_ok=True)
    export_static(viewer)
    STATE.write_text(json.dumps({"blog": str(blog), "original_sha256": digest(original)}, indent=2))
    print(f"Prepared blog preview at {PREVIEW}")


def install(blog):
    state = json.loads(STATE.read_text())
    if state["blog"] != str(blog) or state["original_sha256"] != digest(blog / "index.html"):
        raise ValueError("Blog changed since staging; restage and review before installing")
    target = blog / "blog/franka-playground"
    if target.exists():
        raise ValueError("Viewer destination already exists; inspect before replacing")
    shutil.copytree(PREVIEW / "blog/franka-playground", target)
    shutil.copy2(PREVIEW / "index.html", blog / "index.html")
    print(f"Installed local-only viewer at {target}; homepage updated. Nothing published.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "install"))
    parser.add_argument("blog", type=Path)
    args = parser.parse_args()
    {"stage": stage, "install": install}[args.action](args.blog.resolve())

# Franka Playground

A static HTML robot playground for Hamidreza Raei's portfolio. Includes native HTML
joint sliders, coupled gripper control, a motion demo, tool position, camera presets,
orbit/zoom/pan controls, lighting, shadows, a floor grid, axes, and wireframe mode.

Joint angles support degrees or radians, with both sliders and numeric entry. Changing
units changes the display only; the robot's internal angles remain in radians. Numeric
entries are clamped to URDF limits.

The **Cartesian EE** tab commands the pose of a fingertip reference point. That point
sits on the gripper axis level with the finger tips, 0.1123 m along `panda_hand` +z:
the finger joint origin (0.0584 m) plus the finger length (0.0539 m). It is 7.3 mm
ahead of `panda_grasptarget`, which sits between the fingers rather than at their tips,
and it stays on the centreline at any gripper opening. It is also the point the
readout reports and the point every commanded rotation turns about.

Enter XYZ targets in metres in the fixed robot base frame, use the axis jog buttons
(1 mm, 1 cm, or 5 cm), or drag the gizmo attached to the target. Camera orbit pauses
during a drag. **Use current EE** resets the target to the achieved pose.

Orientation uses roll, pitch and yaw about the fixed base X, Y and Z axes, the same
convention as URDF `rpy`, in whichever unit the angle selector shows. Each jog button
steps its own angle by the rotate step, so pitch continues past -90 degrees rather
than folding back at the decomposition's clamp; the Panda's home gripper points down
at a pitch of about -88 degrees, so that boundary is one step away. Angles adopted
from the robot or from a gizmo drag are read back in the standard range.

**Drag mode** switches the gizmo between Move and Rotate; `G` and `R` do the same from
the keyboard while the tab is open and no field has focus. Rotating turns the gripper
about the fingertip point instead of swinging it around. **Keep gripper orientation**
constrains the solve to the commanded roll/pitch/yaw; unchecking it gives position-only
IK and disables the angle fields, the rotate step, and rotate mode.

The local damped-least-squares IK solver respects the URDF joint limits and accepts
position error below 0.5 mm and held-orientation error below 0.003 radians. It solves
from the current joint pose; a local solve can fail even for some reachable targets.
On failure, the previous pose is restored and the target marker turns red. Joint
controls, demo playback, and Home keep the target synchronized. This is instantaneous
kinematic positioning, without dynamics, collision checking, or hardware control.

## Local preview

The prepared `index.html` and `assets/` are self-contained. No ROS, Python packages,
Node.js, CDN, or simulator is needed at runtime. Serve over HTTP (not `file://`):

```bash
cd /home/hami/workspaces/personal_blog_ws/Hamidraei23.github.io
python3 -m http.server 8000 --bind 127.0.0.1
```

Open http://localhost:8000/#robot-playground for the portfolio embed, or
http://localhost:8000/blog/franka-playground/ for the full page. The existing
portfolio still has its own external fonts and other resources; the viewer itself
makes only same-origin requests. Use a browser with WebGL2 support.

The viewer can also be served directly from `genesis-world/website` with `npm start`
or the same Python command. At that URL, the portfolio backlink leads to the server
root; the final blog installation provides the correct parent portfolio.

## Source and one-time preparation

The editable source lives in `genesis-world/website/`. From that directory:

```bash
npm ci
npm run build
npm test
```

Tests use the locally installed Google Chrome; set `CHROME_PATH` to override its
location. The generated bundle includes Three.js 0.185.1 and urdf-loader 0.13.1;
dependencies are pinned in `package-lock.json`. The build packages all referenced
visual and collision meshes, MTLs, and textures with their licenses. Only visual
geometry is rendered. See `assets/panda/manifest.json` for the asset inventory and
path repairs, including broken upstream MTL references and an absolute Windows
texture path. The original Genesis assets are unchanged.

Preparation also removes loose OBJ line records from the surface meshes. In the
upstream link5/link6 files, these CAD edges are mixed with triangles in the same
object. Three.js OBJLoader otherwise loads that whole object as `LineSegments`,
so it appears broken and cannot cast a surface shadow. All triangle faces, normals,
UVs, materials, and URDF joint transforms are preserved.

`genesis/assets/urdf/panda_bullet/panda.urdf` explicitly records that it was already
generated from `panda_arm_hand.urdf.xacro`. That Xacro source is not in this checkout;
the preparation script reuses the expanded URDF rather than inventing or converting
a different model. All `package://meshes/...` references become relative URLs.
The browser never processes Xacro. If the original source becomes available,
install the standalone Python `xacro` package in a preparation environment and use:

```bash
python3 scripts/prepare_model.py --xacro /path/to/panda_arm_hand.urdf.xacro --asset-root /path/to/panda_bullet
node scripts/build.mjs
```

This optional input must use the same `package://meshes/...` layout and contain the
Panda link/joint names; Xacro includes and any package lookup requirements must be
resolvable in the preparation environment. Preparation fails on unresolved paths.

The active `examples/rigid/env_franka_parallel.py` loads the MJCF Panda, while this
viewer uses the repository's URDF Panda variant. Its home joint positions are copied
from that environment: `[0, -0.82, 0, -2.18, 0, 2.9, 0.78]` radians, with each finger
at `0.00809 m`. The displayed position is the fingertip reference point in the URDF
base frame. Joint moves are forward kinematics only, without dynamics or collision
checking.

## Portfolio integration

### Public build and implementation privacy

Publish only the generated `public/` directory. The build minifies and bundles the
JavaScript with source maps explicitly disabled. The export includes only runtime
files and required third-party licenses/attribution. It excludes this development
README, preparation manifest, source modules, tests, build scripts, and dependencies.
Update the installed viewer without changing the portfolio homepage with:

```bash
python3 scripts/export_site.py --output /home/hami/workspaces/personal_blog_ws/Hamidraei23.github.io/blog/franka-playground
```

This is exposure reduction, not confidentiality. DevTools can inspect, reformat,
download, and debug the shipped JavaScript; the browser also needs the URDF and
meshes. Keyboard shortcuts, right-click blocking, and obfuscation cannot enforce
secrecy. No such blocking is installed. Keep original source repositories private
if the editable source should not be public; do not commit development files to a
public website repository. This export does not change repository visibility or
remove files from Git history. Sensitive algorithms or secrets would require a
separate backend that sends only results to the browser. GitHub Pages itself is
static hosting. The current viewer has no backend or secrets.

References: [Chrome source maps](https://developer.chrome.com/docs/devtools/javascript/source-maps)
and [GitHub Pages hosting](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages).

`scripts/integrate_blog.py stage BLOG_PATH` prepares a full preview in `preview/`,
adds a Robot lab navigation entry, and embeds the viewer before the Projects section.
`scripts/integrate_blog.py install BLOG_PATH` copies that reviewed addition and the
static viewer to the blog. It refuses to overwrite homepage edits made after staging.
It does not commit, push, or publish anything.

The viewer also supports `?embed`, which hides its masthead and article text:

```html
<iframe src="blog/franka-playground/?embed" title="Interactive Franka Panda robot"
        loading="lazy" allow="fullscreen" style="width:100%;height:830px;border:0"></iframe>
```

Use a taller frame (1330px) below 650px viewport width so the controls fit below the
robot. The bundled source follows the official
[Three.js OrbitControls documentation](https://threejs.org/docs/pages/OrbitControls.html)
and [URDF loader API](https://github.com/gkjohnson/urdf-loaders/blob/master/javascript/docs/API.md).
Third-party license text and attribution are included under `assets/vendor/` and
`assets/panda/`.

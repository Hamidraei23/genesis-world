"""
Particle-based deformable sheet for Genesis.

This is a Genesis-native counterpart to the MuJoCo flex-sheet models under
``~/.mujoco/mujoco-3.3.0/model/flex/`` (e.g. ``sheet5x5_b.xml``). That MuJoCo model
represents a cloth as a 5x5 lattice of free rigid bodies connected by spatial tendon
springs (structural + shear + bend) and grabbed by a mocap body welded to a corner.

Genesis has no tendon/spring-network primitive, so the equivalent behavior here is built
on top of its native PBD cloth solver instead: a custom 5x5 (25-vertex) grid mesh is fed
to ``gs.materials.PBD.Cloth`` and ``pbd_options.particle_size`` is tuned so the solver's
remeshing step leaves the grid untouched at exactly 25 particles. PBD's structural edges,
diagonal (shear) edges and cross-face bending constraints play the same structural role as
the MuJoCo tendons. Grasp points are grabbed/released with ``fix_particles`` / ``release_particle``
and dragged with ``set_particles_pos``, mirroring the MuJoCo mocap weld. The actual solver,
constraint model and resulting motion are entirely different from the MuJoCo reference; only
the coarse "few visible particles behaving like a sheet, with grabbable points" behavior is
carried over.
"""

import argparse
import os

import numpy as np
from scipy.spatial.transform import Rotation as R, Slerp
import genesis as gs

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
CHECKPOINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")


def _grid_obj_path(n_side: int, spacing: float) -> str:
    return os.path.join(ASSET_DIR, f"sheet_grid_{n_side}x{n_side}_s{spacing:g}.obj")


def build_grid_mesh_obj(path, n_side=5, spacing=0.08):
    """Write a flat n_side x n_side quad-grid plane (triangulated) as an .obj file."""
    half = (n_side - 1) * spacing / 2.0
    verts = [
        (i * spacing - half, j * spacing - half, 0.0) for i in range(n_side) for j in range(n_side)
    ]

    def vid(i, j):
        return i * n_side + j

    faces = []
    for i in range(n_side - 1):
        for j in range(n_side - 1):
            a, b, c, d = vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)
            # Alternate the quad's diagonal split in a checkerboard pattern. Always cutting
            # along the same (a-c) diagonal gives the cloth a directional "grain" -- every
            # bending constraint resists folding across that one diagonal only, so the sheet
            # is stiffer against folds that cross it than against the other diagonal. That
            # showed up as asymmetric bending depending on where the sheet was grasped.
            if (i + j) % 2 == 0:
                faces.append((a, b, c))
                faces.append((a, c, d))
            else:
                faces.append((a, b, d))
                faces.append((b, c, d))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for v in verts:
            f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        for a, b, c in faces:
            f.write(f"f {a + 1} {b + 1} {c + 1}\n")


class DeformableSheetEnv:
    """A ~25-particle deformable sheet with 3 grabbable points along its top edge, running on Genesis's PBD solver."""

    def __init__(
        self,
        n_side: int = 5,
        spacing: float = 0.05,  # (n_side - 1) * spacing = 0.2 m sheet extent
        particle_size: float = 0.0543,  # remesh target; calibrated to keep exactly n_side**2 particles at this spacing
        particle_radius: float = 0.01,  # rendered particle radius (visual only, see note below)
        sheet_height: float = 0.25,
        vis: bool = False,
        record: bool = False,
        cpu: bool = False,
    ):
        # NOTE on particle_size vs particle_radius: Genesis's PBD cloth remeshes the input mesh
        # to a target edge length equal to `particle_size` (this is what determines how many
        # particles survive from the mesh), and separately derives the solver/render radius as
        # `particle_size / 2`. Those two roles are coupled through one parameter, so a physically
        # tiny particle_size (e.g. 2 * particle_radius) would over-subdivide the grid well past
        # n_side**2 particles. `particle_size` is therefore kept at its calibrated value (which
        # controls particle count) and `particle_radius` only rescales how large the particles are
        # *drawn*, via `vis_options.particle_size_scale`.
        # gs.init() raises if called twice in one process (e.g. generate_all_checkpoints
        # builds 3 envs in a row) -- guard it, at the cost of ignoring `cpu` on the 2nd+
        # construction if it differs from the first (not a concern for any current caller,
        # which always passes the same value across envs built in a single process).
        if not gs._initialized:
            gs.init(backend=gs.cpu if cpu else gs.gpu, precision="32", logging_level="warning")

        obj_path = _grid_obj_path(n_side, spacing)
        build_grid_mesh_obj(obj_path, n_side=n_side, spacing=spacing)

        self.n_side = n_side
        self.spacing = spacing
        self.dt = 2e-2

        # Kinematically commanding a fixed particle below this height drives it into the
        # floor-collision projection, which fights the pin and blows the solve up (verified
        # empirically: stable at particle_size/2, explosive a millimeter below). Callers
        # that place particles near the ground must clamp to this.
        self.solver_particle_radius = particle_size / 2.0
        particle_size_scale = particle_radius / self.solver_particle_radius

        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=self.dt, substeps=10),
            pbd_options=gs.options.PBDOptions(particle_size=particle_size),
            vis_options=gs.options.VisOptions(particle_size_scale=particle_size_scale),
            viewer_options=gs.options.ViewerOptions(
                camera_pos=(0.5, 0.5, 0.5),
                camera_lookat=(0.0, 0.0, 0.1),
                camera_fov=35,
                max_FPS=60,
            ),
            show_viewer=vis,
        )

        self.plane = self.scene.add_entity(gs.morphs.Plane())

        self.sheet = self.scene.add_entity(
            material=gs.materials.PBD.Cloth(
                stretch_compliance=1e-3,
                bending_compliance=2e-3,  # softer than default -> visible creases, not a smooth surface
            ),
            morph=gs.morphs.Mesh(file=obj_path, pos=(0.0, 0.0, sheet_height)),
            surface=gs.surfaces.Default(color=(0.75, 0.25, 0.25, 1.0), vis_mode="particle"),
        )

        self.cam = None
        if record:
            self.cam = self.scene.add_camera(
                res=(1280, 960),
                pos=(0.5, 0.5, 0.5),
                lookat=(0.0, 0.0, 0.1),
                fov=35,
                GUI=False,
            )

        self.scene.build()

        if self.cam is not None:
            self.cam.start_recording()

        assert self.sheet.n_particles == n_side * n_side, (
            f"expected {n_side * n_side} particles, got {self.sheet.n_particles} "
            "-- adjust `spacing`/`particle_size` calibration"
        )
        assert n_side >= 3 and n_side % 2 == 1, "grasp groups assume an odd n_side >= 3 (need a center column)"

        # Logical grid position (i, j): i is the row (0 = front edge, n_side-1 = back edge),
        # j is the column within the row (0 = left, n_side-1 = right). We designate the back
        # row (i = n_side - 1) as the "top" edge of the sheet and place 3 grasp points on it:
        # a 3-particle cluster at each corner (corner + its edge neighbor + its inward
        # neighbor), and a 4-particle cluster at the middle (middle + its left/right edge
        # neighbors + its inward neighbor). The middle cluster shares its left/right particles
        # with the two corner clusters -- harmless here since all 3 groups always move together
        # with the exact same trajectory.
        #
        # We resolve (i, j) -> particle index via `find_closest_particle` (nearest actual
        # particle to that grid point's intended world position) rather than assuming the
        # mesh's remeshing preserves our authored vertex order: remeshing is free to
        # reorder/insert/merge vertices, so a fixed `i * n_side + j` index formula is not
        # guaranteed to still point at the right grid location after remeshing.
        n = self.n_side
        half = (n - 1) * spacing / 2.0

        def grid_world_pos(i, j):
            return (i * spacing - half, j * spacing - half, sheet_height)

        top_row = n - 1
        mid_col = (n - 1) // 2

        target_labels = [
            ("front_left", (0, 0)),
            ("front_right", (0, n - 1)),
            ("back_left", (n - 1, 0)),
            ("back_right", (n - 1, n - 1)),
            ("top_left_corner", (top_row, 0)),
            ("top_left_edge", (top_row, 1)),
            ("top_left_inward", (top_row - 1, 0)),
            ("top_right_corner", (top_row, n - 1)),
            ("top_right_edge", (top_row, n - 2)),
            ("top_right_inward", (top_row - 1, n - 1)),
            ("top_mid", (top_row, mid_col)),
            ("top_mid_right", (top_row, mid_col + 1)),
            ("top_mid_left", (top_row, mid_col - 1)),
            ("top_mid_inward", (top_row - 1, mid_col)),
        ]
        # `find_closest_particle` answers one query position at a time (its batch dimension
        # is over parallel envs, not over multiple distinct query points), so resolve each
        # named grid location with its own call.
        lut = {}
        for name, (i, j) in target_labels:
            found = self.sheet.find_closest_particle(grid_world_pos(i, j))
            if hasattr(found, "detach"):
                found = found.detach().cpu().numpy()
            lut[name] = int(np.asarray(found).reshape(-1)[0])

        self.corner_idx = {
            "front_left": lut["front_left"],
            "front_right": lut["front_right"],
            "back_left": lut["back_left"],
            "back_right": lut["back_right"],
        }
        self.grasp_groups = {
            "top_left": [lut["top_left_corner"], lut["top_left_edge"], lut["top_left_inward"]],
            "top_middle": [lut["top_mid"], lut["top_mid_right"], lut["top_mid_left"], lut["top_mid_inward"]],
            "top_right": [lut["top_right_corner"], lut["top_right_edge"], lut["top_right_inward"]],
        }
        self._held_groups = set()
        self._init_pos = self.get_particle_positions().copy()

    @property
    def n_particles(self) -> int:
        return self.sheet.n_particles

    def get_particle_positions(self) -> np.ndarray:
        pos = self.sheet.get_particles_pos()
        if hasattr(pos, "detach"):
            pos = pos.detach().cpu().numpy()
        return np.asarray(pos)

    def get_corners(self):
        """Return (ar, al, bl, br) corner positions, in the same cyclic order (back_left ->
        front_left -> front_right -> back_right) used by the MuJoCo reference's `self.corners`."""
        pos = self.get_particle_positions()
        ar = pos[self.corner_idx["back_left"]]
        al = pos[self.corner_idx["front_left"]]
        bl = pos[self.corner_idx["front_right"]]
        br = pos[self.corner_idx["back_right"]]
        return ar, al, bl, br

    def get_obs(self, desired_state: int) -> np.ndarray:
        """Corner-relative observation, mirroring the MuJoCo reference's `obs_points = corners -
        corners[0]` slots: desired_state plus the other 3 corners' offsets from `ar` (back_left),
        in the reference's obs order -- al (front_left), br (back_right), bl (front_right)."""
        ar, al, bl, br = self.get_corners()
        obs_points = np.stack([al - ar, br - ar, bl - ar]).astype(np.float32)
        return np.concatenate([[float(desired_state)], obs_points.flatten()]).astype(np.float32)

    def _held_particle_height(self) -> float:
        """Current world-frame height (z), averaged over whatever grasp point(s) are
        currently held. If nothing is currently held (e.g. after release), falls back to
        the average height of the 4 corners -- i.e. "has the sheet settled back down"."""
        idxs = sorted({i for name in self._held_groups for i in self.grasp_groups[name]})
        if not idxs:
            idxs = list(self.corner_idx.values())
        pos = self.get_particle_positions()
        return float(pos[idxs, 2].mean())

    def is_placed(self, height_threshold: float = 0.05) -> bool:
        """Analogous to the MuJoCo reference's position-arrival check (which flips
        `termination_next`/`termination` once the gripper's position error drops below a
        threshold): True once the grasp point(s) -- or, if released, the sheet's corners --
        are back down near the table, i.e. actually placed rather than still lifted mid-air.
        We don't replicate the reference's extra "first-frame arrival" penalty / "must stay
        close for 2 consecutive steps" bookkeeping (that was tuned for its one-macro-action-
        per-`step_counter` RL loop, which has no counterpart in this scripted-trajectory env);
        this is just the direct height check.
        """
        return self._held_particle_height() < height_threshold

    def compute_state_reward(self, desired_state: int, height_threshold: float = 0.05) -> float:
        """Port of the MuJoCo reference's per-corner `state_reward` for its 3 implemented
        target shapes (0 = flat/square, 1 = single-diagonal fold, 2 = edge-to-edge fold in
        half). Formula, weights (10/-8/4, 350/50) and corner cyclic order are unchanged from
        the reference. The reference runs with `scale = 1.0` -- distances in raw meters
        against its absolute constants (0.0225 m^2 reference area, 0.05/0.20 m and
        0.04/0.15 m sigma/ref pairs), even for its 0.2 m `new_variant` sheet (the same
        side length as ours) -- so the constants are used verbatim here too, with no
        sheet-size rescaling.

        Also ported: the reference only computes `state_reward` once the gripper has
        descended within a threshold of its goal height (`abs(pos_err[2]) < 0.015`),
        returning 0.0 otherwise. We have no external position goal to measure error against,
        so the analogous gate is the grasp point's *absolute* height: the shape reward only
        counts once the sheet is actually placed down (`is_placed`), not while still held up
        mid-air (e.g. during a lift-and-hold).
        """
        if desired_state not in (0, 1, 2):
            raise ValueError(f"desired_state must be 0, 1, or 2 (got {desired_state}); state 3 has no reward in the reference either")

        if not self.is_placed(height_threshold):
            return 0.0

        ar, al, bl, br = self.get_corners()

        if desired_state == 0:
            # "not folded and not turned": reward a flat, undistorted, square sheet by
            # comparing the area implied by adjacent edges against the area implied by
            # the two diagonals -- both should agree and match the reference area.
            d_diag = np.linalg.norm(al - br)
            d_diag2 = np.linalg.norm(ar - bl)
            d_top = np.linalg.norm(al - ar)
            d_r = np.linalg.norm(ar - br)
            area_one = d_top * d_r
            area_two = (d_diag * d_diag2) / 2.0
            x_a = abs(area_one) / 0.0225
            x_b = abs(area_two) / 0.0225
            y_1 = 10 * np.exp(-8 * ((x_a - 1) ** 2))
            y_2 = 10 * np.exp(-8 * ((x_b - 1) ** 2))
            return float(y_1 * y_2 * 4)

        if desired_state == 1:
            # single-diagonal fold: reward corner `ar` (back_left) approaching `bl` (front_right).
            d_fold = np.linalg.norm(ar - bl)
            sigma = 0.05
            ref = 0.20
            gauss = np.exp(-0.5 * (d_fold / sigma) ** 2)
            linear = float(np.clip(1.0 - d_fold / ref, 0.0, 1.0))
            return float(gauss * 350.0 + linear * 50.0)

        # desired_state == 2: edge-to-edge fold in half -- both edge pairs (ar<->al and
        # br<->bl) should come together in the xy-plane (z differs by the stacked-layer gap).
        d_xy_a = np.linalg.norm((ar - al)[:2])
        d_xy_b = np.linalg.norm((br - bl)[:2])
        sigma = 0.04
        score_a = np.exp(-0.5 * (d_xy_a / sigma) ** 2)
        score_b = np.exp(-0.5 * (d_xy_b / sigma) ** 2)
        fold_gauss = score_a * score_b
        ref = 0.15
        approach_a = float(np.clip(1.0 - d_xy_a / ref, 0.0, 1.0))
        approach_b = float(np.clip(1.0 - d_xy_b / ref, 0.0, 1.0))
        fold_linear = approach_a * approach_b
        return float(fold_gauss * 350.0 + fold_linear * 50.0)

    def compute_success_score(self, desired_state: int, height_threshold: float = 0.05) -> float:
        """Port of the MuJoCo reference's `compute_success_score` ([0, 100] at episode end),
        restricted to the two components that are actually about corner/sheet geometry:

          - `state_score` (50% in the reference): `state_reward / 400`, clipped to [0, 1].
          - `geom_score` (5% in the reference): a state-specific corner-geometry coherence
            check (z-span for state 0, diagonal asymmetry+proximity for state 1, fold
            distance+layer separation for state 2) -- same formulas and absolute length
            constants as the reference (which runs at scale = 1.0), like `compute_state_reward`.

        The reference's other 3 components -- `pos_score`/`yaw_score` (Gaussian/linear
        error against an external gripper-box position/yaw goal) and `eff_score` (fewer
        RL steps used -> higher) -- have no counterpart here (no position/yaw goal, no
        step-budget concept in this scripted-trajectory env), so they're dropped and the
        remaining two weights are renormalized to sum to 100%: 50/(50+5) = ~90.9% state,
        5/(50+5) = ~9.1% geom, instead of arbitrarily reweighting to 50/50.

        Same height gate as `compute_state_reward`: returns 0.0 while still lifted mid-air.
        """
        if not self.is_placed(height_threshold):
            return 0.0

        MAX_STATE_REWARD = 400.0
        state_score = float(np.clip(self.compute_state_reward(desired_state, height_threshold) / MAX_STATE_REWARD, 0.0, 1.0))

        ar, al, bl, br = self.get_corners()

        if desired_state == 0:
            z_span = max(ar[2], al[2], bl[2], br[2]) - min(ar[2], al[2], bl[2], br[2])
            geom_score = float(np.exp(-0.5 * (z_span / 0.04) ** 2))
        elif desired_state == 1:
            d_fold = np.linalg.norm(ar - bl)
            d_other = np.linalg.norm(al - br)
            asymmetry = float(np.clip((d_other / (d_fold + 1e-6) - 1.0) / 3.0, 0.0, 1.0))
            proximity = float(np.exp(-0.5 * (d_fold / 0.05) ** 2))
            geom_score = 0.5 * asymmetry + 0.5 * proximity
        else:  # desired_state == 2
            d_xy_a = np.linalg.norm((ar - al)[:2])
            d_xy_b = np.linalg.norm((br - bl)[:2])
            fold_score = float(
                np.exp(-0.5 * (d_xy_a / 0.04) ** 2) * np.exp(-0.5 * (d_xy_b / 0.04) ** 2)
            )
            z_span = max(ar[2], al[2], bl[2], br[2]) - min(ar[2], al[2], bl[2], br[2])
            layer_score = float(np.clip(z_span / 0.08, 0.0, 1.0))
            geom_score = 0.6 * fold_score + 0.4 * layer_score

        state_w, geom_w = 50.0 / 55.0, 5.0 / 55.0
        score = (state_w * state_score + geom_w * geom_score) * 100.0
        return float(np.clip(score, 0.0, 100.0))

    def reset(self):
        self.release_all_groups()
        self.sheet.set_particles_pos(self._init_pos)
        self.sheet.set_particles_vel(np.zeros_like(self._init_pos))

    def save_checkpoint(self, path: str):
        """Save current particle positions/velocities and which grasp group(s) are
        currently held to an .npz file, for later restoration via `load_checkpoint`."""
        pos = self.get_particle_positions()
        vel = self.sheet.get_particles_vel()
        if hasattr(vel, "detach"):
            vel = vel.detach().cpu().numpy()
        vel = np.asarray(vel, dtype=np.float32)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        np.savez(path, pos=pos, vel=vel, held_groups=np.array(sorted(self._held_groups), dtype=object))

    def load_checkpoint(self, path: str):
        """Restore particle positions/velocities from a `save_checkpoint` file, and re-grab
        whichever grasp group(s) were held at save time (so the loaded state's kinematic
        hold is preserved, not just its geometry)."""
        data = np.load(path, allow_pickle=True)
        self.release_all_groups()
        self.sheet.set_particles_pos(data["pos"].astype(np.float32))
        self.sheet.set_particles_vel(data["vel"].astype(np.float32))
        for name in data["held_groups"].tolist():
            self.grab_group(str(name))

    def grab_group(self, name: str):
        self.sheet.fix_particles(self.grasp_groups[name])
        self._held_groups.add(name)

    def move_group(self, name: str, delta_xyz):
        """Rigidly translate a grasp group by `delta_xyz` from its initial (spawn) layout."""
        idxs = self.grasp_groups[name]
        target = self._init_pos[idxs] + np.asarray(delta_xyz, dtype=np.float32)
        self.sheet.set_particles_pos(target, particles_idx_local=idxs)

    def release_group(self, name: str):
        self.sheet.release_particle(self.grasp_groups[name])
        self._held_groups.discard(name)

    def release_all_groups(self):
        for name in list(self._held_groups):
            self.release_group(name)

    def step(self, n: int = 1):
        for _ in range(n):
            self.scene.step()
            if self.cam is not None:
                self.cam.render()


class DeformSheetTaskEnv:
    """
    RL task wrapper around DeformableSheetEnv, ported 1:1 from the MuJoCo reference env
    (~/workspaces/ros_two/mujoco_ws/my_playground/envs/deform_env.py, class DeformEnv) so
    that the observation layout/scale, action semantics, reward function and every
    constant match it exactly. `scale` is 1.0 as in the reference (its
    `max(|obs_points|)/0.15` line is commented out), so all observations and reward
    distances are raw meters. The intentional differences are only the physics backend
    (Genesis PBD cloth vs MuJoCo flex) and how the gripper is realized (below).

    Gripper: the reference welds a free "box" body (spawned at (0, 0, 0.5)) to a mocap
        and pins 4 flex vertices of the sheet to it; each RL step commands an *absolute*
        6-DOF pose that the mocap tracks over a 0.75 s trajectory (smoothstep position,
        slerp orientation). Here the analog is the held grasp cluster (default: the
        4-particle "top_middle" group, matching the reference's 4 pins), whose particles
        are moved rigidly along the same 0.75 s trajectory profile to the commanded pose.
        Deviation: the reference rotates the pinned vertices about the box origin (offset
        ~(0.08, 0.10, -0.01) m from the pin centroid -- an artifact of its XML layout),
        whereas here the rotation pivot is the cluster's own centroid and the commanded
        position IS the cluster centroid. Near the floor the actual particle placement is
        clamped to the PBD solver's particle radius (~0.027 m) to keep the solve stable;
        obs/reward always read the *commanded* pose, just as the reference's box tracks
        its mocap command essentially exactly.

    Actions (6,) in [-1, 1]: (dz, dx, dy, x_rad, y_rad, z_rad), mapped exactly as the
        reference (`action[0] *= 0.959`, then normalize_action): z = ((dz + 1)/2)*scale,
        x = 0.3*dx*scale, y = 0.3*dy*scale, roll/pitch = +-pi/4, yaw = +-pi. These are
        absolute pose targets, not increments.

    Observation (19,) float32 -- same slots as the reference get_obs():
        [0:3]   position_goal (x, y, z)
        [3]     yaw_rotation_goal (rad)
        [4]     yaw_diff (rad): wrapped (rotation since reset - yaw goal)
        [5]     state0: classified plane state -- the reference's classifier call is
                commented out so this is always -1.0; kept for slot parity
        [6:9]   current gripper position / scale
        [9]     desired_state
        [10:19] corner offsets from ar (back_left), / scale, in the reference's order:
                al (front_left), br (back_right), bl (front_right)

    Reward: verbatim port of the reference reward() -- position/yaw tracking of the
        reset() goals, roll/pitch penalties near the ground, reverse-motion and
        stationarity penalties, a step-budget penalty (max_steps = 6), and the
        state_reward, which only fires on "final approach" (gripper height within 0.015
        of the goal height); arriving there on the very first step is treated as a slam
        (-500 and termination), arriving on a second step after final approach terminates
        with a +300 bonus.

    step() returns (obs, reward, termination_list, truncated_list, infos) like the
        gymnasium-style reference; there is NO auto-reset -- call reset() explicitly.
    `success_score` ([0, 100], the reference's 5-component score) is computed once the
    episode terminates or truncates.
    """

    GRASP_NAMES = ("top_left", "top_middle", "top_right")
    ACTION_DURATION = 0.75  # seconds of physics per macro-action (reference mocap trajectory duration)
    START_POSE = (0.0, 0.0, 0.5)  # gripper start == the reference's box/mocap spawn pose
    OBS_DIM = 19
    # In training mode, every episode starts from one of these saved single-corner
    # checkpoints, picked at random -- so a single policy generalizes over whether the
    # left or the right corner is the grasped one. Produced by `--make_checkpoints`.
    TRAIN_CHECKPOINTS = ("top_left", "top_right")

    def __init__(self, training: bool = False, desired_state: int = 2, **env_kwargs):
        """
        training : bool
            If True, `reset()` (called with no explicit `checkpoint_path`) starts each
            episode from a randomly-chosen saved corner checkpoint -- alternating between
            `checkpoints/top_left.npz` and `checkpoints/top_right.npz` -- instead of the
            flat spawn layout. This is the intended setup for RL training: the grasped
            corner (and thus which particles the 6-DOF gripper command drives) varies per
            episode, and the reward targets `desired_state`. The checkpoints must already
            exist (generate them once with `--make_checkpoints`).
        desired_state : int
            Which target shape the reward optimizes (0 flat / 1 diagonal fold / 2 edge
            fold), fixed for the whole training run; pass it via the `--desired_state` CLI
            flag. Each `reset()` re-applies it unless overridden.
        """
        self.env = DeformableSheetEnv(**env_kwargs)
        self.action_dim = 6
        self.scale = np.float32(1.0)  # reference: hard-set to 1.0 (raw meters everywhere)
        self.max_steps = 6  # reference: the second (winning) max_steps assignment in its __init__

        self._train_checkpoints = None
        if training:
            self._train_checkpoints = [os.path.join(CHECKPOINT_DIR, f"{n}.npz") for n in self.TRAIN_CHECKPOINTS]
            missing = [p for p in self._train_checkpoints if not os.path.exists(p)]
            if missing:
                raise FileNotFoundError(
                    "training=True needs the corner checkpoints; missing "
                    f"{missing}. Generate them first with:\n"
                    "    python examples/deformable/deform_sheet_env.py --make_checkpoints"
                )

        self.reset(desired_state=desired_state)

    def reset(
        self,
        position_goal=(0.0, 0.0, 0.0),
        yaw_rotation_goal: float = 0.0,
        desired_state: int = 2,
        active_grasp_points=None,
        checkpoint_path: str | None = None,
    ):
        """Mirror of the reference reset(position_goal, yaw_rotation_goal, desired_state):
        stores the goals (yaw goal given in DEGREES, stored in radians, like the reference)
        and returns (obs, infos). The reference resets into a saved qpos where the sheet
        already hangs, settled, from the box at (0, 0, 0.5) and then holds pose for one
        0.75 s "special" step; a fresh reset here recreates that by carrying the grasp
        cluster from its spawn to START_POSE and holding there for the same duration.

        checkpoint_path : str, optional
            Start from a `DeformableSheetEnv.save_checkpoint` state instead; the gripper
            command pose is reconstructed from the held cluster's current centroid (no
            carry-to-start motion is performed). In training mode (constructed with
            `training=True`) and when this is left as None, a checkpoint is picked at
            random from `TRAIN_CHECKPOINTS` each call, so successive episodes alternate
            between the left- and right-corner start states.
        """
        # Training mode: alternate between the saved corner checkpoints unless the caller
        # explicitly forces a specific one (or a specific grasp) this episode.
        if checkpoint_path is None and active_grasp_points is None and self._train_checkpoints:
            checkpoint_path = str(np.random.choice(self._train_checkpoints))

        self._checkpoint_path = checkpoint_path
        if checkpoint_path is not None:
            self.env.load_checkpoint(checkpoint_path)
            self.active_grasp_points = (
                list(active_grasp_points) if active_grasp_points is not None else sorted(self.env._held_groups)
            )
            for name in self.active_grasp_points:
                if name not in self.env._held_groups:
                    self.env.grab_group(name)
        else:
            self.env.reset()
            # Default to the single 4-particle "top_middle" cluster: the reference's gripper
            # pins exactly 4 flex vertices near the middle of one sheet edge.
            self.active_grasp_points = (
                list(active_grasp_points) if active_grasp_points is not None else ["top_middle"]
            )
            for name in self.active_grasp_points:
                self.env.grab_group(name)

        self._cluster_idx = sorted({i for name in self.active_grasp_points for i in self.env.grasp_groups[name]})
        pos = self.env.get_particle_positions()[self._cluster_idx]
        centroid = pos.mean(axis=0)
        # Rigid template used to place the cluster at a commanded pose: particle offsets
        # from the cluster centroid, rotated by the commanded orientation.
        self._cluster_local = (pos - centroid).astype(np.float64)
        self._cmd_pos = centroid.astype(np.float64)
        self._cmd_rot = R.identity()

        self.position_goal = np.asarray(position_goal, dtype=np.float32).reshape(3)
        self.yaw_rotation_goal = float(np.deg2rad(yaw_rotation_goal))
        self.desired_state = int(desired_state)

        # Episode bookkeeping, mirroring the reference reset().
        self.step_counter = 0
        self.station_c = 0
        self.termination = [False]
        self.final_approach = False
        self.timeout = False
        self.state_reward = 0.0
        self.success_score = 0.0
        self.reward_F = 0.0
        self.added = 0.0
        # Reference quirk kept for parity: `self.action` is only ever written by reset()
        # (its step() never assigns it, and neither does the training loop), so the
        # action-dependent reward terms below always see zeros.
        self.action = np.zeros(6, dtype=np.float32)
        self.pre_z_rad = self.pre_y_rad = self.pre_x_rad = 0.0
        self.z_start, self.x_start, self.y_start = 0.5, 0.0, 0.0
        self._last_pos_err = np.zeros(3, dtype=np.float32)
        self._last_yaw_err_deg = 0.0
        self.infos = {
            "next_action": None,
            "termination_next": False,
            "episodic_reward": 0.0,
            "img_depth": None,
        }

        self._world = self.current_pose6() - self.current_pose6()  # zeros(6), like the reference

        if checkpoint_path is None:
            self._move_cluster_to(np.asarray(self.START_POSE, dtype=np.float64), R.identity())
            self.env.step(max(1, int(np.ceil(self.ACTION_DURATION / self.env.dt))))  # settle, like the reference's special step

        return self.get_obs(), self.infos

    def normalize_action(self, action):
        """Verbatim port: maps the [-1, 1] action to absolute pose targets."""
        dz, dx, dy, x_rad, y_rad, z_rad = action
        delta_z = ((dz + 1.0) / 2.0) * self.scale
        delta_x = dx * 0.3 * self.scale
        delta_y = dy * 0.3 * self.scale
        delta_x_rot = x_rad * np.pi / 4
        delta_y_rot = y_rad * np.pi / 4
        delta_z_rot = z_rad * np.pi
        return np.array([delta_z, delta_x, delta_y, delta_x_rot, delta_y_rot, delta_z_rot])

    def current_pose6(self) -> np.ndarray:
        """[x, y, z, roll, pitch, yaw] of the gripper: the commanded (virtual) pose. The
        reference reads its box geom's simulated pose instead, but that box tracks the
        mocap command to < 1e-4 m (verified empirically at the lowest commandable height),
        so command == measurement there too. Reading the command rather than the particles
        also keeps obs/reward exact when the particle placement has to be height-clamped
        near the floor (see `_set_cluster_pose`)."""
        rpy = self._cmd_rot.as_euler("xyz", degrees=False)
        return np.concatenate([self._cmd_pos, rpy]).astype(np.float64)

    def _set_cluster_pose(self, pos, rot: "R"):
        target = (np.asarray(pos, dtype=np.float64) + rot.apply(self._cluster_local)).astype(np.float32)
        # PBD floor-collision projection fights kinematic pins placed below the solver
        # particle radius and blows up the solve -- clamp the actual placement there. The
        # commanded pose (what obs/reward see) is NOT clamped, mirroring the reference,
        # where the sheet also can't physically follow the box all the way down (its pins
        # hang 1 cm below a box whose lowest command is z=0.0205).
        target[:, 2] = np.maximum(target[:, 2], self.env.solver_particle_radius + 1e-4)
        self.env.sheet.set_particles_pos(target, particles_idx_local=self._cluster_idx)

    def _move_cluster_to(self, p_end, r_end: "R"):
        """Carry the cluster to an absolute pose over ACTION_DURATION seconds of physics:
        smoothstep (3t^2 - 2t^3) position + orientation slerped linearly in time, the same
        trajectory profile the reference's mocap follows."""
        p_start = self._cmd_pos.copy()
        p_end = np.asarray(p_end, dtype=np.float64)
        n_steps = max(1, int(np.ceil(self.ACTION_DURATION / self.env.dt)))
        slerp = Slerp([0.0, 1.0], R.from_quat(np.stack([self._cmd_rot.as_quat(), r_end.as_quat()])))
        for k in range(1, n_steps + 1):
            t = k / n_steps
            s = 3 * t**2 - 2 * t**3
            self._set_cluster_pose((1 - s) * p_start + s * p_end, slerp(t))
            self.env.step()
        self._cmd_pos = p_end
        self._cmd_rot = r_end

    def get_obs(self) -> np.ndarray:
        """The reference's 19-D observation vector, slot for slot (scale = 1.0)."""
        current_pose6 = self.current_pose6()

        init_yaw = float(self._world[5])
        curr_yaw = float(current_pose6[5])
        rotate_deg = np.rad2deg(curr_yaw - init_yaw)
        yaw_goal_deg = np.rad2deg(float(self.yaw_rotation_goal))
        yaw_diff = np.deg2rad(((rotate_deg - yaw_goal_deg + 180.0) % 360.0) - 180.0)

        ar, al, bl, br = self.env.get_corners()
        scale = float(self.scale) if self.scale else 1.0
        obs_al = (al - ar) / scale
        obs_br = (br - ar) / scale
        obs_bl = (bl - ar) / scale

        # Reference: `self.state` is never assigned (its classifier call is commented
        # out), so the state slot is the -1.0 fallback every step.
        state0 = -1.0

        return np.array(
            [
                float(self.position_goal[0]),
                float(self.position_goal[1]),
                float(self.position_goal[2]),
                float(self.yaw_rotation_goal),
                float(yaw_diff),
                state0,
                float(current_pose6[0] / scale),
                float(current_pose6[1] / scale),
                float(current_pose6[2] / scale),
                float(self.desired_state),
                # Reference corner order: al (front_left), br (back_right), bl (front_right).
                float(obs_al[0]), float(obs_al[1]), float(obs_al[2]),
                float(obs_br[0]), float(obs_br[1]), float(obs_br[2]),
                float(obs_bl[0]), float(obs_bl[1]), float(obs_bl[2]),
            ],
            dtype=np.float32,
        )

    def reward(self) -> float:
        """Verbatim port of the reference reward()."""
        current_pose6 = self.current_pose6()

        xx = self.action[0]
        yy = self.action[1]
        zz = self.action[2]

        if (np.sqrt((self.z_delta**2) + (self.x_delta**2) + (self.y_delta**2)) < 0.15) and self.action[0] > -0.89:
            self.station_c += 1
            stationary_rew = -300 * self.station_c
        else:
            stationary_rew = 0.0

        non_centric_traj = -(150 * ((xx**2) + (1.2 * xx) + 0.2) * (abs(np.sqrt((abs(yy) ** 2) + (abs(zz) ** 2))) - 0.45) * 30)

        move = current_pose6 - self._world

        init_yaw = self._world[5]
        curr_yaw = current_pose6[5]
        rotate_deg = np.rad2deg(curr_yaw - init_yaw)

        pos_err = move[:3] - self.position_goal

        # Reference quirk kept verbatim: rotate_deg (degrees) minus yaw_rotation_goal
        # (radians) -- exact for the yaw_rotation_goal = 0.0 the training loop always passes.
        yaw_diff_deg = ((rotate_deg - self.yaw_rotation_goal + 180) % 360) - 180

        roll_current = current_pose6[3]
        pitch_current = current_pose6[4]

        yaw_err = yaw_diff_deg

        if ((pos_err[1] / self.scale) ** 2 + (pos_err[0] / self.scale) ** 2) < 0.015 and abs(pos_err[2] / self.scale) < 0.01:
            if self.desired_state == 0:
                self.infos["next_action"] = "None"
            self.infos["termination_next"] = True

        if self.z_delta > 0.0:
            reverse_mv = -1000 * abs(self.z_delta)
        else:
            reverse_mv = 0

        pos_reward = -np.sqrt((pos_err[0] / self.scale) ** 2 + (pos_err[1] / self.scale) ** 2 + (pos_err[2] / self.scale) ** 2) * 100

        if abs(pos_err[2]) < 0.08:
            roll_pen = -((5 * roll_current) ** 2)
            pitch_pen = -((5 * pitch_current) ** 2)
        else:
            roll_pen = 0
            pitch_pen = 0
            pos_reward = 5 * pos_reward

        yaw_reward = -20 * (np.abs(yaw_err))

        added = 0.0
        if abs(pos_err[2]) / self.scale < 0.015 and self.step_counter == 1:
            self.state_reward = -500
            self.termination = [True]

        elif abs(pos_err[2]) / self.scale < 0.015 and self.step_counter > 1:
            pos_reward = pos_reward * 60
            if self.final_approach:
                added = 300
                self.termination = [True]
            self.final_approach = True

            ar, al, bl, br = self.env.get_corners()

            if self.desired_state == 0:
                d_diag = np.linalg.norm(al - br) / self.scale
                d_diag2 = np.linalg.norm(ar - bl) / self.scale
                d_top = np.linalg.norm(al - ar) / self.scale
                d_r = np.linalg.norm(ar - br) / self.scale

                area_one = d_top * d_r
                area_two = (d_diag * d_diag2) / 2.0

                x_a = abs(area_one) / 0.0225
                x_b = abs(area_two) / 0.0225

                y_1 = 10 * np.exp(-8 * ((x_a - 1) ** 2))
                y_2 = 10 * np.exp(-8 * ((x_b - 1) ** 2))

                self.state_reward = y_1 * y_2
                self.state_reward = self.state_reward * 4

            if self.desired_state == 1:
                d_fold = np.linalg.norm(ar - bl) / self.scale
                sigma = 0.05
                ref = 0.20
                gauss = np.exp(-0.5 * (d_fold / sigma) ** 2)
                linear = float(np.clip(1.0 - d_fold / ref, 0.0, 1.0))
                self.state_reward = gauss * 350.0 + linear * 50.0

            if self.desired_state == 2:
                scale = float(self.scale) if self.scale else 1.0
                d_xy_a = np.linalg.norm((ar - al)[:2]) / scale
                d_xy_b = np.linalg.norm((br - bl)[:2]) / scale
                sigma = 0.04
                score_a = np.exp(-0.5 * (d_xy_a / sigma) ** 2)
                score_b = np.exp(-0.5 * (d_xy_b / sigma) ** 2)
                fold_gauss = score_a * score_b
                ref = 0.15
                approach_a = float(np.clip(1.0 - d_xy_a / ref, 0.0, 1.0))
                approach_b = float(np.clip(1.0 - d_xy_b / ref, 0.0, 1.0))
                fold_linear = approach_a * approach_b
                self.state_reward = fold_gauss * 350.0 + fold_linear * 50.0

        else:
            self.state_reward = 0.0

        step_rew = (self.step_counter / self.max_steps) * -900

        self._last_pos_err = pos_err.copy()
        self._last_yaw_err_deg = float(yaw_err)

        self.reward_F = (
            pos_reward * 0.5
            + yaw_reward * 0.25
            + 9 * self.state_reward
            + step_rew
            + roll_pen
            + pitch_pen
            + reverse_mv
            + added
            + non_centric_traj
            + stationary_rew
        )
        return self.reward_F

    def compute_success_score(self) -> float:
        """Verbatim port of the reference's unified success score [0-100] at episode end:
        50% deformation quality (state_reward / 400), 25% position accuracy (Gaussian,
        sigma = 0.085), 15% yaw alignment, 5% step efficiency, 5% geometric coherence."""
        scale = float(self.scale) if self.scale else 1.0

        MAX_STATE_REWARD = 400.0
        state_score = float(np.clip(self.state_reward / MAX_STATE_REWARD, 0.0, 1.0))

        pos_err = getattr(self, "_last_pos_err", np.zeros(3))
        pos_dist = float(np.sqrt((pos_err[0] / scale) ** 2 + (pos_err[1] / scale) ** 2 + (pos_err[2] / scale) ** 2))
        pos_score = float(np.exp(-0.5 * (pos_dist / 0.085) ** 2))

        yaw_err_deg = abs(float(getattr(self, "_last_yaw_err_deg", 0.0)))
        yaw_score = float(np.clip(1.0 - yaw_err_deg / 90.0, 0.0, 1.0))

        eff_score = float(np.clip(1.0 - self.step_counter / max(self.max_steps, 1), 0.0, 1.0))

        ar, al, bl, br = self.env.get_corners()
        if self.desired_state == 0:
            z_span = (max(ar[2], al[2], bl[2], br[2]) - min(ar[2], al[2], bl[2], br[2])) / scale
            geom_score = float(np.exp(-0.5 * (z_span / 0.04) ** 2))
        elif self.desired_state == 1:
            d_fold = np.linalg.norm(ar - bl) / scale
            d_other = np.linalg.norm(al - br) / scale
            asymmetry = float(np.clip((d_other / (d_fold + 1e-6) - 1.0) / 3.0, 0.0, 1.0))
            proximity = float(np.exp(-0.5 * (d_fold / 0.05) ** 2))
            geom_score = 0.5 * asymmetry + 0.5 * proximity
        elif self.desired_state == 2:
            d_xy_a = np.linalg.norm((ar - al)[:2]) / scale
            d_xy_b = np.linalg.norm((br - bl)[:2]) / scale
            fold_score = float(np.exp(-0.5 * (d_xy_a / 0.04) ** 2) * np.exp(-0.5 * (d_xy_b / 0.04) ** 2))
            z_span = (max(ar[2], al[2], bl[2], br[2]) - min(ar[2], al[2], bl[2], br[2])) / scale
            layer_score = float(np.clip(z_span / 0.08, 0.0, 1.0))
            geom_score = 0.6 * fold_score + 0.4 * layer_score
        else:
            geom_score = 0.0

        score = (
            0.50 * state_score + 0.25 * pos_score + 0.15 * yaw_score + 0.05 * eff_score + 0.05 * geom_score
        ) * 100.0
        return float(np.clip(score, 0.0, 100.0))

    def truncation(self):
        """Reference truncation: past the step budget, or gripper essentially at the floor."""
        if self.step_counter > self.max_steps or (self.current_pose6()[2] / self.scale) < 0.02:
            return [True]
        return [False]

    def step(self, action):
        action = np.asarray(action, dtype=np.float32).reshape(-1).copy()
        if action.shape != (self.action_dim,):
            raise ValueError(f"action must have shape ({self.action_dim},), got {action.shape}")

        action[0] = action[0] * 0.959
        self.step_counter += 1

        act = self.normalize_action(action)
        dz, dx, dy, x_rad, y_rad, z_rad = act

        self.diff_z = z_rad - self.pre_z_rad
        self.diff_y = y_rad - self.pre_y_rad
        self.diff_x = x_rad - self.pre_x_rad

        self.z_delta = dz - self.z_start
        self.x_delta = dx - self.x_start
        self.y_delta = dy - self.y_start

        self.pre_z_rad = z_rad
        self.pre_y_rad = y_rad
        self.pre_x_rad = x_rad

        self.z_start = dz
        self.x_start = dx
        self.y_start = dy

        self._move_cluster_to(
            np.array([dx, dy, dz], dtype=np.float64),
            R.from_euler("xyz", [x_rad, y_rad, z_rad], degrees=False),
        )

        obs = self.get_obs()
        reward = self.reward()
        truncated = self.truncation()
        self.infos["episodic_reward"] += reward

        if bool(np.asarray(self.termination).any()) or bool(np.asarray(truncated).any()):
            self.success_score = self.compute_success_score()

        return obs, reward, self.termination, truncated, self.infos


def _lerp_groups(env: DeformableSheetEnv, names, z_start: float, z_end: float, n_steps: int):
    for k in range(1, n_steps + 1):
        z = z_start + (z_end - z_start) * (k / n_steps)
        for name in names:
            env.move_group(name, [0.0, 0.0, z])
        env.step()


def _lerp_pose(env: DeformableSheetEnv, names, pose_start, pose_end, n_steps: int):
    """Linearly interpolate all `names` grasp points from pose_start to pose_end (each a
    (dz, dx, dy) tuple) over n_steps physics steps."""
    dz0, dx0, dy0 = pose_start
    dz1, dx1, dy1 = pose_end
    for k in range(1, n_steps + 1):
        frac = k / n_steps
        dz = dz0 + (dz1 - dz0) * frac
        dx = dx0 + (dx1 - dx0) * frac
        dy = dy0 + (dy1 - dy0) * frac
        for name in names:
            env.move_group(name, [dx, dy, dz])
        env.step()


def run_rl_random_policy_demo(vis: bool, cpu: bool, desired_state: int, n_steps: int):
    """Smoke-test DeformSheetTaskEnv's step(action)/reset() API with a random policy."""
    task_env = DeformSheetTaskEnv(vis=vis, cpu=cpu)
    obs, infos = task_env.reset(desired_state=desired_state)
    print("obs (reset):", obs)
    for i in range(n_steps):
        action = np.random.uniform(-1.0, 1.0, size=task_env.action_dim).astype(np.float32)
        obs, reward, termination, truncated, infos = task_env.step(action)
        print(
            f"step {i}: reward={reward:.2f} state_reward={task_env.state_reward:.2f} "
            f"termination={termination} truncated={truncated}"
        )
        if any(termination) or any(truncated):
            print(
                f"  -> episode ended at step {i} (success_score={task_env.success_score:.1f}, "
                f"episodic_reward={infos['episodic_reward']:.1f}), resetting"
            )
            obs, infos = task_env.reset(desired_state=desired_state)


def run_training_setup(vis: bool, cpu: bool, desired_state: int, n_demo_episodes: int = 2):
    """Build a DeformSheetTaskEnv configured for RL training on a single `desired_state`,
    and hand back a ready-to-drive step()/reset() interface. This is CONFIG-ONLY: it sets
    the env up (alternating corner-checkpoint starts, reward targeting `desired_state`) but
    does NOT run any RL algorithm -- plug the returned env into your own trainer (the same
    role DeformEnv plays for SAC_MOE in the MuJoCo workspace).

    Every `reset()` starts the episode from a randomly chosen saved corner checkpoint
    (top_left.npz / top_right.npz), so the grasped corner alternates across episodes. Runs
    a short random-action rollout first so the alternation and the
    (obs, reward, termination, truncated, infos) contract are visible before you attach a
    policy.
    """
    task_env = DeformSheetTaskEnv(training=True, desired_state=desired_state, vis=vis, cpu=cpu)

    print("=" * 72)
    print("DeformSheetTaskEnv configured for training (config-only -- no RL algo is run)")
    print(f"  desired_state     : {desired_state}  (0 flat / 1 diagonal fold / 2 edge fold)")
    print(f"  obs_dim           : {task_env.OBS_DIM}")
    print(f"  action_dim        : {task_env.action_dim}   (dz, dx, dy, x_rad, y_rad, z_rad), each in [-1, 1]")
    print(f"  max_steps/episode : {task_env.max_steps}")
    print(f"  episode start     : random.choice({list(task_env.TRAIN_CHECKPOINTS)}) checkpoint (alternating corner)")
    print("  reward            : DeformSheetTaskEnv.reward()  (1:1 port of the MuJoCo reference)")
    print("  drive it with     : obs, infos = env.reset();  obs, r, term, trunc, infos = env.step(action)")
    print("=" * 72)

    for ep in range(n_demo_episodes):
        obs, infos = task_env.reset()
        held = sorted(task_env.env._held_groups)
        i = 0
        reward = 0.0
        for i in range(task_env.max_steps + 1):
            action = np.random.uniform(-1.0, 1.0, size=task_env.action_dim).astype(np.float32)
            obs, reward, termination, truncated, infos = task_env.step(action)
            if any(termination) or any(truncated):
                break
        print(
            f"[demo episode {ep}] start={os.path.basename(str(task_env._checkpoint_path))} held={held} "
            f"-> ended step {i}: reward={reward:.1f} state_reward={task_env.state_reward:.2f} "
            f"success_score={task_env.success_score:.1f}"
        )

    return task_env


def run_pose_sequence_demo(
    vis: bool,
    cpu: bool,
    record: bool,
    grasp_points,
    desired_state: int,
    hold_seconds: float,
    transition_steps: int = 200,
    checkpoint_path: str | None = None,
):
    """Move the selected grasp point(s) through a fixed sequence of 3 poses -- dz=0.0, 0.25,
    0.5 (dx=dy=0 for all 3) -- linearly interpolating over `transition_steps` physics steps
    between each pose (instead of snapping instantly), then holding for `hold_seconds` once
    each pose is reached. Poses are given (dz, dx, dy), the same ordering as
    DeformSheetTaskEnv's per-point action slots (dz first); note this is the reverse of
    `move_group`'s own `[dx, dy, dz]` argument order, so each pose is reordered before being
    passed through.

    If `checkpoint_path` is given, saves a checkpoint (env.save_checkpoint) right after the
    final pose's hold completes -- i.e. the settled state at dz=0.5 -- before releasing.
    """
    env = DeformableSheetEnv(vis=vis, record=record, cpu=cpu)
    poses_dz_dx_dy = [(0.0, 0.0, 0.0), (0.25, 0.0, 0.0), (0.5, 0.0, 0.0)]
    hold_steps = max(1, int(round(hold_seconds / env.dt)))

    for name in grasp_points:
        env.grab_group(name)

    prev_pose = (0.0, 0.0, 0.0)
    for i, pose in enumerate(poses_dz_dx_dy):
        _lerp_pose(env, grasp_points, prev_pose, pose, transition_steps)
        prev_pose = pose
        env.step(hold_steps)
        dz, dx, dy = pose
        print(
            f"pose dz={dz} dx={dx} dy={dy}: is_placed={env.is_placed()} "
            f"state_reward={env.compute_state_reward(desired_state)} obs={env.get_obs(desired_state)}"
        )
        if checkpoint_path is not None and i == len(poses_dz_dx_dy) - 1:
            env.save_checkpoint(checkpoint_path)
            print(f"saved checkpoint to {checkpoint_path}")

    env.release_all_groups()

    if env.cam is not None:
        env.cam.stop_recording(save_to_filename="deform_sheet_pose_demo.mp4", fps=1 / env.dt)
        print("Saved video to deform_sheet_pose_demo.mp4")


def generate_all_checkpoints(cpu: bool, desired_state: int, hold_seconds: float, transition_steps: int):
    """Run the pose sequence demo once per grasp point (top_left, top_middle, top_right --
    each grabbed alone, the other two left free), saving a checkpoint of the settled dz=0.5
    state for each to CHECKPOINT_DIR/<name>.npz. Use DeformSheetTaskEnv.reset(checkpoint_path=
    ...) to start training from one of these instead of the flat spawn layout."""
    for name in ("top_left", "top_middle", "top_right"):
        checkpoint_path = os.path.join(CHECKPOINT_DIR, f"{name}.npz")
        print(f"=== generating checkpoint for grasp point '{name}' -> {checkpoint_path} ===")
        run_pose_sequence_demo(
            vis=False,
            cpu=cpu,
            record=False,
            grasp_points=[name],
            desired_state=desired_state,
            hold_seconds=hold_seconds,
            transition_steps=transition_steps,
            checkpoint_path=checkpoint_path,
        )


def run_checkpoint_viewer(vis: bool, cpu: bool, checkpoint_path: str, view_seconds: float, record: bool):
    """Load a checkpoint saved by save_checkpoint/--make_checkpoints and just hold/view it
    (the checkpoint's grasp point stays kinematically held at its saved position -- nothing
    else moves it) for `view_seconds`, so you can see what that checkpoint actually captured."""
    env = DeformableSheetEnv(vis=vis, cpu=cpu, record=record)
    env.load_checkpoint(checkpoint_path)
    print(f"loaded checkpoint '{checkpoint_path}', held grasp point(s): {sorted(env._held_groups)}")
    view_steps = max(1, int(round(view_seconds / env.dt)))
    env.step(view_steps)

    if env.cam is not None:
        env.cam.stop_recording(save_to_filename="deform_sheet_checkpoint_view.mp4", fps=1 / env.dt)
        print("Saved video to deform_sheet_checkpoint_view.mp4")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vis", "-v", action="store_true", default=False)
    parser.add_argument("--cpu", action="store_true", default=False)
    parser.add_argument("--record", action="store_true", default=False)
    parser.add_argument("--lift_height", type=float, default=0.5)
    parser.add_argument("--lift_seconds", type=float, default=1.0)
    parser.add_argument("--hold_seconds", type=float, default=2.0)
    parser.add_argument(
        "--grasp_points",
        nargs="+",
        choices=["top_left", "top_middle", "top_right"],
        default=["top_left", "top_middle", "top_right"],
        help="Which grasp point(s) to hold and lift. Any subset not selected is left free "
        "(so it hangs/sags under gravity instead of moving rigidly with the rest).",
    )
    parser.add_argument(
        "--desired_state",
        type=int,
        choices=[0, 1, 2],
        default=2,
        help="Target shape for the corner-based reward: 0=flat/square, 1=single-diagonal fold, "
        "2=edge-to-edge fold in half.",
    )
    parser.add_argument(
        "--settle_seconds",
        type=float,
        default=1.0,
        help="After releasing, how long to let the sheet fall/settle before checking "
        "is_placed()/reward/success -- the reward is gated to 0 until it actually comes to rest.",
    )
    parser.add_argument(
        "--rl_demo",
        action="store_true",
        default=False,
        help="Instead of the scripted lift/fold demo, roll out DeformSheetTaskEnv with a "
        "random policy for --rl_steps steps to smoke-test the step(action)/reset() RL API.",
    )
    parser.add_argument("--rl_steps", type=int, default=100)
    parser.add_argument(
        "--training",
        action="store_true",
        default=False,
        help="Build DeformSheetTaskEnv set up for RL training on a single --desired_state: "
        "every episode starts from a randomly chosen saved corner checkpoint (top_left / "
        "top_right, alternating), with the reward targeting --desired_state. Config-only -- "
        "it prints the step()/reset() interface and runs a short random rollout to show the "
        "alternation, then leaves the env ready for your own trainer (no RL algo is run). "
        "Requires the checkpoints to exist (see --make_checkpoints).",
    )
    parser.add_argument(
        "--action_demo",
        action="store_true",
        default=False,
        help="Instead of the scripted lift/fold demo, hold the selected --grasp_points at a "
        "fixed 3-pose sequence (dz=0, 0.25, 0.5; dx=dy=0), each for --hold_seconds.",
    )
    parser.add_argument(
        "--transition_steps",
        type=int,
        default=200,
        help="With --action_demo, how many physics steps to linearly interpolate over when "
        "moving from one pose to the next (instead of snapping instantly).",
    )
    parser.add_argument(
        "--make_checkpoints",
        action="store_true",
        default=False,
        help="Instead of any demo, generate 3 checkpoints (one per grasp point -- top_left, "
        "top_middle, top_right -- each grabbed alone) by running the pose sequence up to "
        "dz=0.5 with --hold_seconds at each pose, and save the settled final state of each "
        "to examples/deformable/checkpoints/<name>.npz. Always headless (no --vis).",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="Path to a checkpoint saved by --make_checkpoints (e.g. "
        "examples/deformable/checkpoints/top_middle.npz). Loads that state (instead of the "
        "flat spawn layout) and just holds/views it for --view_seconds -- use with --vis.",
    )
    parser.add_argument("--view_seconds", type=float, default=10.0)
    args = parser.parse_args()

    if args.rl_demo:
        run_rl_random_policy_demo(
            vis=args.vis, cpu=args.cpu, desired_state=args.desired_state, n_steps=args.rl_steps
        )
        return

    if args.training:
        run_training_setup(vis=args.vis, cpu=args.cpu, desired_state=args.desired_state)
        return

    if args.make_checkpoints:
        generate_all_checkpoints(
            cpu=args.cpu,
            desired_state=args.desired_state,
            hold_seconds=args.hold_seconds,
            transition_steps=args.transition_steps,
        )
        return

    if args.checkpoint is not None:
        run_checkpoint_viewer(
            vis=args.vis, cpu=args.cpu, checkpoint_path=args.checkpoint, view_seconds=args.view_seconds, record=args.record
        )
        return

    if args.action_demo:
        run_pose_sequence_demo(
            vis=args.vis,
            cpu=args.cpu,
            record=args.record,
            grasp_points=args.grasp_points,
            desired_state=args.desired_state,
            hold_seconds=args.hold_seconds,
            transition_steps=args.transition_steps,
        )
        return

    env = DeformableSheetEnv(vis=args.vis, record=args.record, cpu=args.cpu)

    print("obs (start):", env.get_obs(args.desired_state))
    print(f"state_reward (start, desired_state={args.desired_state}):", env.compute_state_reward(args.desired_state))

    groups = args.grasp_points
    for name in groups:
        env.grab_group(name)

    lift_steps = max(1, int(round(args.lift_seconds / env.dt)))
    hold_steps = max(1, int(round(args.hold_seconds / env.dt)))
    settle_steps = max(1, int(round(args.settle_seconds / env.dt)))

    # Go up to `lift_height`, wait, come back down -- the selected grasp point(s) move together.
    _lerp_groups(env, groups, 0.0, args.lift_height, lift_steps)
    env.step(hold_steps)
    print(f"is_placed (at hold): {env.is_placed()}")
    print(f"state_reward (at hold, desired_state={args.desired_state}):", env.compute_state_reward(args.desired_state))
    _lerp_groups(env, groups, args.lift_height, 0.0, lift_steps)

    env.release_all_groups()
    env.step(settle_steps)

    print(f"is_placed (after release+settle): {env.is_placed()}")
    print("obs (final):", env.get_obs(args.desired_state))
    print(f"state_reward (final, desired_state={args.desired_state}):", env.compute_state_reward(args.desired_state))
    print(f"success_score (final, desired_state={args.desired_state}):", env.compute_success_score(args.desired_state))

    if env.cam is not None:
        env.cam.stop_recording(save_to_filename="deform_sheet_demo.mp4", fps=1 / env.dt)
        print("Saved video to deform_sheet_demo.mp4")


if __name__ == "__main__":
    main()

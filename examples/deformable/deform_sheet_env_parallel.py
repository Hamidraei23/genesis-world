"""
Batched (n_envs) GPU port of DeformSheetTaskEnv for large-scale training.

One Genesis scene built with ``scene.build(n_envs=B)`` steps all B environments in a
single batched PBD solve; observations, rewards, terminations and resets are computed
as torch tensors on the GPU. The observation layout, action semantics, reward function
and every constant are the same 1:1 MuJoCo-reference port as the single-env
DeformSheetTaskEnv in deform_sheet_env.py -- see that class's docstring for the full
mapping; this file only vectorizes it.

Episode starts (training use): each env independently starts every episode from one of
the two saved corner checkpoints (checkpoints/top_left.npz / top_right.npz), chosen at
random per env per episode, so at any moment roughly half the batch is grasping the
left corner and half the right. Per-env grasping uses the PBD API's `envs_idx` support
(fix_particles / release_particle / set_particles_pos are all per-env here).

step() auto-resets done envs and returns the POST-reset observation in their slot.
This is safe for the reference's replay convention (done = termination OR truncation
stored in the buffer): the TD target multiplies the bootstrap term by (1 - done), so
next_obs is never read at exactly the transitions where it holds a reset state.

Notes vs the single-env class:
  - The gripper pose read by obs/reward is the commanded pose (identical to the
    single-env class; verified there to equal the reference's measured box pose).
  - The commanded orientation is tracked as (roll, pitch, yaw) directly; quaternions
    are only used internally for the 0.75 s slerp trajectory.
  - No scripted-demo API (grab/lift/etc.) -- this class is only the RL task.
"""

import os

import numpy as np
import torch
import genesis as gs

try:
    from .deform_sheet_env import CHECKPOINT_DIR, _grid_obj_path, build_grid_mesh_obj
except ImportError:
    from deform_sheet_env import CHECKPOINT_DIR, _grid_obj_path, build_grid_mesh_obj


# ----------------- Torch quaternion helpers (wxyz) -----------------


def euler_xyz_to_quat(rpy: torch.Tensor) -> torch.Tensor:
    """Extrinsic xyz euler (scipy R.from_euler('xyz', ...) convention, R = Rz @ Ry @ Rx)
    to quaternion (w, x, y, z). rpy: (B, 3) -> (B, 4)."""
    half = 0.5 * rpy
    cr, sr = torch.cos(half[:, 0]), torch.sin(half[:, 0])
    cp, sp = torch.cos(half[:, 1]), torch.sin(half[:, 1])
    cy, sy = torch.cos(half[:, 2]), torch.sin(half[:, 2])
    # q = qz * qy * qx
    w = cy * cp * cr + sy * sp * sr
    x = cy * cp * sr - sy * sp * cr
    y = cy * sp * cr + sy * cp * sr
    z = sy * cp * cr - cy * sp * sr
    return torch.stack([w, x, y, z], dim=-1)


def quat_apply(q: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """Rotate vectors v (B, N, 3) by quaternions q (B, 4) (w, x, y, z)."""
    qw = q[:, None, 0:1]
    qv = q[:, None, 1:4]
    t = 2.0 * torch.cross(qv.expand_as(v), v, dim=-1)
    return v + qw * t + torch.cross(qv.expand_as(v), t, dim=-1)


def quat_slerp(q0: torch.Tensor, q1: torch.Tensor, t: float) -> torch.Tensor:
    """Spherical interpolation between (B, 4) quaternion batches at scalar t."""
    dot = (q0 * q1).sum(dim=-1, keepdim=True)
    q1 = torch.where(dot < 0.0, -q1, q1)
    dot = dot.abs().clamp(max=1.0)
    theta = torch.acos(dot)
    sin_theta = torch.sin(theta)
    near = sin_theta < 1e-6
    w0 = torch.where(near, 1.0 - t, torch.sin((1.0 - t) * theta) / (sin_theta + 1e-12))
    w1 = torch.where(near, torch.full_like(sin_theta, t), torch.sin(t * theta) / (sin_theta + 1e-12))
    out = w0 * q0 + w1 * q1
    return out / out.norm(dim=-1, keepdim=True).clamp_min(1e-12)


class DeformSheetTaskEnvParallel:
    """Batched DeformSheetTaskEnv: same constants and reward as the single-env port."""

    ACTION_DURATION = 0.75  # seconds of physics per macro-action
    OBS_DIM = 19
    ACTION_DIM = 6
    TRAIN_CHECKPOINTS = ("top_left", "top_right")

    def __init__(
        self,
        num_envs: int,
        desired_state: int = 2,
        n_side: int = 5,
        spacing: float = 0.05,
        particle_size: float = 0.0543,
        sheet_height: float = 0.25,
        vis: bool = False,
        cpu: bool = False,
    ):
        if not gs._initialized:
            gs.init(backend=gs.cpu if cpu else gs.gpu, precision="32", logging_level="warning")

        self.num_envs = int(num_envs)
        self.desired_state = int(desired_state)
        self.device = gs.device
        self.dt = 2e-2
        self.scale = 1.0  # reference: hard-set to 1.0
        self.max_steps = 6
        self.solver_particle_radius = particle_size / 2.0
        self._traj_steps = max(1, int(np.ceil(self.ACTION_DURATION / self.dt)))

        obj_path = _grid_obj_path(n_side, spacing)
        build_grid_mesh_obj(obj_path, n_side=n_side, spacing=spacing)

        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=self.dt, substeps=10),
            pbd_options=gs.options.PBDOptions(particle_size=particle_size),
            viewer_options=gs.options.ViewerOptions(
                camera_pos=(0.5, 0.5, 0.5), camera_lookat=(0.0, 0.0, 0.1), camera_fov=35, max_FPS=60
            ),
            show_viewer=vis,
        )
        self.scene.add_entity(gs.morphs.Plane())
        self.sheet = self.scene.add_entity(
            material=gs.materials.PBD.Cloth(stretch_compliance=1e-3, bending_compliance=2e-3),
            morph=gs.morphs.Mesh(file=obj_path, pos=(0.0, 0.0, sheet_height)),
            surface=gs.surfaces.Default(color=(0.75, 0.25, 0.25, 1.0), vis_mode="particle"),
        )
        self.scene.build(n_envs=self.num_envs)

        assert self.sheet.n_particles == n_side * n_side, (
            f"expected {n_side * n_side} particles, got {self.sheet.n_particles}"
        )

        # --- Particle lookup (same scheme as the single-env class) ---
        half = (n_side - 1) * spacing / 2.0

        def grid_world_pos(i, j):
            return (i * spacing - half, j * spacing - half, sheet_height)

        top_row, n = n_side - 1, n_side
        target_labels = {
            "front_left": (0, 0),
            "front_right": (0, n - 1),
            "back_left": (n - 1, 0),
            "back_right": (n - 1, n - 1),
            "top_left_corner": (top_row, 0),
            "top_left_edge": (top_row, 1),
            "top_left_inward": (top_row - 1, 0),
            "top_right_corner": (top_row, n - 1),
            "top_right_edge": (top_row, n - 2),
            "top_right_inward": (top_row - 1, n - 1),
        }
        lut = {}
        for name, (i, j) in target_labels.items():
            found = self.sheet.find_closest_particle(grid_world_pos(i, j))
            if hasattr(found, "detach"):
                found = found.detach().cpu().numpy()
            lut[name] = int(np.asarray(found).reshape(-1)[0])  # identical across envs at spawn

        self.corner_idx = {k: lut[k] for k in ("front_left", "front_right", "back_left", "back_right")}
        self.grasp_groups = {
            "top_left": [lut["top_left_corner"], lut["top_left_edge"], lut["top_left_inward"]],
            "top_right": [lut["top_right_corner"], lut["top_right_edge"], lut["top_right_inward"]],
        }

        # --- Checkpoint states (one per corner), shared templates for every env ---
        self._ckpt = {}
        for name in self.TRAIN_CHECKPOINTS:
            path = os.path.join(CHECKPOINT_DIR, f"{name}.npz")
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"missing {path}; generate with: python examples/deformable/deform_sheet_env.py --make_checkpoints"
                )
            data = np.load(path, allow_pickle=True)
            pos = torch.as_tensor(data["pos"], dtype=torch.float32, device=self.device)
            vel = torch.as_tensor(data["vel"], dtype=torch.float32, device=self.device)
            group = self.grasp_groups[name]
            cluster_pos = pos[group]
            centroid = cluster_pos.mean(dim=0)
            self._ckpt[name] = {
                "pos": pos,  # (25, 3)
                "vel": vel,
                "group": torch.as_tensor(group, dtype=torch.long, device=self.device),
                "centroid": centroid,  # (3,)
                "local": cluster_pos - centroid,  # (3, 3) rigid template
            }

        B = self.num_envs
        dev = self.device
        self._all_envs = torch.arange(B, device=dev)
        # Per-env episode state
        self.active_left = torch.zeros(B, dtype=torch.bool, device=dev)  # True -> top_left grasped
        self._cmd_pos = torch.zeros(B, 3, device=dev)
        self._cmd_rpy = torch.zeros(B, 3, device=dev)
        self._cluster_local = torch.zeros(B, 3, 3, device=dev)
        self.step_counter = torch.zeros(B, dtype=torch.long, device=dev)
        self.termination = torch.zeros(B, dtype=torch.bool, device=dev)
        self.final_approach = torch.zeros(B, dtype=torch.bool, device=dev)
        self.station_c = torch.zeros(B, device=dev)
        self.state_reward = torch.zeros(B, device=dev)
        self.success_score = torch.zeros(B, device=dev)
        self.episodic_reward = torch.zeros(B, device=dev)
        self.z_start = torch.full((B,), 0.5, device=dev)
        self.x_start = torch.zeros(B, device=dev)
        self.y_start = torch.zeros(B, device=dev)
        self.z_delta = torch.zeros(B, device=dev)
        self.x_delta = torch.zeros(B, device=dev)
        self.y_delta = torch.zeros(B, device=dev)
        self._last_pos_err = torch.zeros(B, 3, device=dev)
        self._last_yaw_err_deg = torch.zeros(B, device=dev)

        self.position_goal = torch.zeros(3, device=dev)
        self.yaw_rotation_goal = 0.0  # radians (reference: deg2rad of the reset arg, default 0)

        self._fixed_once = False
        self.reset()

    # ----------------- Reset -----------------

    def reset(self, envs_idx: torch.Tensor | None = None) -> torch.Tensor:
        """Reset the given envs (all if None) to a random corner checkpoint. Returns obs (B, 19)."""
        if envs_idx is None:
            envs_idx = self._all_envs
        if envs_idx.numel() == 0:
            return self.get_obs()

        choose_left = torch.rand(envs_idx.shape[0], device=self.device) < 0.5
        self.active_left[envs_idx] = choose_left

        left_ck, right_ck = self._ckpt["top_left"], self._ckpt["top_right"]

        # Release both groups for the resetting envs, then re-fix the chosen one per env.
        # (release_particle is a no-op on particles that aren't currently fixed.)
        for ck in (left_ck, right_ck):
            self.sheet.release_particle(ck["group"], envs_idx=envs_idx)

        # Particle state: per-env pick of the two checkpoint layouts.
        pos = torch.where(choose_left[:, None, None], left_ck["pos"][None], right_ck["pos"][None])
        vel = torch.where(choose_left[:, None, None], left_ck["vel"][None], right_ck["vel"][None])
        self.sheet.set_particles_pos(pos, envs_idx=envs_idx)
        self.sheet.set_particles_vel(vel, envs_idx=envs_idx)

        left_envs = envs_idx[choose_left]
        right_envs = envs_idx[~choose_left]
        if left_envs.numel():
            self.sheet.fix_particles(left_ck["group"], envs_idx=left_envs)
        if right_envs.numel():
            self.sheet.fix_particles(right_ck["group"], envs_idx=right_envs)

        # Gripper command pose: reconstructed from the (deterministic) checkpoint cluster.
        self._cmd_pos[envs_idx] = torch.where(choose_left[:, None], left_ck["centroid"][None], right_ck["centroid"][None])
        self._cmd_rpy[envs_idx] = 0.0
        self._cluster_local[envs_idx] = torch.where(
            choose_left[:, None, None], left_ck["local"][None], right_ck["local"][None]
        )

        # Episode bookkeeping (mirror of the single-env reset)
        self.step_counter[envs_idx] = 0
        self.termination[envs_idx] = False
        self.final_approach[envs_idx] = False
        self.station_c[envs_idx] = 0.0
        self.state_reward[envs_idx] = 0.0
        self.episodic_reward[envs_idx] = 0.0
        self.z_start[envs_idx] = 0.5
        self.x_start[envs_idx] = 0.0
        self.y_start[envs_idx] = 0.0
        self._last_pos_err[envs_idx] = 0.0
        self._last_yaw_err_deg[envs_idx] = 0.0
        return self.get_obs()

    # ----------------- Gripper trajectory -----------------

    def _cluster_indices(self):
        return self._ckpt["top_left"]["group"], self._ckpt["top_right"]["group"]

    def _set_cluster_pose(self, pos: torch.Tensor, quat: torch.Tensor):
        """Place every env's held cluster rigidly at (pos, quat); z clamped to the
        solver-feasible floor (see the single-env class for why)."""
        target = pos[:, None, :] + quat_apply(quat, self._cluster_local)
        target[:, :, 2].clamp_(min=self.solver_particle_radius + 1e-4)
        left_group, right_group = self._cluster_indices()
        left_envs = self._all_envs[self.active_left]
        right_envs = self._all_envs[~self.active_left]
        if left_envs.numel():
            self.sheet.set_particles_pos(target[left_envs], particles_idx_local=left_group, envs_idx=left_envs)
        if right_envs.numel():
            self.sheet.set_particles_pos(target[right_envs], particles_idx_local=right_group, envs_idx=right_envs)

    def _move_clusters_to(self, p_end: torch.Tensor, rpy_end: torch.Tensor):
        """0.75 s batched trajectory: smoothstep position + slerp orientation (linear in t),
        the same profile as the reference mocap / single-env class."""
        p_start = self._cmd_pos.clone()
        q_start = euler_xyz_to_quat(self._cmd_rpy)
        q_end = euler_xyz_to_quat(rpy_end)
        for k in range(1, self._traj_steps + 1):
            t = k / self._traj_steps
            s = 3 * t**2 - 2 * t**3
            self._set_cluster_pose((1 - s) * p_start + s * p_end, quat_slerp(q_start, q_end, t))
            self.scene.step()
        self._cmd_pos = p_end.clone()
        self._cmd_rpy = rpy_end.clone()

    # ----------------- Observation / reward (vectorized 1:1 port) -----------------

    def _corners(self):
        pos = self.sheet.get_particles_pos()  # (B, n_particles, 3)
        ar = pos[:, self.corner_idx["back_left"]]
        al = pos[:, self.corner_idx["front_left"]]
        bl = pos[:, self.corner_idx["front_right"]]
        br = pos[:, self.corner_idx["back_right"]]
        return ar, al, bl, br

    def get_obs(self) -> torch.Tensor:
        B = self.num_envs
        ar, al, bl, br = self._corners()
        scale = self.scale

        rotate_deg = torch.rad2deg(self._cmd_rpy[:, 2])
        yaw_goal_deg = float(np.rad2deg(self.yaw_rotation_goal))
        yaw_diff = torch.deg2rad(((rotate_deg - yaw_goal_deg + 180.0) % 360.0) - 180.0)

        obs = torch.empty(B, self.OBS_DIM, device=self.device)
        obs[:, 0:3] = self.position_goal
        obs[:, 3] = self.yaw_rotation_goal
        obs[:, 4] = yaw_diff
        obs[:, 5] = -1.0  # state0: always the reference's -1.0 fallback
        obs[:, 6:9] = self._cmd_pos / scale
        obs[:, 9] = float(self.desired_state)
        obs[:, 10:13] = (al - ar) / scale
        obs[:, 13:16] = (br - ar) / scale
        obs[:, 16:19] = (bl - ar) / scale
        return obs

    def _reward(self) -> torch.Tensor:
        """Vectorized verbatim port of the reference reward() (see single-env class)."""
        scale = self.scale
        dev = self.device
        B = self.num_envs

        # `self.action` is zeros in the reference (never assigned by its step/training
        # loop), so: the stationary gate's action[0] > -0.89 always passes, and
        # non_centric_traj is the constant -(150*0.2*(0-0.45)*30) = +405.
        move_mag = torch.sqrt(self.z_delta**2 + self.x_delta**2 + self.y_delta**2)
        stationary_mask = move_mag < 0.15
        self.station_c = torch.where(stationary_mask, self.station_c + 1.0, self.station_c)
        stationary_rew = torch.where(stationary_mask, -300.0 * self.station_c, torch.zeros(B, device=dev))

        non_centric_traj = torch.full((B,), -(150.0 * 0.2 * (0.0 - 0.45) * 30.0), device=dev)

        pos_err = self._cmd_pos - self.position_goal  # move - goal (world baseline is zeros)
        rotate_deg = torch.rad2deg(self._cmd_rpy[:, 2])
        # Reference quirk kept: degrees minus radians goal (exact for goal = 0)
        yaw_err = ((rotate_deg - self.yaw_rotation_goal + 180.0) % 360.0) - 180.0
        roll_cur, pitch_cur = self._cmd_rpy[:, 0], self._cmd_rpy[:, 1]

        reverse_mv = torch.where(self.z_delta > 0.0, -1000.0 * self.z_delta.abs(), torch.zeros(B, device=dev))

        pos_reward = -torch.sqrt((pos_err / scale).pow(2).sum(dim=1)) * 100.0
        near_ground = pos_err[:, 2].abs() < 0.08
        roll_pen = torch.where(near_ground, -((5.0 * roll_cur) ** 2), torch.zeros(B, device=dev))
        pitch_pen = torch.where(near_ground, -((5.0 * pitch_cur) ** 2), torch.zeros(B, device=dev))
        pos_reward = torch.where(near_ground, pos_reward, 5.0 * pos_reward)

        yaw_reward = -20.0 * yaw_err.abs()

        gate = (pos_err[:, 2].abs() / scale) < 0.015
        first = gate & (self.step_counter == 1)
        later = gate & (self.step_counter > 1)
        fa_prev = self.final_approach.clone()

        pos_reward = torch.where(later, pos_reward * 60.0, pos_reward)
        added = torch.where(later & fa_prev, torch.full((B,), 300.0, device=dev), torch.zeros(B, device=dev))
        self.termination = self.termination | first | (later & fa_prev)
        self.final_approach = self.final_approach | later

        # state_reward for the fixed desired_state, only where `later`; -500 where `first`
        ar, al, bl, br = self._corners()
        if self.desired_state == 0:
            d_diag = (al - br).norm(dim=1) / scale
            d_diag2 = (ar - bl).norm(dim=1) / scale
            d_top = (al - ar).norm(dim=1) / scale
            d_r = (ar - br).norm(dim=1) / scale
            x_a = (d_top * d_r).abs() / 0.0225
            x_b = ((d_diag * d_diag2) / 2.0).abs() / 0.0225
            shaped = 10.0 * torch.exp(-8.0 * (x_a - 1.0) ** 2) * 10.0 * torch.exp(-8.0 * (x_b - 1.0) ** 2) * 4.0
        elif self.desired_state == 1:
            d_fold = (ar - bl).norm(dim=1) / scale
            gauss = torch.exp(-0.5 * (d_fold / 0.05) ** 2)
            linear = (1.0 - d_fold / 0.20).clamp(0.0, 1.0)
            shaped = gauss * 350.0 + linear * 50.0
        else:  # desired_state == 2
            d_xy_a = (ar - al)[:, :2].norm(dim=1) / scale
            d_xy_b = (br - bl)[:, :2].norm(dim=1) / scale
            fold_gauss = torch.exp(-0.5 * (d_xy_a / 0.04) ** 2) * torch.exp(-0.5 * (d_xy_b / 0.04) ** 2)
            fold_linear = (1.0 - d_xy_a / 0.15).clamp(0.0, 1.0) * (1.0 - d_xy_b / 0.15).clamp(0.0, 1.0)
            shaped = fold_gauss * 350.0 + fold_linear * 50.0

        self.state_reward = torch.where(later, shaped, torch.zeros(B, device=dev))
        self.state_reward = torch.where(first, torch.full((B,), -500.0, device=dev), self.state_reward)

        step_rew = (self.step_counter.float() / self.max_steps) * -900.0

        self._last_pos_err = pos_err.clone()
        self._last_yaw_err_deg = yaw_err.clone()

        return (
            pos_reward * 0.5
            + yaw_reward * 0.25
            + 9.0 * self.state_reward
            + step_rew
            + roll_pen
            + pitch_pen
            + reverse_mv
            + added
            + non_centric_traj
            + stationary_rew
        )

    def _truncation(self) -> torch.Tensor:
        return (self.step_counter > self.max_steps) | ((self._cmd_pos[:, 2] / self.scale) < 0.02)

    def compute_success_score(self) -> torch.Tensor:
        """Vectorized verbatim port of the reference 5-component success score [0, 100]."""
        scale = self.scale
        state_score = (self.state_reward / 400.0).clamp(0.0, 1.0)
        pos_dist = torch.sqrt((self._last_pos_err / scale).pow(2).sum(dim=1))
        pos_score = torch.exp(-0.5 * (pos_dist / 0.085) ** 2)
        yaw_score = (1.0 - self._last_yaw_err_deg.abs() / 90.0).clamp(0.0, 1.0)
        eff_score = (1.0 - self.step_counter.float() / self.max_steps).clamp(0.0, 1.0)

        ar, al, bl, br = self._corners()
        corner_z = torch.stack([ar[:, 2], al[:, 2], bl[:, 2], br[:, 2]], dim=1)
        z_span = (corner_z.max(dim=1).values - corner_z.min(dim=1).values) / scale
        if self.desired_state == 0:
            geom_score = torch.exp(-0.5 * (z_span / 0.04) ** 2)
        elif self.desired_state == 1:
            d_fold = (ar - bl).norm(dim=1) / scale
            d_other = (al - br).norm(dim=1) / scale
            asymmetry = ((d_other / (d_fold + 1e-6) - 1.0) / 3.0).clamp(0.0, 1.0)
            proximity = torch.exp(-0.5 * (d_fold / 0.05) ** 2)
            geom_score = 0.5 * asymmetry + 0.5 * proximity
        else:
            d_xy_a = (ar - al)[:, :2].norm(dim=1) / scale
            d_xy_b = (br - bl)[:, :2].norm(dim=1) / scale
            fold_score = torch.exp(-0.5 * (d_xy_a / 0.04) ** 2) * torch.exp(-0.5 * (d_xy_b / 0.04) ** 2)
            layer_score = (z_span / 0.08).clamp(0.0, 1.0)
            geom_score = 0.6 * fold_score + 0.4 * layer_score

        score = (0.50 * state_score + 0.25 * pos_score + 0.15 * yaw_score + 0.05 * eff_score + 0.05 * geom_score) * 100.0
        return score.clamp(0.0, 100.0)

    # ----------------- Step -----------------

    def step(self, actions: torch.Tensor):
        """actions: (B, 6) in [-1, 1]. Returns (obs, reward, termination, truncated, infos);
        done envs are auto-reset and their obs slot holds the post-reset observation
        (safe: done masks the bootstrap term in the TD target)."""
        actions = torch.as_tensor(actions, dtype=torch.float32, device=self.device).clamp(-1.0, 1.0).clone()
        actions[:, 0] = actions[:, 0] * 0.959
        self.step_counter += 1

        dz = ((actions[:, 0] + 1.0) / 2.0) * self.scale
        dx = actions[:, 1] * 0.3 * self.scale
        dy = actions[:, 2] * 0.3 * self.scale
        rpy_end = torch.stack(
            [actions[:, 3] * np.pi / 4, actions[:, 4] * np.pi / 4, actions[:, 5] * np.pi], dim=1
        )

        self.z_delta = dz - self.z_start
        self.x_delta = dx - self.x_start
        self.y_delta = dy - self.y_start
        self.z_start, self.x_start, self.y_start = dz.clone(), dx.clone(), dy.clone()

        self._move_clusters_to(torch.stack([dx, dy, dz], dim=1), rpy_end)

        reward = self._reward()
        truncated = self._truncation()
        self.episodic_reward += reward

        # Snapshot flags BEFORE auto-reset (reset() clears the termination latch and the
        # per-episode bookkeeping for the envs it touches).
        termination_out = self.termination.clone()
        done = termination_out | truncated
        infos = {
            "episodic_reward": self.episodic_reward.clone(),
            "success_score": torch.zeros(self.num_envs, device=self.device),
            "active_left": self.active_left.clone(),
        }
        if done.any():
            infos["success_score"][done] = self.compute_success_score()[done]
            self.success_score = infos["success_score"]
            self.reset(self._all_envs[done])

        obs = self.get_obs()
        return obs, reward, termination_out, truncated, infos

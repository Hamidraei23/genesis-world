"""
GPU-parallel FrankaEnv for RL training with Genesis.

Key changes vs. env_franka.py (single-env):
  - scene.build(n_envs=num_envs)          → N envs simulated in parallel on GPU
  - all state: numpy (dim,) → torch (N, dim) on gs.device
  - step(actions) accepts (N, action_dim) tensors
  - reset(envs_idx) allows selective per-env reset
  - controller fully vectorised with torch.linalg.solve (batched Jacobian)
  - camera removed (not needed for training)
"""

import math
from pathlib import Path

import torch
import numpy as np
from tensordict import TensorDict

import genesis as gs

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Torch-native helpers (Genesis's quat_to_rotvec is numpy-only)
# ---------------------------------------------------------------------------

def _tc_quat_to_rotvec(quat: torch.Tensor) -> torch.Tensor:
    """Angle-axis (rotvec) from quaternion (w, x, y, z). Supports any batch shape."""
    q_w = quat[..., :1]           # (..., 1)
    q_vec = quat[..., 1:]         # (..., 3)
    s2 = q_vec.norm(dim=-1, keepdim=True)
    angle = 2.0 * torch.atan2(s2, q_w.abs())
    inv_sinc = angle / s2.clamp(min=1e-8)
    sign = torch.where(q_w < 0.0, torch.full_like(q_w, -1.0), torch.ones_like(q_w))
    return sign * inv_sinc * q_vec


def _tc_inv_quat(quat: torch.Tensor) -> torch.Tensor:
    """Conjugate (inverse) of a unit quaternion."""
    inv = quat.clone()
    inv[..., 1:] = -inv[..., 1:]
    return inv


def _tc_quat_mul(u: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """Hamilton product  u ⊗ v  for unit quaternions (w, x, y, z)."""
    uw, ux, uy, uz = u[..., 0], u[..., 1], u[..., 2], u[..., 3]
    vw, vx, vy, vz = v[..., 0], v[..., 1], v[..., 2], v[..., 3]
    quat = torch.stack([
        uw * vw - ux * vx - uy * vy - uz * vz,
        uw * vx + ux * vw + uy * vz - uz * vy,
        uw * vy - ux * vz + uy * vw + uz * vx,
        uw * vz + ux * vy - uy * vx + uz * vw,
    ], dim=-1)
    return quat / quat.norm(dim=-1, keepdim=True).clamp(min=1e-8)


# ---------------------------------------------------------------------------
# Parallel Franka environment
# ---------------------------------------------------------------------------

class FrankaEnvParallel:
    """
    Vectorised Franka environment.

    Observations: flat tensor (N, OBS_DIM=14)
        [0]    ee_pos_z
        [1]    ee_vel_z
        [2]    target_z_vel
        [3]    target_z_acc
        [4]    cuboid_rel_z
        [5]    cuboid_rel_x
        [6]    cuboid_rel_y
        [7]    desired_rel_z
        [8:10]   last 2 z_improvement values (oldest → newest)
        [10:12]  last 2 time-to-next(ee_vel_z < 0) after pulse-open, in ms (oldest → newest)
        [12:14]  last 2 time-to-last(ee_vel_z < 0) after pulse-open, in ms (oldest → newest)

    Actions (per env, shape (N, action_dim=3)):
        [0]   target_z_vel
        [1:3] gripper_pos (left, right finger)
    """

    # Observation layout constants
    OBS_DIM            = 14
    HISTORY_LEN        = 2
    OBS_EE_POS_Z       = 0
    OBS_EE_VEL_Z       = 1
    OBS_TARGET_Z_VEL   = 2
    OBS_TARGET_Z_ACC   = 3
    OBS_CUBOID_REL_Z   = 4
    OBS_CUBOID_REL_X   = 5
    OBS_CUBOID_REL_Y   = 6
    OBS_DESIRED_REL_Z  = 7
    OBS_LAST_Z_IMPROVE_START = 8
    OBS_NEXT_NEG_VEL_MS_START = 10
    OBS_LAST_NEG_VEL_MS_START = 12

    # Fixed observation normalization scales (divide raw obs by these)
    # Order: ee_pos_z, ee_vel_z, target_z_vel, target_z_acc,
    #        cuboid_rel_z, cuboid_rel_x, cuboid_rel_y, desired_rel_z,
    #        z_improvement history,
    #        time_to_next_neg_vel_ms history,
    #        time_to_last_neg_vel_ms history
    OBS_SCALE = (
        [1.0, 0.6, 0.6, 15.0, 0.05, 0.05, 0.05, 0.05]
        + [0.05] * HISTORY_LEN
        + [200.0] * HISTORY_LEN
        + [200.0] * HISTORY_LEN
    )

    # Action scaling constants
    Z_VEL_MAX       = 0.6
    Z_ACC_MAX       = 16.00   # m/s² — hard limit on target-velocity rate of change
    Z_ACC_PENALTY_THRESHOLD = 15.0
    Z_ACC_PENALTY_WEIGHT    = 0.0
    EE_Z_TARGET     = 0.8
    GRIPPER_CLOSED  = 0.000251
    GRIPPER_OPEN    = 0.0124
    # Release-to-regrasp detection
    FORCE_FREE_THRESHOLD = 0.15  # N: avg finger force below this -> fully released
    REGRASP_FORCE_THRESHOLD = 0.75  # N: avg finger force at/above this -> firm grasp
    AMBIGUOUS_FORCE_PENALTY = 10.0
    FREE_FORCE_REWARD = 1.0
    REGRASP_BONUS           = 200.0  # duration-weighted reward scale for successful regrasp events
    REGRASP_TERMINATION_COUNT = 7   # fail on the 7th regrasp if not successful by then
    REGRASP_BONUS_MAX_COUNT = 4     # regrasp bonus paid only for the first 4 regrasps
    FIRM_GRASP_SLIP_PENALTY_WEIGHT = 15.0  # harsh penalty per (m/step)² of Z-slip during firm grasp
    SUCCESS_EE_Z_MIN = 0.7
    SUCCESS_EE_Z_MAX = 0.86
    SUCCESS_REQUIRED_STEPS = 1
    EE_HOLD_Z_TARGET = 0.8
    EE_HOLD_Z_TOLERANCE = 0.025
    EE_HOLD_VEL_TOLERANCE = 0.04
    EE_HOLD_REQUIRED_STEPS = 1
    EE_HOLD_ACC_THRESHOLD = 2.0
    PULSE_DELAY_STEPS = 0   # target-period steps to wait before the open window begins
    PULSE_DELAY_RANDOM_MIN = 1
    PULSE_DELAY_RANDOM_MAX = 3
    PULSE_LENGTH = 4   # steps: 5 open, 1 close, then back to policy control
    PULSE_LENGTH_RANDOM_MIN = 3
    PULSE_LENGTH_RANDOM_MAX = 6
    ZERO_HOLD_DURATION_MIN = 0.1  # seconds
    ZERO_HOLD_DURATION_MAX = 0.3  # seconds; used when randomize=True
    # Post-pulse hold penalty: after pulse completes, wait 0.5s, then penalise instability for 0.5s
    POST_PULSE_DELAY_DURATION = 0.5  # wait before evaluation window
    POST_PULSE_HOLD_DURATION = 0.5   # seconds
    POST_PULSE_HOLD_PENALTY_Z = 40.0    # per-step penalty weight for distance from target
    POST_PULSE_HOLD_PENALTY_VEL = 40.0  # per-step penalty weight for velocity excess
    POST_PULSE_HOLD_Z_TARGET = 0.8   # desired ee_z during hold
    POST_PULSE_HOLD_VEL_MAX = 0.03   # deadzone for velocity penalty
    # Commanded-z-velocity penalty (gated by minimize_vel): free below the deadzone,
    # then grows exponentially from _MIN at the deadzone to _MAX at Z_VEL_MAX.
    VEL_CMD_PENALTY_DEADZONE = 0.3   # m/s: |commanded z-vel| at or below this costs nothing
    VEL_CMD_PENALTY_MIN = 10.0       # penalty magnitude just past the deadzone
    VEL_CMD_PENALTY_MAX = 60.0       # penalty magnitude at |commanded z-vel| = Z_VEL_MAX
    # Progressive shaping on the remaining cuboid-z error, potential-based.
    # Phi is PROGRESS_POTENTIAL_MAX at zero error and 0 at PROGRESS_POTENTIAL_RANGE,
    # exponential in between. Paid as gamma * Phi(s') - Phi(s), so the policy is
    # paid for error it removes, never for proximity it merely holds for a step.
    # The episode total telescopes to -Phi(s0), so this cannot be farmed.
    REWARD_GAMMA = 0.99               # must equal the PPO gamma; train script asserts it
    PROGRESS_POTENTIAL_MAX = 1000.0   # potential at zero error
    PROGRESS_POTENTIAL_RANGE = 0.04   # m: error at or beyond this has zero potential
    PROGRESS_POTENTIAL_DECAY = 0.01   # m: e-folding length of the exponential
    # Reward terms summed over each episode and reported in extras["episode"]
    EP_LOG_TERMS = ("total", "base", "regrasp", "progress", "jerk", "vel_cmd", "hold")
    FINGER_GAIN_RANDOM_MIN = 100.0
    FINGER_GAIN_RANDOM_MAX = 500.0
    # Friction domain randomisation: effective contact μ sampled each episode
    FRICTION_BASE = 0.75   # sliding friction in the MJCF files (dominant value)
    FRICTION_MIN  = 0.6   # minimum desired effective contact friction
    FRICTION_MAX  = 0.90   # maximum desired effective contact friction
    CONTROL_ERROR_ABS_MIN = 0.03
    CONTROL_ERROR_ABS_MAX = 0.06

    def __init__(
        self,
        num_envs: int = 1,
        *,
        vis: bool = False,
        record: bool = False,
        dt: float = 0.001,
        target_dt: float = 0.02,
        gripper_pos_min: float = 0.000251,
        gripper_pos_max: float = 0.0124,
        pos_gain: float = 8.0,
        rot_gain: float = 4.0,
        jacobian_damping: float = 1e-4,
        limit_regrasp: bool = False,
        solid_up: bool = False,
        mix: bool = False,
        randomize: bool = False,
        normalize: bool = False,
        zero: bool = False,
        control_error: bool = False,
        minimize_vel: bool = False,
    ):
        self.num_envs = num_envs
        self.device = gs.device
        self.dt = dt
        self.target_dt = target_dt
        self.target_update_every = max(1, int(round(target_dt / dt)))
        self.target_period = self.target_update_every * dt
        self.num_actions = 3
        self.max_episode_length = 450
        self.limit_regrasp = limit_regrasp
        self.solid_up = solid_up
        self.mix = mix
        self.randomize = randomize
        self.normalize = normalize
        self.zero = zero
        self.control_error = control_error
        self.minimize_vel = minimize_vel
        # Exponential rate that carries the velocity penalty from _MIN at the
        # deadzone to _MAX at Z_VEL_MAX: MIN * exp(rate * (Z_VEL_MAX - deadzone)) == MAX
        self._vel_cmd_penalty_rate = math.log(
            self.VEL_CMD_PENALTY_MAX / self.VEL_CMD_PENALTY_MIN
        ) / (self.Z_VEL_MAX - self.VEL_CMD_PENALTY_DEADZONE)
        self.extras: dict = {}
        self.cfg = {
            "num_envs": num_envs,
            "num_actions": 3,
            "obs_dim": self.OBS_DIM,
            "max_episode_length": self.max_episode_length,
            "dt": dt,
            "target_dt": target_dt,
            "z_vel_max": self.Z_VEL_MAX,
            "gripper_closed": self.GRIPPER_CLOSED,
            "gripper_open": self.GRIPPER_OPEN,
            "limit_regrasp": limit_regrasp,
            "solid_up": solid_up,
            "mix": mix,
            "randomize": randomize,
            "normalize": normalize,
            "zero": zero,
            "control_error": control_error,
            "minimize_vel": minimize_vel,
            "success_ee_z_min": self.SUCCESS_EE_Z_MIN,
            "success_ee_z_max": self.SUCCESS_EE_Z_MAX,
            "success_required_steps": self.SUCCESS_REQUIRED_STEPS,
            "ee_hold_z_target": self.EE_HOLD_Z_TARGET,
            "ee_hold_z_tolerance": self.EE_HOLD_Z_TOLERANCE,
            "ee_hold_vel_tolerance": self.EE_HOLD_VEL_TOLERANCE,
            "ee_hold_required_steps": self.EE_HOLD_REQUIRED_STEPS,
            "ee_hold_acc_threshold": self.EE_HOLD_ACC_THRESHOLD,
            "pulse_delay": self.PULSE_DELAY_STEPS,
            "pulse_delay_random_min": self.PULSE_DELAY_RANDOM_MIN,
            "pulse_delay_random_max": self.PULSE_DELAY_RANDOM_MAX,
            "pulse_length": self.PULSE_LENGTH,
            "pulse_length_random_min": self.PULSE_LENGTH_RANDOM_MIN,
            "pulse_length_random_max": self.PULSE_LENGTH_RANDOM_MAX,
            "finger_gain_random_min": self.FINGER_GAIN_RANDOM_MIN,
            "finger_gain_random_max": self.FINGER_GAIN_RANDOM_MAX,
        }

        self.gripper_pos_min = torch.tensor([gripper_pos_min, gripper_pos_min], device=self.device)
        self.gripper_pos_max = torch.tensor([gripper_pos_max, gripper_pos_max], device=self.device)

        # Controller gains (scalar – same for all envs)
        self.pos_gain = pos_gain
        self.rot_gain = rot_gain
        # Jacobian regulariser: (6, 6), broadcast over batch in _control_once
        reg = jacobian_damping * torch.eye(6, device=self.device)
        self.jacobian_regularizer = reg  # (6, 6)

        # ------------------------------------------------------------------ #
        # 2nd-order low-pass filter: H(s) = 9025 / (s² + 100.8 s + 9025)     #
        # Discretised via bilinear (Tustin) transform at the sim rate T=dt.   #
        # Runs at 1000 Hz inside _control_once().                              #
        # ------------------------------------------------------------------ #
        _T   = self.dt
        _k   = 2.0 / _T
        _wn2 = 160000.0
        _blin = 490.0
        _a0  = _k**2 + _blin * _k + _wn2
        self._filt_b0 =  _wn2 / _a0
        self._filt_b1 = (2.0 * _wn2) / _a0
        self._filt_b2 =  _wn2 / _a0
        self._filt_a1 = (-2.0 * _k**2 + 2.0 * _wn2) / _a0  # signed: subtracted below
        self._filt_a2 = (_k**2 - _blin * _k + _wn2) / _a0  # signed: subtracted below

        # ------------------------------------------------------------------ #
        # Build scene with N parallel envs                                   #
        # ------------------------------------------------------------------ #
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=dt, substeps=1),
            rigid_options=gs.options.RigidOptions(batch_dofs_info=True),
            viewer_options=gs.options.ViewerOptions(
                camera_pos=(3.5, 0.0, 2.5),
                camera_lookat=(0.0, 0.0, 0.5),
                camera_fov=40,
            ),
            show_viewer=vis,
        )

        self.plane = self.scene.add_entity(gs.morphs.Plane())

        self.franka = self.scene.add_entity(
            gs.morphs.MJCF(file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/panda.xml")),
        )
        self.cuboid = self.scene.add_entity(
            gs.morphs.MJCF(file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/box.xml")),
            surface=gs.surfaces.Plastic(color=(0.18, 0.42, 0.82)),
        )

        # Optional offscreen recording camera — must be added BEFORE scene.build()
        self.cam = None
        if record:
            self.cam = self.scene.add_camera(
                res=(1280, 960),
                pos=(3.5, 0.0, 2.5),
                lookat=(0.0, 0.0, 0.5),
                fov=30,
                GUI=False,
            )

        # One shared spacing so envs do not overlap visually
        self.scene.build(n_envs=num_envs, env_spacing=(1.5, 1.5))

        # Global geom index range for friction randomisation (franka + cuboid)
        # Both are built; their geom_start / n_geoms are valid after scene.build().
        _franka_geom_idx = torch.arange(
            self.franka.geom_start, self.franka.geom_start + self.franka.n_geoms,
            device=self.device,
        )
        _cuboid_geom_idx = torch.arange(
            self.cuboid.geom_start, self.cuboid.geom_start + self.cuboid.n_geoms,
            device=self.device,
        )
        self._contact_geoms_idx = torch.cat([_franka_geom_idx, _cuboid_geom_idx])  # (n_contact_geoms,)

        # ------------------------------------------------------------------ #
        # DOF / link indices                                                  #
        # ------------------------------------------------------------------ #
        self.motors_dof = torch.arange(7, device=self.device)
        self.fingers_dof = torch.arange(7, 9, device=self.device)
        self.q_home = torch.tensor(
            [0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78, 0.008090, 0.008090],
            device=self.device,
        )  # (9,)

        self.left_finger = self.franka.get_link("left_finger")
        self.right_finger = self.franka.get_link("right_finger")
        self.ee_link = self.franka.get_link("hand")

        # Fingertip offsets in local finger frame
        self.fingertip_local = torch.tensor([0.0, 0.0055, 0.0445], device=self.device)

        self._set_franka_gains()
        self.reset()

    # ------------------------------------------------------------------ #
    # Reset                                                               #
    # ------------------------------------------------------------------ #

    def reset(self, envs_idx: torch.Tensor | None = None, warmup_steps: int = 100):
        """
        Reset selected environments (all if envs_idx is None).

        Returns obs tensor (num_envs, obs_dim) or (len(envs_idx), obs_dim).
        """
        N = self.num_envs
        if envs_idx is None:
            envs_idx = torch.arange(N, device=self.device)

        # ---- robot pose ----
        q_home_batch = self.q_home.unsqueeze(0).expand(len(envs_idx), -1)  # (|idx|, 9)
        self.franka.set_qpos(q_home_batch, envs_idx=envs_idx)
        self.franka.control_dofs_position(q_home_batch, envs_idx=envs_idx)

        # ---- cuboid ----
        self._reset_cuboid_home_pose(envs_idx)

        # ---- controller state (only for selected envs) ----
        if not hasattr(self, "target_center"):
            # First call: allocate full buffers
            self.target_center = torch.zeros(N, 3, device=self.device)
            self.target_quat = torch.zeros(N, 4, device=self.device)
            self.target_z = torch.zeros(N, device=self.device)
            self.target_z_vel = torch.zeros(N, device=self.device)
            self.target_z_acc = torch.zeros(N, device=self.device)
            self.prev_target_z_vel = torch.zeros(N, device=self.device)
            self.desired_rel_z = torch.zeros(N, device=self.device)
            self.episode_length_buf = torch.zeros(N, dtype=torch.long, device=self.device)
            self.obs_buf = torch.zeros(N, self.OBS_DIM, device=self.device)
            self.rew_buf = torch.zeros(N, device=self.device)
            self.reset_buf = torch.zeros(N, dtype=torch.bool, device=self.device)
            self.direction_change_count = torch.zeros(N, dtype=torch.long, device=self.device)
            self.last_reward_terms = {}
            # Per-episode reward-term accumulators, published via extras["episode"]
            self._ep_term_sums = {
                k: torch.zeros(N, device=self.device) for k in self.EP_LOG_TERMS
            }
            # Release-to-regrasp tracking
            self._in_release = torch.zeros(N, dtype=torch.bool, device=self.device)
            self._regrasp_count = torch.zeros(N, dtype=torch.long, device=self.device)
            self._release_start_step = torch.full((N,), -1, dtype=torch.long, device=self.device)
            self._last_regrasp_duration_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._regrasp_duration_sum_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._regrasp_duration_max_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._regrasp_duration_steps = torch.zeros(N, 3, dtype=torch.long, device=self.device)
            # Consecutive firm-grasp step counter (used in success condition)
            self._firm_grasp_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._success_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._pre_success = torch.zeros(N, dtype=torch.bool, device=self.device)
            self._ee_hold_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._ee_hold_complete = torch.zeros(N, dtype=torch.bool, device=self.device)
            # Potential-based progress shaping: Phi of the previous step
            self._prev_potential = torch.zeros(N, device=self.device)
            # Firm-grasp Z-slip tracking
            self._prev_cuboid_rel_z = torch.zeros(N, device=self.device)
            # Regrasp improvement tracking: cuboid_rel_z at the moment of release
            self._release_cuboid_rel_z = torch.zeros(N, device=self.device)
            # 2nd-order low-pass filter state (z-vel command path)
            self._filt_u1 = torch.zeros(N, device=self.device)  # u[n-1]
            self._filt_u2 = torch.zeros(N, device=self.device)  # u[n-2]
            self._filt_y1 = torch.zeros(N, device=self.device)  # y[n-1]
            self._filt_y2 = torch.zeros(N, device=self.device)  # y[n-2]
            # Gripper pulse state: counter length..2 → force max, 1 → force min, 0 → policy
            self._gripper_pulse_steps = torch.zeros(N, dtype=torch.long, device=self.device)
            self._gripper_pulse_delays = torch.full((N,), self.PULSE_DELAY_STEPS, dtype=torch.long, device=self.device)
            self._gripper_pulse_lengths = torch.full((N,), self.PULSE_LENGTH, dtype=torch.long, device=self.device)
            self._prev_gripper_avg = torch.full((N,), self.gripper_pos_min.mean().item(), device=self.device)
            # Optional post-pulse zero-action hold state
            self._zero_hold_steps = torch.full(
                (N,),
                max(1, int(round(self.ZERO_HOLD_DURATION_MIN / self.target_period))),
                dtype=torch.long,
                device=self.device,
            )
            self._zero_hold_countdown = torch.zeros(N, dtype=torch.long, device=self.device)
            self._zero_wait_for_direction_change = torch.zeros(N, dtype=torch.bool, device=self.device)
            self._prev_policy_z_vel_sign = torch.zeros(N, dtype=torch.long, device=self.device)
            # Post-pulse hold countdown: high-level steps remaining in the hold window
            _delay_hl_steps = max(1, int(round(self.POST_PULSE_DELAY_DURATION / self.target_period)))
            _hold_hl_steps = max(1, int(round(self.POST_PULSE_HOLD_DURATION / self.target_period)))
            self._post_pulse_delay_total = _delay_hl_steps
            self._post_pulse_hold_total = _hold_hl_steps
            self._post_pulse_delay_countdown = torch.zeros(N, dtype=torch.long, device=self.device)
            self._post_pulse_hold_countdown = torch.zeros(N, dtype=torch.long, device=self.device)
            # Observation histories (oldest -> newest)
            self._z_improve_hist = torch.zeros(N, self.HISTORY_LEN, device=self.device)
            # Per-episode sum of z_improvement over regrasp events (for logging)
            self._z_improve_sum = torch.zeros(N, device=self.device)
            self._pulse_to_next_neg_vel_ms_hist = torch.zeros(N, self.HISTORY_LEN, device=self.device)
            self._pulse_to_last_neg_vel_ms_hist = torch.zeros(N, self.HISTORY_LEN, device=self.device)
            # Per-pulse transient state for timing extraction
            self._pulse_open_start_ms = torch.zeros(N, device=self.device)
            self._pulse_open_active = torch.zeros(N, dtype=torch.bool, device=self.device)
            self._current_pulse_next_neg_ms = torch.full((N,), -1.0, device=self.device)
            self._current_pulse_last_neg_ms = torch.full((N,), -1.0, device=self.device)
            # Cubic-hermite segment state per env
            self._seg_start = None   # (N, 3): (z, z_vel, z_acc)
            self._seg_end = None     # (N, 3)
            self._seg_t0 = torch.zeros(N, device=self.device)  # wall-time at segment start
            # Per-episode control-error bias added to policy z-velocity command
            self._episode_control_error = torch.zeros(N, device=self.device)

        # Warmup first so ee_link.get_pos() is valid
        for _ in range(warmup_steps):
            q_home_batch_all = self.q_home.unsqueeze(0).expand(N, -1)
            self.franka.control_dofs_position(q_home_batch_all)
            self.scene.step()

        # Read ee state after warmup
        ee_pos = self.ee_link.get_pos()    # (N, 3)
        ee_quat = self.ee_link.get_quat()  # (N, 4)

        self.target_center[envs_idx] = ee_pos[envs_idx].clone()
        self.target_quat[envs_idx] = ee_quat[envs_idx].clone()
        self.target_z[envs_idx] = ee_pos[envs_idx, 2].clone()
        self.target_z_vel[envs_idx] = 0.0
        self.target_z_acc[envs_idx] = 0.0
        self.prev_target_z_vel[envs_idx] = 0.0
        self.direction_change_count[envs_idx] = 0
        _n = len(envs_idx)
        _mag = 0.02 + torch.rand(_n, device=self.device) * 0.02   # uniform in [0.01, 0.04]
        # _mag = 0.0375
        if self.mix:
            _sign = torch.where(torch.rand(_n, device=self.device) < 0.5,
                                torch.ones(_n, device=self.device),
                                torch.full((_n,), -1.0, device=self.device))
            _mag = _sign * _mag   # randomly flip sign per env
        elif self.solid_up:
            _mag = -_mag   # negative desired_rel_z → solid-up training regime
        self.desired_rel_z[envs_idx] = _mag
        self.episode_length_buf[envs_idx] = 0
        for _term_sum in self._ep_term_sums.values():
            _term_sum[envs_idx] = 0.0
        self._in_release[envs_idx] = False
        self._regrasp_count[envs_idx] = 0
        self._release_start_step[envs_idx] = -1
        self._last_regrasp_duration_steps[envs_idx] = 0
        self._regrasp_duration_sum_steps[envs_idx] = 0
        self._regrasp_duration_max_steps[envs_idx] = 0
        self._regrasp_duration_steps[envs_idx] = 0
        self._prev_cuboid_rel_z[envs_idx] = 0.0
        self._release_cuboid_rel_z[envs_idx] = 0.0
        self._firm_grasp_steps[envs_idx] = 0
        self._success_steps[envs_idx] = 0
        self._pre_success[envs_idx] = False
        self._ee_hold_steps[envs_idx] = 0
        self._ee_hold_complete[envs_idx] = False
        self._filt_u1[envs_idx] = 0.0
        self._filt_u2[envs_idx] = 0.0
        self._filt_y1[envs_idx] = 0.0
        self._filt_y2[envs_idx] = 0.0
        self._gripper_pulse_steps[envs_idx] = 0
        self._sample_gripper_pulse_delays(envs_idx)
        self._sample_gripper_pulse_lengths(envs_idx)
        self._sample_zero_hold_steps(envs_idx)
        self._randomize_finger_gains(envs_idx)
        self._prev_gripper_avg[envs_idx] = self.gripper_pos_min.mean()
        self._zero_hold_countdown[envs_idx] = 0
        self._zero_wait_for_direction_change[envs_idx] = False
        self._prev_policy_z_vel_sign[envs_idx] = 0
        self._post_pulse_delay_countdown[envs_idx] = 0
        self._post_pulse_hold_countdown[envs_idx] = 0
        self._z_improve_hist[envs_idx] = 0.0
        self._z_improve_sum[envs_idx] = 0.0
        self._pulse_to_next_neg_vel_ms_hist[envs_idx] = 0.0
        self._pulse_to_last_neg_vel_ms_hist[envs_idx] = 0.0
        self._pulse_open_start_ms[envs_idx] = 0.0
        self._pulse_open_active[envs_idx] = False
        self._current_pulse_next_neg_ms[envs_idx] = -1.0
        self._current_pulse_last_neg_ms[envs_idx] = -1.0

        self._seg_start = None
        self._seg_end = None
        self._seg_t0 = torch.zeros(N, device=self.device)

        self._randomize_friction(envs_idx)
        self._sample_control_error(envs_idx)

        # ---- initial velocity randomization (domain randomization) ----
        if self.randomize:
            _n = len(envs_idx)
            # Add small random joint velocities to arm DOFs (±0.01 rad/s)
            qvel_noise = (torch.rand(_n, 7, device=self.device) * 2.0 - 1.0) * 0.01
            qvel = torch.zeros(_n, 9, device=self.device)
            qvel[:, :7] = qvel_noise
            self.franka.set_dofs_velocity(qvel, envs_idx=envs_idx)

        self._seed_progress_potential(envs_idx)

        self.sim_step = 0

        self._update_obs_buf()
        return self.get_observations()

    # ------------------------------------------------------------------ #
    # Step                                                                #
    # ------------------------------------------------------------------ #

    def step(
        self,
        actions: torch.Tensor,
        *,
        update_visualizer: bool = True,
        refresh_visualizer: bool = True,
    ):
        """
        actions: (num_envs, 3)  —  [target_z_vel, finger_l, finger_r]

        Runs target_update_every sim steps and returns obs.
        """
        if actions.shape != (self.num_envs, self.num_actions):
            raise ValueError(f"actions must be ({self.num_envs}, {self.num_actions}), got {actions.shape}")

        if self.zero:
            policy_z_vel = actions[:, 0].clamp(-1.0, 1.0) * self.Z_VEL_MAX
            policy_z_vel_sign = torch.where(
                policy_z_vel > 1e-4,
                torch.ones_like(self._prev_policy_z_vel_sign),
                torch.where(
                    policy_z_vel < -1e-4,
                    -torch.ones_like(self._prev_policy_z_vel_sign),
                    torch.zeros_like(self._prev_policy_z_vel_sign),
                ),
            )

            start_zero_hold = (
                self._zero_wait_for_direction_change
                & (self._zero_hold_countdown == 0)
                & (self._prev_policy_z_vel_sign != 0)
                & (policy_z_vel_sign != 0)
                & (policy_z_vel_sign != self._prev_policy_z_vel_sign)
            )
            self._zero_hold_countdown = torch.where(
                start_zero_hold,
                self._zero_hold_steps,
                self._zero_hold_countdown,
            )
            self._zero_wait_for_direction_change = torch.where(
                start_zero_hold,
                torch.zeros_like(self._zero_wait_for_direction_change),
                self._zero_wait_for_direction_change,
            )

            nonzero_sign = policy_z_vel_sign != 0
            self._prev_policy_z_vel_sign = torch.where(
                nonzero_sign,
                policy_z_vel_sign,
                self._prev_policy_z_vel_sign,
            )

            in_zero_hold = self._zero_hold_countdown > 0
            if torch.any(in_zero_hold):
                actions = actions.clone()
                actions[in_zero_hold, 0] = 0.0
                actions[in_zero_hold, 1:] = -1.0
                self._zero_hold_countdown = torch.where(
                    in_zero_hold,
                    self._zero_hold_countdown - 1,
                    self._zero_hold_countdown,
                )

        new_z_vel = actions[:, 0].clamp(-1.0, 1.0) * self.Z_VEL_MAX  # (N,)
        if self.control_error:
            new_z_vel = new_z_vel + self._episode_control_error
        new_z_vel = new_z_vel.clamp(-self.Z_VEL_MAX, self.Z_VEL_MAX)
        # Limit acceleration: |Δv| ≤ Z_ACC_MAX * target_period
        max_dv = self.Z_ACC_MAX * self.target_period
        new_z_vel = new_z_vel.clamp(self.target_z_vel - max_dv, self.target_z_vel + max_dv)
        
        gripper_raw = actions[:, 1:].clamp(-1.0, 1.0)                 # (N, 2)
        gripper_pos = self.gripper_pos_min + (gripper_raw + 1.0) * 0.5 * (self.gripper_pos_max - self.gripper_pos_min)  # (N, 2)

        # Gripper pulse: rising edge through midpoint →
        #   per-env pulse_delay steps of policy (delay),
        #   then per-env pulse_length - 1 steps forced open,
        #   then 1 step forced close.
        # Delay region:  steps > pulse_length       → policy (no override)
        # Open region:   2 <= steps <= pulse_length → gripper_pos_max
        # Close step:    steps == 1                 → gripper_pos_min
        # Done:          steps == 0                 → policy
        _pulse_start = self._gripper_pulse_lengths + self._gripper_pulse_delays
        _gp_mid = (self.gripper_pos_min + self.gripper_pos_max).mean() * 0.5  # scalar midpoint
        _gp_avg = gripper_pos.mean(dim=-1)                                     # (N,)
        _rising = (
            (self._prev_gripper_avg < _gp_mid)
            & (_gp_avg >= _gp_mid)
            & (self._gripper_pulse_steps == 0)
        )  # (N,) rising-edge crossing with no active pulse
        self._gripper_pulse_steps = torch.where(
            _rising, _pulse_start, self._gripper_pulse_steps
        )
        _gp_max_b = self.gripper_pos_max.unsqueeze(0).expand(self.num_envs, -1)  # (N, 2)
        _gp_min_b = self.gripper_pos_min.unsqueeze(0).expand(self.num_envs, -1)  # (N, 2)
        _in_open = (self._gripper_pulse_steps >= 2) & (
            self._gripper_pulse_steps <= self._gripper_pulse_lengths
        )
        _in_close = self._gripper_pulse_steps == 1
        _step_start_ms = self.sim_step * self.dt * 1000.0
        _open_start = _in_open & (~self._pulse_open_active)
        self._pulse_open_start_ms = torch.where(
            _open_start,
            torch.full_like(self._pulse_open_start_ms, _step_start_ms),
            self._pulse_open_start_ms,
        )
        self._current_pulse_next_neg_ms = torch.where(
            _open_start,
            torch.full_like(self._current_pulse_next_neg_ms, -1.0),
            self._current_pulse_next_neg_ms,
        )
        self._current_pulse_last_neg_ms = torch.where(
            _open_start,
            torch.full_like(self._current_pulse_last_neg_ms, -1.0),
            self._current_pulse_last_neg_ms,
        )
        self._pulse_open_active = _in_open
        gripper_pos = torch.where(
            _in_open.unsqueeze(-1), _gp_max_b,
            torch.where(_in_close.unsqueeze(-1), _gp_min_b, gripper_pos),
        )
        self._prev_gripper_avg = _gp_avg
        # Detect pulse completion (close step, about to go 1 → 0) → start hold window
        _pulse_just_completed = _in_close  # (N,) True on the final close step
        self._post_pulse_delay_countdown = torch.where(
            _pulse_just_completed,
            torch.full_like(self._post_pulse_delay_countdown, self._post_pulse_delay_total),
            self._post_pulse_delay_countdown,
        )
        self._post_pulse_hold_countdown = torch.where(
            _pulse_just_completed,
            torch.full_like(self._post_pulse_hold_countdown, self._post_pulse_hold_total),
            self._post_pulse_hold_countdown,
        )
        if self.zero:
            self._zero_wait_for_direction_change = torch.where(
                _pulse_just_completed,
                torch.ones_like(self._zero_wait_for_direction_change),
                self._zero_wait_for_direction_change,
            )
        self._gripper_pulse_steps = (self._gripper_pulse_steps - 1).clamp(min=0)

        # Trapezoid integration for z
        new_z_acc = (new_z_vel - self.target_z_vel) / self.target_period
        new_z = self.target_z + 0.5 * (self.target_z_vel + new_z_vel) * self.target_period

        # Update segment for cubic-hermite interpolation
        old_sample = torch.stack([self.target_z, self.target_z_vel, self.target_z_acc], dim=-1)  # (N,3)
        new_sample = torch.stack([new_z, new_z_vel, new_z_acc], dim=-1)
        self._seg_start = old_sample
        self._seg_end = new_sample
        self._seg_t0 = torch.full((self.num_envs,), self.sim_step * self.dt, device=self.device)

        self.prev_target_z_vel = self.target_z_vel.clone()
        self.target_z = new_z
        self.target_z_vel = new_z_vel
        self.target_z_acc = new_z_acc

        # Control gripper (same pos for all substeps within this target period)
        self.franka.control_dofs_position(gripper_pos, dofs_idx_local=self.fingers_dof)

        for local_step in range(self.target_update_every):
            t = (self.sim_step + local_step) * self.dt
            tz, tz_vel = self._sample_target_z(t)   # (N,), (N,)
            self._control_once(tz, tz_vel)
            self.scene.step(
                update_visualizer=update_visualizer,
                refresh_visualizer=refresh_visualizer,
            )

        ee_vel_z_step = self.ee_link.get_vel()[:, 2]
        _step_end_ms = (self.sim_step + self.target_update_every) * self.dt * 1000.0
        _elapsed_ms = (_step_end_ms - self._pulse_open_start_ms).clamp(min=0.0)
        _neg_vel_now = ee_vel_z_step < 0.0
        _next_neg_mask = _in_open & _neg_vel_now & (self._current_pulse_next_neg_ms < 0.0)
        self._current_pulse_next_neg_ms = torch.where(
            _next_neg_mask,
            _elapsed_ms,
            self._current_pulse_next_neg_ms,
        )
        _last_neg_mask = _in_open & _neg_vel_now
        self._current_pulse_last_neg_ms = torch.where(
            _last_neg_mask,
            _elapsed_ms,
            self._current_pulse_last_neg_ms,
        )

        if torch.any(_pulse_just_completed):
            _next_ms = torch.where(
                self._current_pulse_next_neg_ms >= 0.0,
                self._current_pulse_next_neg_ms,
                torch.zeros_like(self._current_pulse_next_neg_ms),
            )
            _last_ms = torch.where(
                self._current_pulse_last_neg_ms >= 0.0,
                self._current_pulse_last_neg_ms,
                torch.zeros_like(self._current_pulse_last_neg_ms),
            )
            self._push_history(self._pulse_to_next_neg_vel_ms_hist, _next_ms, _pulse_just_completed)
            self._push_history(self._pulse_to_last_neg_vel_ms_hist, _last_ms, _pulse_just_completed)
            # print(f"Pulse completed: next_neg_ms={_next_ms[_pulse_just_completed]}, last_neg_ms={_last_ms[_pulse_just_completed]}")
            self._pulse_open_start_ms = torch.where(
                _pulse_just_completed,
                torch.zeros_like(self._pulse_open_start_ms),
                self._pulse_open_start_ms,
            )
            self._pulse_open_active = torch.where(
                _pulse_just_completed,
                torch.zeros_like(self._pulse_open_active),
                self._pulse_open_active,
            )
            self._current_pulse_next_neg_ms = torch.where(
                _pulse_just_completed,
                torch.full_like(self._current_pulse_next_neg_ms, -1.0),
                self._current_pulse_next_neg_ms,
            )
            self._current_pulse_last_neg_ms = torch.where(
                _pulse_just_completed,
                torch.full_like(self._current_pulse_last_neg_ms, -1.0),
                self._current_pulse_last_neg_ms,
            )

        self.sim_step += self.target_update_every
        self.episode_length_buf += 1
        done, reward, timeout = self._compute_done_and_reward()
        done_idx = done.nonzero(as_tuple=False).squeeze(-1)
        self._update_episode_extras(done_idx)
        if done_idx.numel() > 0:
            self._reset_idx(done_idx)
        self.rew_buf = reward
        self.reset_buf = done
        self.extras["time_outs"] = timeout.float()
        self._update_obs_buf()
        return self.get_observations(), self.rew_buf, self.reset_buf, self.extras

    def _update_episode_extras(self, done_idx: torch.Tensor):
        """
        Publish mean per-episode reward terms and outcome rates for the envs that
        just finished, under extras["episode"] (the key rsl_rl aggregates and logs).

        Must be called before _reset_idx() clears the accumulators.
        """
        if done_idx.numel() == 0:
            # extras persists across steps; drop stale data so it is not re-counted
            self.extras.pop("episode", None)
            return

        t = self.last_reward_terms
        # success excludes fail by construction; timeout may coincide with fail
        succ = t["success"][done_idx]
        fail = t["fail"][done_idx] * (1.0 - succ)
        tout = t["timeout"][done_idx] * (1.0 - succ) * (1.0 - fail)

        episode = {f"rew_{k}": v[done_idx].mean() for k, v in self._ep_term_sums.items()}
        episode["outcome_success"] = succ.mean()
        episode["outcome_fail"] = fail.mean()
        episode["outcome_timeout"] = tout.mean()
        episode["regrasp_count"] = self._regrasp_count[done_idx].float().mean()
        # Mean z_improvement per regrasp event, in mm, over episodes that regrasped
        _n_regrasp = self._regrasp_count[done_idx].float()
        _has_regrasp = _n_regrasp > 0
        if _has_regrasp.any():
            _per_event = (
                self._z_improve_sum[done_idx][_has_regrasp] / _n_regrasp[_has_regrasp]
            )
            episode["z_improve_mm"] = _per_event.mean() * 1000.0

        episode["ep_len"] = self.episode_length_buf[done_idx].float().mean()
        # How many episodes these means cover, so the training table can weight
        # each step's contribution instead of averaging means of unequal samples.
        episode["n_episodes"] = torch.tensor(float(done_idx.numel()), device=self.device)
        self.extras["episode"] = episode

    # ------------------------------------------------------------------ #
    # Observations                                                        #
    # ------------------------------------------------------------------ #

    def _update_obs_buf(self):
        """Compute observations and write into self.obs_buf."""
        ee_pos = self.ee_link.get_pos()     # (N, 3)
        ee_vel = self.ee_link.get_vel()     # (N, 3)
        cuboid_pos = self.cuboid.get_pos()  # (N, 3)
        left_ft = self._fingertip_pos(self.left_finger)    # (N, 3)
        right_ft = self._fingertip_pos(self.right_finger)  # (N, 3)

        finger_mid = (left_ft + right_ft) / 2.0  # (N, 3)
        _noise_range = 0.003 if self.randomize else 0.0
        _N = self.num_envs

        def _unoise(shape):
            return (torch.rand(shape, device=self.device) * 2.0 - 1.0) * _noise_range

        self.obs_buf = torch.cat([
            ee_pos[:, 2:3]  + _unoise((_N, 1)),                                     # [0]    ee_pos_z
            ee_vel[:, 2:3]  + _unoise((_N, 1)),                                     # [1]    ee_vel_z
            self.target_z_vel.unsqueeze(-1),                                         # [2]    target_z_vel
            self.target_z_acc.unsqueeze(-1),                                         # [3]    target_z_acc
            (cuboid_pos[:, 2] - finger_mid[:, 2]).unsqueeze(-1) + _unoise((_N, 1)), # [4]    cuboid_rel_z
            (cuboid_pos[:, 0] - finger_mid[:, 0]).unsqueeze(-1) + _unoise((_N, 1)), # [5]    cuboid_rel_x
            (cuboid_pos[:, 1] - finger_mid[:, 1]).unsqueeze(-1) + _unoise((_N, 1)), # [6]    cuboid_rel_y
            self.desired_rel_z.unsqueeze(-1),                                        # [7]    desired_rel_z
            self._z_improve_hist,                                                    # [8:10]
            self._pulse_to_next_neg_vel_ms_hist,                                    # [10:12]
            self._pulse_to_last_neg_vel_ms_hist,                                    # [12:14]
        ], dim=-1)  # (N, 14)

        if self.normalize:
            if not hasattr(self, '_obs_scale_t'):
                self._obs_scale_t = torch.tensor(self.OBS_SCALE, device=self.device)
            self.obs_buf = self.obs_buf / self._obs_scale_t

    def get_observation(self) -> torch.Tensor:
        """Returns flat obs tensor (num_envs, OBS_DIM). Convenience for non-rsl_rl usage."""
        self._update_obs_buf()
        return self.obs_buf

    def get_observations(self) -> TensorDict:
        """Returns TensorDict({"policy": obs_buf}) for rsl_rl compatibility."""
        return TensorDict({"policy": self.obs_buf}, batch_size=[self.num_envs])

    # ------------------------------------------------------------------ #
    # Done detection and partial reset                                    #
    # ------------------------------------------------------------------ #

    def _compute_done_and_reward(self) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Simple 3-term reward:
          1. Terminal:       success +1000 (minus small time penalty), fail/timeout -250
          2. Regrasp bonus:  up to +250 per regrasp, ONLY if z_error improved; zero otherwise
          3. Jerk penalty:   -0.5 * (Δv / Z_VEL_MAX)^2 per step
          4. (opt-in, --minimze_vel) Velocity penalty: 0 for |v| <= 0.3 m/s, then
             exponential from -10 to -60 at Z_VEL_MAX, per step

        Opening the gripper alone gives no reward — only completing release→regrasp
        that moves the cuboid closer to desired_rel_z is rewarded.
        Recommended gamma: 0.99 (terminal signal meaningful up to ~200 steps out).
        """
        cuboid_pos = self.cuboid.get_pos()                          # (N, 3)
        ee_pos = self.ee_link.get_pos()                             # (N, 3)
        ee_vel = self.ee_link.get_vel()                             # (N, 3)
        left_ft = self._fingertip_pos(self.left_finger)             # (N, 3)
        right_ft = self._fingertip_pos(self.right_finger)           # (N, 3)
        finger_mid = (left_ft + right_ft) / 2.0                    # (N, 3)
        fingertip_dist = (left_ft - right_ft).norm(dim=-1)          # (N,)

        link_forces     = self.franka.get_links_net_contact_force()     # (N, n_links, 3)
        left_force_mag  = link_forces[:, self.left_finger.idx_local,  :].norm(dim=-1)  # (N,)
        right_force_mag = link_forces[:, self.right_finger.idx_local, :].norm(dim=-1)  # (N,)
        avg_force       = (left_force_mag + right_force_mag) * 0.5    # (N,)

        cuboid_rel_z = cuboid_pos[:, 2] - finger_mid[:, 2]            # (N,)
        cuboid_rel_x = cuboid_pos[:, 0] - finger_mid[:, 0]            # (N,)
        cuboid_rel_y = cuboid_pos[:, 1] - finger_mid[:, 1]            # (N,)
        ee_z     = ee_pos[:, 2]                                        # (N,)
        ee_vel_z = ee_vel[:, 2]                                        # (N,)

        # ---- done conditions ----
        timeout = self.episode_length_buf >= self.max_episode_length   # (N,)

        # Track consecutive steps in firm grasp (reset when grasp is lost)
        firm_grasp_now = avg_force >= self.REGRASP_FORCE_THRESHOLD
        self._firm_grasp_steps = torch.where(
            firm_grasp_now,
            self._firm_grasp_steps + 1,
            torch.zeros_like(self._firm_grasp_steps),
        )

        success_candidate = (
            (~timeout)
            & ((cuboid_rel_z - self.desired_rel_z).abs() <= 0.005)
            & (ee_vel_z.abs() < 0.02)
            & (self._firm_grasp_steps >= 3)
            & (ee_z >= self.SUCCESS_EE_Z_MIN)
            & (ee_z <= self.SUCCESS_EE_Z_MAX)
        )
        fail = (
            (cuboid_rel_x.abs() > 0.04)
            | (cuboid_rel_y.abs() > 0.04)
            | (fingertip_dist < 0.01)
            | (cuboid_rel_z.abs() > 0.15)
            | (ee_z < 0.6)
            | (ee_z > 0.95)
        )

        # ---- regrasp event tracking ----
        firm_grasp     = firm_grasp_now                                # (N,)
        fully_released = avg_force < self.FORCE_FREE_THRESHOLD         # (N,)

        # Snapshot cuboid_rel_z at the start of each release window
        release_start = fully_released & (~self._in_release)
        self._release_start_step = torch.where(
            release_start, self.episode_length_buf, self._release_start_step
        )
        self._release_cuboid_rel_z = torch.where(
            release_start, cuboid_rel_z, self._release_cuboid_rel_z
        )

        # A regrasp event = was in release → now firm grasp
        regrasp_event = self._in_release & firm_grasp                  # (N,)
        self._in_release = (self._in_release | fully_released) & (~regrasp_event)

        # Optional: terminate episode after too many regrasps
        # Always terminate as failure on the REGRASP_TERMINATION_COUNT-th regrasp
        limit_fail = regrasp_event & (self._regrasp_count + 1 >= self.REGRASP_TERMINATION_COUNT)
        fail    = fail    | limit_fail
        success_candidate = success_candidate & ~limit_fail

        success_candidate = success_candidate & (~fail)
        self._success_steps = torch.where(
            success_candidate,
            self._success_steps + 1,
            torch.zeros_like(self._success_steps),
        )
        success = self._success_steps >= self.SUCCESS_REQUIRED_STEPS

        done = timeout | success | fail
        self._regrasp_count += regrasp_event.long()

        # ---- regrasp bonus: reward = 0 for opening alone; stronger penalty if z_error got worse ----
        # Scale: perfect 30 mm improvement → +250; 0 mm improvement → 0; worse → negative x5.
        # maximum z_improvement happens with acceleration 2*(g - mu_k*N) and the z_improvement is 
        # 0.5*(EE acceleration - g - mu_k*N)* t_pulse**2 = Z_improvement_est and based on actual gap 
        # and time by time acceleration of the robot EE, all of these only in comment to be in future 
        # in observation, or used to estimate mu_k * N
        z_err_before  = (self._release_cuboid_rel_z - self.desired_rel_z).abs()  # (N,)
        z_err_after   = (cuboid_rel_z               - self.desired_rel_z).abs()  # (N,)
        z_improvement = z_err_before - z_err_after                               # (N,) positive = closer
        self._push_history(self._z_improve_hist, z_improvement, regrasp_event)
        self._z_improve_sum += torch.where(
            regrasp_event, z_improvement, torch.zeros_like(z_improvement)
        )

        # ---- progressive shaping: potential-based, paid on change, not per step ----
        # gamma * Phi(s') - Phi(s). True terminals absorb, so Phi is zeroed there;
        # timeout keeps its potential because rsl_rl bootstraps truncated episodes.
        potential = self._progress_potential(z_err_after)
        terminal_potential = torch.where(
            success | fail, torch.zeros_like(potential), potential
        )
        progress_reward = self.REWARD_GAMMA * terminal_potential - self._prev_potential
        self._prev_potential = potential.clone()
        # print(f"z_improvement: {z_improvement.mean().item():.4f}, z_err_before: {z_err_before.mean().item():.4f}, z_err_after: {z_err_after.mean().item():.4f}")    
        raw_regrasp_bonus = (z_improvement.clamp(min=-0.05) * 15000.0).clamp(max=250.0)
        raw_regrasp_bonus = torch.where(
            raw_regrasp_bonus < 0.0,
            raw_regrasp_bonus * 5.0,
            raw_regrasp_bonus,
        )
        # Bonus only for the first REGRASP_BONUS_MAX_COUNT regrasps. _regrasp_count was
        # already incremented above, so the k-th regrasp event sees _regrasp_count == k.
        bonus_eligible = regrasp_event & (self._regrasp_count <= self.REGRASP_BONUS_MAX_COUNT)
        regrasp_bonus = (
            raw_regrasp_bonus * bonus_eligible.float()
        )  # (N,)

        # ---- jerk penalty: penalise jerky EE velocity commands ----
        jerk         = (self.target_z_vel - self.prev_target_z_vel) / self.Z_VEL_MAX  # (N,)
        # jerk_penalty = torch.where(fully_released, torch.zeros_like(jerk), -1.0 * jerk.pow(2))  # (N,)
        jerk_penalty = -0.1 * jerk.pow(2)

        # ---- velocity-minimization penalty: exponential in |commanded z-vel|, opt-in via --minimze_vel ----
        # Zero at/below VEL_CMD_PENALTY_DEADZONE, then -10 rising to -60 at Z_VEL_MAX.
        vel_cmd_penalty = torch.zeros_like(ee_z)
        if self.minimize_vel:
            z_vel_cmd = self.target_z_vel.abs()                                    # (N,)
            excess = (z_vel_cmd - self.VEL_CMD_PENALTY_DEADZONE).clamp(min=0.0)    # (N,)
            vel_cmd_penalty = torch.where(
                z_vel_cmd > self.VEL_CMD_PENALTY_DEADZONE,
                -self.VEL_CMD_PENALTY_MIN * torch.exp(self._vel_cmd_penalty_rate * excess),
                torch.zeros_like(z_vel_cmd),
            )

        # ---- terminal reward (dominates with gamma=0.99) ----
        ep = self.episode_length_buf.float()
        base_reward = torch.where(
            success,
            3000.0 - ep * 0.5,                          # up to 3000; small time penalty
            torch.where(
                fail | timeout,
                torch.full_like(ee_z, -250.0),
                torch.full_like(ee_z, -1.25),             # alive penalty: urgency to finish
            ),
        )

        # ---- post-pulse hold penalty: penalise instability after regrasp ----
        _in_delay_window = self._post_pulse_delay_countdown > 0
        _in_hold_window = (~_in_delay_window) & (self._post_pulse_hold_countdown > 0)  # (N,)
        
        # Penalize velocity if it exceeds the limit
        vel_err = (ee_vel_z.abs() - self.POST_PULSE_HOLD_VEL_MAX).clamp(min=0.0)
        # Penalize distance from target
        z_err = (ee_z - self.POST_PULSE_HOLD_Z_TARGET).abs()
        
        post_pulse_hold_penalty = torch.where(
            _in_hold_window,
            - (vel_err * self.POST_PULSE_HOLD_PENALTY_VEL) - (z_err * self.POST_PULSE_HOLD_PENALTY_Z),
            torch.zeros_like(ee_z),
        )  # (N,)
        
        # Decrement the countdowns
        self._post_pulse_hold_countdown = torch.where(
            (~_in_delay_window) & (self._post_pulse_hold_countdown > 0),
            self._post_pulse_hold_countdown - 1,
            self._post_pulse_hold_countdown,
        )
        self._post_pulse_delay_countdown = (self._post_pulse_delay_countdown - 1).clamp(min=0)

        reward = (base_reward + regrasp_bonus*2.0 + progress_reward + jerk_penalty
                  + post_pulse_hold_penalty + vel_cmd_penalty)

        # Accumulate each term's episode total; _update_episode_extras() reads these
        # for finished envs before _reset_idx() zeroes them.
        self._ep_term_sums["total"]   += reward
        self._ep_term_sums["base"]    += base_reward
        self._ep_term_sums["regrasp"] += regrasp_bonus
        self._ep_term_sums["progress"] += progress_reward
        self._ep_term_sums["jerk"]    += jerk_penalty
        self._ep_term_sums["vel_cmd"] += vel_cmd_penalty
        self._ep_term_sums["hold"]    += post_pulse_hold_penalty

        self.last_reward_terms = {
            "base_reward":   base_reward.detach().clone(),
            "regrasp_bonus": regrasp_bonus.detach().clone(),
            "progress_reward": progress_reward.detach().clone(),
            "potential": potential.detach().clone(),
            "jerk_penalty":  jerk_penalty.detach().clone(),
            "vel_cmd_penalty": vel_cmd_penalty.detach().clone(),
            "z_improvement": z_improvement.detach().clone(),
            "avg_force":     avg_force.detach().clone(),
            "regrasp_event": regrasp_event.float().detach().clone(),
            "success": success.float().detach().clone(),
            "fail": fail.float().detach().clone(),
            "timeout": timeout.float().detach().clone(),
            "done": done.float().detach().clone(),
            "success_candidate": success_candidate.float().detach().clone(),
            "success_steps": self._success_steps.float().detach().clone(),
            "post_pulse_hold_penalty": post_pulse_hold_penalty.detach().clone(),
        }

        return done, reward, timeout

    def _reset_idx(self, envs_idx: torch.Tensor):
        """Reset a subset of envs in-place; sim_step continues uninterrupted."""
        q = self.q_home.unsqueeze(0).expand(len(envs_idx), -1)
        self.franka.set_qpos(q, envs_idx=envs_idx, zero_velocity=True)
        self.franka.control_dofs_position(q, envs_idx=envs_idx)
        self._reset_cuboid_home_pose(envs_idx)

        ee_pos = self.ee_link.get_pos()    # FK updated by set_qpos
        ee_quat = self.ee_link.get_quat()
        self.target_center[envs_idx] = ee_pos[envs_idx].clone()
        self.target_quat[envs_idx] = ee_quat[envs_idx].clone()
        self.target_z[envs_idx] = ee_pos[envs_idx, 2].clone()
        self.target_z_vel[envs_idx] = 0.0
        self.target_z_acc[envs_idx] = 0.0
        self.prev_target_z_vel[envs_idx] = 0.0
        self.direction_change_count[envs_idx] = 0
        _n = len(envs_idx)
        _mag = 0.02 + torch.rand(_n, device=self.device) * 0.02   # uniform in [0.02, 0.04]
        if self.mix:
            _sign = torch.where(torch.rand(_n, device=self.device) < 0.5,
                                torch.ones(_n, device=self.device),
                                torch.full((_n,), -1.0, device=self.device))
            _mag = _sign * _mag   # randomly flip sign per env
        elif self.solid_up:
            _mag = -_mag   # negative desired_rel_z → solid-up training regime
        self.desired_rel_z[envs_idx] = _mag
        self.episode_length_buf[envs_idx] = 0
        for _term_sum in self._ep_term_sums.values():
            _term_sum[envs_idx] = 0.0
        self._in_release[envs_idx] = False
        self._regrasp_count[envs_idx] = 0
        self._release_start_step[envs_idx] = -1
        self._last_regrasp_duration_steps[envs_idx] = 0
        self._regrasp_duration_sum_steps[envs_idx] = 0
        self._regrasp_duration_max_steps[envs_idx] = 0
        self._regrasp_duration_steps[envs_idx] = 0
        self._prev_cuboid_rel_z[envs_idx] = 0.0
        self._release_cuboid_rel_z[envs_idx] = 0.0
        self._firm_grasp_steps[envs_idx] = 0
        self._success_steps[envs_idx] = 0
        self._filt_u1[envs_idx] = 0.0
        self._filt_u2[envs_idx] = 0.0
        self._filt_y1[envs_idx] = 0.0
        self._filt_y2[envs_idx] = 0.0
        self._gripper_pulse_steps[envs_idx] = 0
        self._sample_gripper_pulse_delays(envs_idx)
        self._sample_gripper_pulse_lengths(envs_idx)
        self._sample_zero_hold_steps(envs_idx)
        self._randomize_finger_gains(envs_idx)
        self._prev_gripper_avg[envs_idx] = self.gripper_pos_min.mean()
        self._zero_hold_countdown[envs_idx] = 0
        self._zero_wait_for_direction_change[envs_idx] = False
        self._prev_policy_z_vel_sign[envs_idx] = 0
        self._post_pulse_delay_countdown[envs_idx] = 0
        self._post_pulse_hold_countdown[envs_idx] = 0
        self._z_improve_hist[envs_idx] = 0.0
        self._z_improve_sum[envs_idx] = 0.0
        self._pulse_to_next_neg_vel_ms_hist[envs_idx] = 0.0
        self._pulse_to_last_neg_vel_ms_hist[envs_idx] = 0.0
        self._pulse_open_start_ms[envs_idx] = 0.0
        self._pulse_open_active[envs_idx] = False
        self._current_pulse_next_neg_ms[envs_idx] = -1.0
        self._current_pulse_last_neg_ms[envs_idx] = -1.0
        self._randomize_friction(envs_idx)
        self._sample_control_error(envs_idx)

        # ---- initial velocity randomization (domain randomization) ----
        if self.randomize:
            _n = len(envs_idx)
            qvel_noise = (torch.rand(_n, 7, device=self.device) * 2.0 - 1.0) * 0.01
            qvel = torch.zeros(_n, 9, device=self.device)
            qvel[:, :7] = qvel_noise
            self.franka.set_dofs_velocity(qvel, envs_idx=envs_idx)

        self._seed_progress_potential(envs_idx)

    # ------------------------------------------------------------------ #
    # Internal: friction randomisation                                    #
    # ------------------------------------------------------------------ #

    def _randomize_friction(self, envs_idx: torch.Tensor):
        """
        Sample a fresh friction ratio for each env in envs_idx and apply it
        to all franka + cuboid geoms.

        Effective contact friction = FRICTION_BASE * ratio,
        sampled uniformly so that the result lies in [FRICTION_MIN, FRICTION_MAX].
        """
        n = len(envs_idx)
        ratio_min = self.FRICTION_MIN / self.FRICTION_BASE  # 0.15 / 0.75 = 0.2
        ratio_max = self.FRICTION_MAX / self.FRICTION_BASE  # 0.90 / 0.75 = 1.2
        # Per-env ratio: (n,)
        ratio = ratio_min + torch.rand(n, device=self.device) * (ratio_max - ratio_min)
        # set_geoms_friction_ratio expects shape (n_envs, n_geoms)
        n_geoms = len(self._contact_geoms_idx)
        friction_ratio = ratio.unsqueeze(1).expand(n, n_geoms)  # (n, n_geoms)
        self.scene.sim.rigid_solver.set_geoms_friction_ratio(
            friction_ratio,
            geoms_idx=self._contact_geoms_idx,
            envs_idx=envs_idx,
        )

    def _sample_gripper_pulse_delays(self, envs_idx: torch.Tensor):
        """Set per-env pulse delays, optionally randomized per episode."""
        if self.randomize:
            self._gripper_pulse_delays[envs_idx] = torch.randint(
                self.PULSE_DELAY_RANDOM_MIN,
                self.PULSE_DELAY_RANDOM_MAX + 1,
                (len(envs_idx),),
                dtype=torch.long,
                device=self.device,
            )
        else:
            self._gripper_pulse_delays[envs_idx] = self.PULSE_DELAY_STEPS

    def _sample_gripper_pulse_lengths(self, envs_idx: torch.Tensor):
        """Set per-env pulse lengths, optionally randomized per episode."""
        if self.randomize:
            self._gripper_pulse_lengths[envs_idx] = torch.randint(
                self.PULSE_LENGTH_RANDOM_MIN,
                self.PULSE_LENGTH_RANDOM_MAX + 1,
                (len(envs_idx),),
                dtype=torch.long,
                device=self.device,
            )
        else:
            self._gripper_pulse_lengths[envs_idx] = self.PULSE_LENGTH

    def _sample_control_error(self, envs_idx: torch.Tensor):
        """Set a per-env constant z-velocity command bias for the whole episode."""
        if self.control_error:
            n = len(envs_idx)
            mag = self.CONTROL_ERROR_ABS_MIN + torch.rand(n, device=self.device) * (
                self.CONTROL_ERROR_ABS_MAX - self.CONTROL_ERROR_ABS_MIN
            )
            sign = torch.where(
                torch.rand(n, device=self.device) < 0.5,
                -torch.ones(n, device=self.device),
                torch.ones(n, device=self.device),
            )
            # self._episode_control_error[envs_idx] = sign * mag
            self._episode_control_error[envs_idx] = - mag
        else:
            self._episode_control_error[envs_idx] = 0.0

    def _progress_potential(self, z_err: torch.Tensor) -> torch.Tensor:
        """Exponential potential on |cuboid_rel_z - desired_rel_z|.

        PROGRESS_POTENTIAL_MAX at zero error, exactly 0 at PROGRESS_POTENTIAL_RANGE
        and beyond, decaying with an e-folding length of PROGRESS_POTENTIAL_DECAY.
        """
        scaled = z_err.clamp(0.0, self.PROGRESS_POTENTIAL_RANGE) / self.PROGRESS_POTENTIAL_DECAY
        edge = math.exp(-self.PROGRESS_POTENTIAL_RANGE / self.PROGRESS_POTENTIAL_DECAY)
        return self.PROGRESS_POTENTIAL_MAX * (torch.exp(-scaled) - edge) / (1.0 - edge)

    def _seed_progress_potential(self, envs_idx: torch.Tensor):
        """Seed Phi for freshly reset envs so step 1 pays no spurious jump."""
        cuboid_pos = self.cuboid.get_pos()
        left_ft = self._fingertip_pos(self.left_finger)
        right_ft = self._fingertip_pos(self.right_finger)
        finger_mid_z = (left_ft[:, 2] + right_ft[:, 2]) * 0.5
        z_err = (cuboid_pos[:, 2] - finger_mid_z - self.desired_rel_z).abs()
        self._prev_potential[envs_idx] = self._progress_potential(z_err)[envs_idx]

    def _sample_zero_hold_steps(self, envs_idx: torch.Tensor):
        """Set per-env zero-hold duration in high-level steps.

        If randomization is enabled, sample duration uniformly in [0.1s, 0.3s].
        Otherwise, use the fixed minimum duration (0.1s).
        """
        if self.randomize:
            duration_s = (
                self.ZERO_HOLD_DURATION_MIN
                + torch.rand(len(envs_idx), device=self.device)
                * (self.ZERO_HOLD_DURATION_MAX - self.ZERO_HOLD_DURATION_MIN)
            )
            steps = torch.round(duration_s / self.target_period).to(dtype=torch.long)
            self._zero_hold_steps[envs_idx] = torch.clamp(steps, min=1)
        else:
            fixed_steps = max(1, int(round(self.ZERO_HOLD_DURATION_MIN / self.target_period)))
            self._zero_hold_steps[envs_idx] = fixed_steps

    def _randomize_finger_gains(self, envs_idx: torch.Tensor):
        """Sample one shared finger value per env and apply it with preserved signs."""
        n = len(envs_idx)
        value = (
            self.FINGER_GAIN_RANDOM_MIN
            + torch.rand(n, device=self.device) * (self.FINGER_GAIN_RANDOM_MAX - self.FINGER_GAIN_RANDOM_MIN)
        )
        if not hasattr(self, "_finger_gain_values"):
            self._finger_gain_values = torch.zeros(self.num_envs, device=self.device)
        self._finger_gain_values[envs_idx] = value

        value_b = value.unsqueeze(-1).expand(n, len(self.fingers_dof))
        self.franka.set_dofs_kp(value_b, self.fingers_dof, envs_idx=envs_idx)
        self.franka.set_dofs_force_range(-value_b, value_b, self.fingers_dof, envs_idx=envs_idx)

    # ------------------------------------------------------------------ #
    # Internal: controller                                                #
    # ------------------------------------------------------------------ #

    def _control_once(self, target_z: torch.Tensor, target_z_vel: torch.Tensor):
        """
        One Jacobian-based velocity-IK step for all N envs simultaneously.

        target_z, target_z_vel: (N,)
        """
        N = self.num_envs

        # Build Cartesian target pos/vel (only z changes)
        target_pos = self.target_center.clone()          # (N, 3)
        target_pos[:, 2] = target_z
        target_vel = torch.zeros(N, 3, device=self.device)
        # 2nd-order low-pass filter: H(s) = 9025 / (s² + 100.8 s + 9025) at 1000 Hz
        # y[n] = b0*u[n] + b1*u[n-1] + b2*u[n-2] - a1*y[n-1] - a2*y[n-2]
        _u_n = target_z_vel
        target_z_vel = (
            self._filt_b0 * _u_n
            + self._filt_b1 * self._filt_u1
            + self._filt_b2 * self._filt_u2
            - self._filt_a1 * self._filt_y1
            - self._filt_a2 * self._filt_y2
        )
        self._filt_u2 = self._filt_u1.clone()
        self._filt_u1 = _u_n
        self._filt_y2 = self._filt_y1.clone()
        self._filt_y1 = target_z_vel.clone()
        target_vel[:, 2] = target_z_vel

        # EE state
        ee_pos = self.ee_link.get_pos()    # (N, 3)
        ee_quat = self.ee_link.get_quat()  # (N, 4)

        # Cartesian error
        error_pos = target_pos - ee_pos    # (N, 3)
        rel_quat = _tc_quat_mul(self.target_quat, _tc_inv_quat(ee_quat))  # (N, 4)
        error_rotvec = _tc_quat_to_rotvec(rel_quat)  # (N, 3)

        # ee_velocity_cmd: (N, 6)
        ee_vel_cmd = torch.cat([
            target_vel + self.pos_gain * error_pos,   # (N, 3)
            self.rot_gain * error_rotvec,              # (N, 3)
        ], dim=-1)

        # Jacobian: (N, 6, n_dof_total) → slice motor dofs → (N, 6, 7)
        J_full = self.franka.get_jacobian(link=self.ee_link)  # (N, 6, n_dof)
        J = J_full[:, :, self.motors_dof]  # (N, 6, 7)

        # Damped-least-squares solve: (J J^T + λI) x = ee_vel_cmd
        JJT = J @ J.transpose(-1, -2)  # (N, 6, 6)
        JJT_reg = JJT + self.jacobian_regularizer.unsqueeze(0)  # (N, 6, 6)

        # x: (N, 6)
        x = torch.linalg.solve(JJT_reg, ee_vel_cmd.unsqueeze(-1)).squeeze(-1)

        # qvel = J^T x  →  (N, 7)
        qvel = (J.transpose(-1, -2) @ x.unsqueeze(-1)).squeeze(-1)

        self.franka.control_dofs_velocity(qvel, dofs_idx_local=self.motors_dof)

    # ------------------------------------------------------------------ #
    # Internal: cubic-Hermite z reference                                 #
    # ------------------------------------------------------------------ #

    def _sample_target_z(self, t: float):
        """
        Returns z, z_vel tensors (N,) at global sim time t,
        interpolated along the current cubic-Hermite segment.
        """
        if self._seg_end is None:
            return self.target_z.clone(), self.target_z_vel.clone()

        local_t = torch.clamp(
            torch.full((self.num_envs,), t, device=self.device) - self._seg_t0,
            0.0, self.target_period,
        )
        s = (local_t / self.target_period).clamp(0.0, 1.0)  # (N,)
        s2, s3 = s * s, s * s * s

        z0, zv0 = self._seg_start[:, 0], self._seg_start[:, 1]
        z1, zv1 = self._seg_end[:, 0],   self._seg_end[:, 1]
        T = self.target_period

        h00 = 2 * s3 - 3 * s2 + 1
        h10 = s3 - 2 * s2 + s
        h01 = -2 * s3 + 3 * s2
        h11 = s3 - s2

        z = h00 * z0 + h10 * T * zv0 + h01 * z1 + h11 * T * zv1

        dh00 = 6 * s2 - 6 * s
        dh10 = 3 * s2 - 4 * s + 1
        dh01 = -6 * s2 + 6 * s
        dh11 = 3 * s2 - 2 * s
        z_vel = (dh00 * z0 + dh10 * T * zv0 + dh01 * z1 + dh11 * T * zv1) / T

        return z, z_vel

    # ------------------------------------------------------------------ #
    # Internal: utilities                                                 #
    # ------------------------------------------------------------------ #

    def _push_history(self, hist: torch.Tensor, values: torch.Tensor, mask: torch.Tensor):
        """Push scalar values into per-env fixed-length histories (oldest -> newest)."""
        idx = mask.nonzero(as_tuple=False).squeeze(-1)
        if idx.numel() == 0:
            return
        hist[idx, :-1] = hist[idx, 1:].clone()
        hist[idx, -1] = values[idx]

    def _fingertip_pos(self, finger_link) -> torch.Tensor:
        """Compute fingertip world position (N, 3) from link pose."""
        pos = finger_link.get_pos()    # (N, 3)
        quat = finger_link.get_quat()  # (N, 4)
        # Rotate local offset by link orientation, then add to link pos
        # transform_by_quat supports torch batched inputs
        from genesis.utils.geom import transform_by_quat as _tbq
        offset = self.fingertip_local.unsqueeze(0).expand(self.num_envs, -1)  # (N, 3)
        return pos + _tbq(offset, quat)

    def get_fingertip_distance(self) -> torch.Tensor:
        """Return per-env fingertip distance (N,)."""
        left_ft = self._fingertip_pos(self.left_finger)
        right_ft = self._fingertip_pos(self.right_finger)
        return (left_ft - right_ft).norm(dim=-1)

    def _reset_cuboid_home_pose(self, envs_idx: torch.Tensor):
        from genesis.utils.geom import transform_by_quat as _tbq, transform_quat_by_quat as _tqbq

        hand_pos = self.ee_link.get_pos()[envs_idx]    # (|idx|, 3)
        hand_quat = self.ee_link.get_quat()[envs_idx]  # (|idx|, 4)

        local_offset = torch.tensor([0.0, 0.0, 0.1029], device=self.device)
        local_quat_np = np.array([0.00187891, -0.71790805, -0.00193768, -0.69613270])
        local_quat_np /= np.linalg.norm(local_quat_np)
        local_quat = torch.tensor(local_quat_np, dtype=hand_quat.dtype, device=self.device)

        M = len(envs_idx)
        offset_batch = local_offset.unsqueeze(0).expand(M, -1)
        lq_batch = local_quat.unsqueeze(0).expand(M, -1)

        cuboid_pos = hand_pos + _tbq(offset_batch, hand_quat)
        cuboid_quat = _tqbq(lq_batch, hand_quat)

        self.cuboid.set_pos(cuboid_pos, zero_velocity=True, envs_idx=envs_idx)
        self.cuboid.set_quat(cuboid_quat, zero_velocity=True, envs_idx=envs_idx)

    def _set_franka_gains(self):
        kp_motors = torch.tensor([4500, 4500, 3500, 3500, 2000, 2000, 2000],
                                  dtype=torch.float32, device=self.device)
        kv_motors = torch.tensor([450, 450, 350, 350, 200, 200, 200],
                                  dtype=torch.float32, device=self.device)
        f_lo = torch.tensor([-87, -87, -87, -87, -12, -12, -12],
                             dtype=torch.float32, device=self.device)
        f_hi = torch.tensor([87, 87, 87, 87, 12, 12, 12],
                              dtype=torch.float32, device=self.device)

        envs_idx = torch.arange(self.num_envs, device=self.device)
        self.franka.set_dofs_kp(kp_motors.unsqueeze(0).expand(self.num_envs, -1), self.motors_dof, envs_idx=envs_idx)
        self.franka.set_dofs_kv(kv_motors.unsqueeze(0).expand(self.num_envs, -1), self.motors_dof, envs_idx=envs_idx)
        self.franka.set_dofs_force_range(
            f_lo.unsqueeze(0).expand(self.num_envs, -1),
            f_hi.unsqueeze(0).expand(self.num_envs, -1),
            self.motors_dof,
            envs_idx=envs_idx,
        )

        finger_kv = torch.full((self.num_envs, len(self.fingers_dof)), 25.0, device=self.device)
        self.franka.set_dofs_kv(finger_kv, self.fingers_dof, envs_idx=envs_idx)
        self._randomize_finger_gains(envs_idx)


# ---------------------------------------------------------------------------
# Minimal smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-B", "--num_envs", type=int, default=16)
    parser.add_argument("--vis", action="store_true")
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--limit-regrasp", action="store_true")
    parser.add_argument("--control-error", action="store_true")
    args = parser.parse_args()

    gs.init(backend=gs.gpu, precision="32", logging_level="warning")

    env = FrankaEnvParallel(
        num_envs=args.num_envs,
        vis=args.vis,
        limit_regrasp=args.limit_regrasp,
        control_error=args.control_error,
    )
    obs_td = env.reset()
    print("obs shape:", obs_td["policy"].shape)
    print("obs_dim:", FrankaEnvParallel.OBS_DIM)

    for i in range(args.steps):
        actions = torch.zeros(args.num_envs, 3, device=gs.device)
        obs_td, rew_buf, reset_buf, extras = env.step(actions)

    print("Done. ee_pos_z mean:", obs_td["policy"][:, FrankaEnvParallel.OBS_EE_POS_Z].mean().item())

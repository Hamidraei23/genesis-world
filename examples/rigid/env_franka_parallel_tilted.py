"""Franka regrasp with a configurable fixed grasp-axis tilt from 0 to 45 degrees.

``tilt_deg`` is inclination from world vertical, toward the pictured joint-5
homing direction (approximately world +Y), not an absolute Euler pitch angle.
The joint-5 rotation is calculated from forward kinematics. Zero reproduces the
original joint home, object alignment, and world-coordinate task axes.

All Z names refer to the fixed tilted grasp axis. Position coordinates are
HOME_TASK_Z plus displacement along that axis from home. Observations, reward
formulas, success/failure distances and hold targets use this same frame.
The observation layout remains compatible with existing policies; the tilt is
fixed for the run and is not an additional observation. Gravity stays world-down.
Transverse and orientation feedback hold the line and home quaternion; axial
position gain, action filter, reward weights and action scales are retained.

Train:
    python examples/rigid/train_franka_ppo.py --env env_franka_parallel_tilted --tilt-deg 20 -e franka-tilted20
Preview:
    python examples/rigid/env_franka_parallel_tilted.py -B 1 --vis --tilt-deg 45
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
    q_w = quat[..., :1]  # (..., 1)
    q_vec = quat[..., 1:]  # (..., 3)
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
    quat = torch.stack(
        [
            uw * vw - ux * vx - uy * vy - uz * vz,
            uw * vx + ux * vw + uy * vz - uz * vy,
            uw * vy - ux * vz + uy * vw + uz * vx,
            uw * vz + ux * vy - uy * vx + uz * vw,
        ],
        dim=-1,
    )
    return quat / quat.norm(dim=-1, keepdim=True).clamp(min=1e-8)


# ---------------------------------------------------------------------------
# Parallel Franka environment
# ---------------------------------------------------------------------------


class FrankaEnvParallelTilted:
    """
    Vectorised tilted Franka environment.

    Z is the fixed home grasp axis; it does not rotate with tracking errors.
    EE positions use HOME_TASK_Z + displacement from home along that axis.
    Cuboid relative X/Y/Z and EE velocities are projected into that same frame.

    Observations: flat tensor (N, OBS_DIM=10)
        [0]    ee_pos_z
        [1]    ee_vel_z
        [2]    target_z_vel
        [3]    target_z_acc
        [4]    left_force_mag   (scalar)
        [5]    right_force_mag  (scalar)
        [6]    cuboid_rel_z
        [7]    cuboid_rel_x
        [8]    cuboid_rel_y
        [9]    desired_rel_z

        Future Observations (To be added):
        [10]   last_pulse_gap_z_improve (LAST PULSE GAP z improvement)
        [11]   pulse_gap_duration (Duration of gap derived from finger pos)
        [12]   acc_drop_to_open_delay (Delay from the moment acceleration of EE goes below -10.0 to the moment end effector opens, in ms. Can be positive or negative)

    Actions (per env, shape (N, action_dim=3)):
        [0]   target_z_vel
        [1:3] gripper_pos (left, right finger)
    """

    DEFAULT_TILT_DEG = 30.0
    HOME_ARM_Q = (0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78)
    # Original environment's nominal FK hand height, retained as task origin value.
    HOME_TASK_Z = 0.854586497

    # Observation layout constants
    OBS_DIM = 10
    OBS_EE_POS_Z = 0
    OBS_EE_VEL_Z = 1
    OBS_TARGET_Z_VEL = 2
    OBS_TARGET_Z_ACC = 3
    OBS_LEFT_FORCE_MAG = 4
    OBS_RIGHT_FORCE_MAG = 5
    OBS_CUBOID_REL_Z = 6
    OBS_CUBOID_REL_X = 7
    OBS_CUBOID_REL_Y = 8
    OBS_DESIRED_REL_Z = 9
    # Future Observations (To be added):
    # OBS_LAST_PULSE_GAP_Z_IMPROVE = 10
    # OBS_PULSE_GAP_DURATION       = 11
    # OBS_ACC_DROP_TO_OPEN_DELAY   = 12

    # Fixed observation normalization scales (divide raw obs by these)
    # Order: ee_pos_z, ee_vel_z, target_z_vel, target_z_acc,
    #        left_force_mag, right_force_mag,
    #        cuboid_rel_z, cuboid_rel_x, cuboid_rel_y, desired_rel_z
    #        (Future: last_pulse_gap_z_improve, pulse_gap_duration, acc_drop_to_open_delay)
    OBS_SCALE = [1.0, 0.6, 0.6, 15.0, 5.0, 5.0, 0.05, 0.05, 0.05, 0.05]

    # Weights applied to each additive reward component in _compute_done_and_reward.
    # Also read by reward_table.RewardTermTracker for the per-iteration table.
    # Only the keys listed here are summed into rew_buf. jerk_penalty,
    # vel_sign_flip_penalty and post_pulse_hold_penalty are still computed (their
    # countdown side effects are needed) but no longer contribute; add a key back
    # here and to last_reward_terms to re-enable one.
    # base_reward carries the terminal signal (success payout, fail/timeout penalty,
    # per-step alive cost). Without it the dense proximity term makes loitering near
    # the target strictly better than finishing, so it stays in the sum.
    REWARD_TERM_WEIGHTS = {
        "base_reward": 1.0,
        "proximity_reward": 1.0,
        "regrasp_bonus": 2.0,
        "z_acc_penalty": 1.0,
    }

    # Action scaling constants
    Z_VEL_MAX = 0.45
    Z_ACC_MAX = 15.00  # m/s² — hard limit on target-velocity rate of change
    Z_ACC_PENALTY_THRESHOLD = 5.0
    Z_ACC_PENALTY_WEIGHT = 2.0
    EE_Z_TARGET = 0.7
    GRIPPER_CLOSED = 0.000251
    GRIPPER_OPEN = 0.0130  # per finger; gap 26.0 mm clears the 25 mm block by 1.0 mm
    # Release-to-regrasp detection
    FORCE_FREE_THRESHOLD = 0.15  # N: avg finger force below this -> fully released
    REGRASP_FORCE_THRESHOLD = 0.75  # N: avg finger force at/above this -> firm grasp
    AMBIGUOUS_FORCE_PENALTY = 10.0
    FREE_FORCE_REWARD = 1.0
    REGRASP_BONUS = 75.0  # duration-weighted reward scale for successful regrasp events
    REGRASP_TERMINATION_COUNT = 7  # fail on the 7th regrasp if not successful by then
    REGRASP_BONUS_MAX_COUNT = 4  # regrasp bonus paid only for the first 4 regrasps
    FIRM_GRASP_SLIP_PENALTY_WEIGHT = 30.0  # harsh penalty per (m/step)² of Z-slip during firm grasp
    SUCCESS_EE_Z_MIN = 0.7
    SUCCESS_EE_Z_MAX = 0.86
    SUCCESS_EE_VEL_MAX = 0.05  # m/s: |ee_vel_z| must be below this to count as settled
    # Terminal and per-step base reward. Failing must always cost more than surviving to
    # the timeout, otherwise ending an episode early is the cheapest outcome. The fail
    # penalty is derived from these in _compute_done_and_reward, so that stays true if
    # any of them or max_episode_length change.
    SUCCESS_REWARD = 3750.0  # +25 % over the previous 3000
    SUCCESS_TIME_PENALTY = 0.5  # per episode step, subtracted from SUCCESS_REWARD
    ALIVE_PENALTY = 1.25  # per non-terminal step
    TIMEOUT_PENALTY = 250.0
    FAIL_PENALTY_MARGIN = 100.0  # fail = TIMEOUT + ALIVE * max_episode_length + margin
    SUCCESS_REQUIRED_STEPS = 5
    # Dense proximity reward on the grasp-axis gap to the commanded offset.
    # r(d) = MAX * (exp(-K*d/RANGE) - exp(-K)) / (1 - exp(-K)), clamped to [0, MAX].
    # Exponential in d, so it climbs steeply only near the target: MAX at d = 0,
    # exactly 0 at d = RANGE, and clamped to 0 beyond it -- never negative.
    # With the values below: 0 mm -> 20.0, 5 mm -> 12.1, 10 mm -> 7.3, 20 mm -> 2.6.
    # Scaled so a full 450-step episode of loitering near the target returns roughly
    # 475, well under SUCCESS_REWARD, leaving the terminal signal in charge.
    PROXIMITY_REWARD_MAX = 20.0
    PROXIMITY_REWARD_RANGE = 0.05  # m: gap at which the reward reaches zero
    PROXIMITY_REWARD_SHARPNESS = 5.0  # K: larger = more concentrated near d = 0
    EE_HOLD_Z_TARGET = 0.8
    EE_HOLD_Z_TOLERANCE = 0.025
    EE_HOLD_VEL_TOLERANCE = 0.02
    EE_HOLD_REQUIRED_STEPS = 5
    EE_HOLD_ACC_THRESHOLD = 2.0
    PULSE_DELAY_STEPS = 2  # target-period steps to wait before the open window begins
    PULSE_DELAY_RANDOM_MIN = 1  # 20 ms
    PULSE_DELAY_RANDOM_MAX = 2  # 40 ms
    PULSE_LENGTH = 5  # steps: open window, then 1 close step, back to policy control
    # The policy keeps commanding "open" through the delay, so the object is released for
    # PULSE_DELAY + PULSE_LENGTH - 1 steps: 6 steps = 120 ms with the fixed values above.
    # Holding the randomised length at 5 and letting the delay pick 1 or 2 steps keeps the
    # randomised release inside 100-120 ms.
    PULSE_LENGTH_RANDOM_MIN = 5
    PULSE_LENGTH_RANDOM_MAX = 5
    # Pulse lockout: no new pulse may start until this long after the previous TRIGGER.
    PULSE_START_MIN_STEPS = 50  # no pulse may fire in the first N steps of an episode
    PULSE_LOCKOUT_DURATION = 1.2  # s since the trigger before another pulse may fire
    # Commanded-velocity sign flip, charged at every step (not only in the lockout) and in
    # proportion to the size of the jump across zero: a 0.2 m/s flip costs 5, 0.8 m/s costs 20.
    VEL_SIGN_FLIP_PENALTY_PER_MPS = 25.0
    # Post-pulse hold penalty: after pulse completes, wait 0.5s, then penalise instability for 0.5s
    POST_PULSE_DELAY_DURATION = 0.5  # wait before evaluation window
    POST_PULSE_HOLD_DURATION = 0.5  # seconds
    POST_PULSE_HOLD_PENALTY_Z = 200.0  # per-step penalty weight for distance from target
    POST_PULSE_HOLD_PENALTY_VEL = 200.0  # per-step penalty weight for velocity excess
    POST_PULSE_HOLD_Z_TARGET = 0.8  # desired ee_z during hold
    POST_PULSE_HOLD_VEL_MAX = 0.03  # deadzone for velocity penalty
    FINGER_GAIN_RANDOM_MIN = 100.0
    FINGER_GAIN_RANDOM_MAX = 500.0
    # Friction domain randomisation: effective contact μ sampled each episode
    FRICTION_BASE = 0.75  # sliding friction in the MJCF files (dominant value)
    FRICTION_MIN = 0.6  # minimum desired effective contact friction
    FRICTION_MAX = 0.90  # maximum desired effective contact friction

    def __init__(
        self,
        num_envs: int = 1,
        *,
        tilt_deg: float = DEFAULT_TILT_DEG,
        vis: bool = False,
        record: bool = False,
        dt: float = 0.001,
        target_dt: float = 0.02,
        gripper_pos_min: float = 0.000251,
        gripper_pos_max: float = 0.0130,
        pos_gain: float = 8.0,
        rot_gain: float = 40.0,
        transverse_pos_gain: float = 80.0,
        jacobian_damping: float = 1e-4,
        limit_regrasp: bool = False,
        solid_up: bool = False,
        mix: bool = False,
        randomize: bool = False,
        normalize: bool = False,
    ):
        self.tilt_deg = float(tilt_deg)
        if not math.isfinite(self.tilt_deg) or not 0.0 <= self.tilt_deg <= 45.0:
            raise ValueError("tilt_deg must be a finite angle between 0 and 45 degrees")
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
        self.extras: dict = {}
        self.cfg = {
            "environment": "env_franka_parallel_tilted",
            "coordinate_frame": "fixed_home_grasp",
            "tilt_deg": self.tilt_deg,
            "home_task_z": self.HOME_TASK_Z,
            "transverse_pos_gain": transverse_pos_gain,
            "rot_gain": rot_gain,
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
            "success_ee_z_min": self.SUCCESS_EE_Z_MIN,
            "success_ee_z_max": self.SUCCESS_EE_Z_MAX,
            "success_ee_vel_max": self.SUCCESS_EE_VEL_MAX,
            "success_reward": self.SUCCESS_REWARD,
            "alive_penalty": self.ALIVE_PENALTY,
            "timeout_penalty": self.TIMEOUT_PENALTY,
            "fail_penalty": self.fail_penalty(),
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
            "pulse_start_min_steps": self.PULSE_START_MIN_STEPS,
            "pulse_lockout_duration": self.PULSE_LOCKOUT_DURATION,
            "vel_sign_flip_penalty_per_mps": self.VEL_SIGN_FLIP_PENALTY_PER_MPS,
            "z_acc_penalty_threshold": self.Z_ACC_PENALTY_THRESHOLD,
            "z_acc_penalty_weight": self.Z_ACC_PENALTY_WEIGHT,
            "finger_gain_random_min": self.FINGER_GAIN_RANDOM_MIN,
            "finger_gain_random_max": self.FINGER_GAIN_RANDOM_MAX,
        }

        self.gripper_pos_min = torch.tensor([gripper_pos_min, gripper_pos_min], device=self.device)
        self.gripper_pos_max = torch.tensor([gripper_pos_max, gripper_pos_max], device=self.device)

        # Controller gains (scalar – same for all envs)
        self.pos_gain = pos_gain
        self.rot_gain = rot_gain
        self.transverse_pos_gain = transverse_pos_gain
        # Jacobian regulariser: (6, 6), broadcast over batch in _control_once
        reg = jacobian_damping * torch.eye(6, device=self.device)
        self.jacobian_regularizer = reg  # (6, 6)

        # ------------------------------------------------------------------ #
        # 2nd-order low-pass filter: H(s) = 5625 / (s² + 49.5 s + 5625)      #
        # wn = 75 rad/s, zeta = 0.33: ~13 ms of command lag and visible overshoot, #
        # tuned against the real arm with examples/rigid/tune_ee_vel_controller.py #
        # Discretised via bilinear (Tustin) transform at the sim rate T=dt.   #
        # Runs at 1000 Hz inside _control_once().                              #
        # ------------------------------------------------------------------ #
        _T = self.dt
        _k = 2.0 / _T
        _wn2 = 5625.0  # wn = 75 rad/s
        _blin = 49.5  # 2 * zeta * wn, zeta = 0.33
        _a0 = _k**2 + _blin * _k + _wn2
        self._filt_b0 = _wn2 / _a0
        self._filt_b1 = (2.0 * _wn2) / _a0
        self._filt_b2 = _wn2 / _a0
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
            self.franka.geom_start,
            self.franka.geom_start + self.franka.n_geoms,
            device=self.device,
        )
        _cuboid_geom_idx = torch.arange(
            self.cuboid.geom_start,
            self.cuboid.geom_start + self.cuboid.n_geoms,
            device=self.device,
        )
        self._contact_geoms_idx = torch.cat([_franka_geom_idx, _cuboid_geom_idx])  # (n_contact_geoms,)

        # ------------------------------------------------------------------ #
        # DOF / link indices                                                  #
        # ------------------------------------------------------------------ #
        self.motors_dof = torch.arange(7, device=self.device)
        self.fingers_dof = torch.arange(7, 9, device=self.device)
        self.q_home = torch.tensor(
            [*self.HOME_ARM_Q, 0.008090, 0.008090],
            device=self.device,
        )  # (9,)

        self.left_finger = self.franka.get_link("left_finger")
        self.right_finger = self.franka.get_link("right_finger")
        self.ee_link = self.franka.get_link("hand")
        self._configure_home_pose()

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
        self.franka.set_qpos(q_home_batch, envs_idx=envs_idx, zero_velocity=True)
        self.franka.control_dofs_position(q_home_batch, envs_idx=envs_idx)

        # ---- cuboid ----
        self._reset_task_frame(envs_idx)
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
            # Steps since the last pulse trigger. Gates the lockout; starts "expired" so the
            # first pulse can fire at once.
            self._pulse_lockout_steps = max(1, int(round(self.PULSE_LOCKOUT_DURATION / self.target_period)))
            self._steps_since_pulse = torch.full(
                (N,), self._pulse_lockout_steps + 1, dtype=torch.long, device=self.device
            )
            # Post-pulse hold countdown: high-level steps remaining in the hold window
            _delay_hl_steps = max(1, int(round(self.POST_PULSE_DELAY_DURATION / self.target_period)))
            _hold_hl_steps = max(1, int(round(self.POST_PULSE_HOLD_DURATION / self.target_period)))
            self._post_pulse_delay_total = _delay_hl_steps
            self._post_pulse_hold_total = _hold_hl_steps
            self._post_pulse_delay_countdown = torch.zeros(N, dtype=torch.long, device=self.device)
            self._post_pulse_hold_countdown = torch.zeros(N, dtype=torch.long, device=self.device)
            # Cubic-hermite segment state per env
            self._seg_start = None  # (N, 3): (z, z_vel, z_acc)
            self._seg_end = None  # (N, 3)
            self._seg_t0 = torch.zeros(N, device=self.device)  # wall-time at segment start

        # Warmup first so ee_link.get_pos() is valid
        for _ in range(warmup_steps):
            q_home_batch_all = self.q_home.unsqueeze(0).expand(N, -1)
            self.franka.control_dofs_position(q_home_batch_all)
            self.scene.step()

        self.target_center[envs_idx] = self.task_origin[envs_idx]
        self.target_quat[envs_idx] = self.task_quat[envs_idx]
        self.target_z[envs_idx] = self.HOME_TASK_Z
        self.target_z_vel[envs_idx] = 0.0
        self.target_z_acc[envs_idx] = 0.0
        self.prev_target_z_vel[envs_idx] = 0.0
        self.direction_change_count[envs_idx] = 0
        _n = len(envs_idx)
        _mag = 0.01 + torch.rand(_n, device=self.device) * 0.01  # uniform in [0.01, 0.04]
        # _mag = 0.0375
        if self.mix:
            _sign = torch.where(
                torch.rand(_n, device=self.device) < 0.5,
                torch.ones(_n, device=self.device),
                torch.full((_n,), -1.0, device=self.device),
            )
            _mag = _sign * _mag  # randomly flip sign per env
        elif self.solid_up:
            _mag = -_mag  # negative desired_rel_z → solid-up training regime
        self.desired_rel_z[envs_idx] = _mag
        self.episode_length_buf[envs_idx] = 0
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
        self._randomize_finger_gains(envs_idx)
        self._prev_gripper_avg[envs_idx] = self.gripper_pos_min.mean()
        self._post_pulse_delay_countdown[envs_idx] = 0
        self._post_pulse_hold_countdown[envs_idx] = 0
        self._steps_since_pulse[envs_idx] = self._pulse_lockout_steps + 1

        self._seg_start = None
        self._seg_end = None
        self._seg_t0 = torch.zeros(N, device=self.device)

        self._randomize_friction(envs_idx)

        # ---- initial velocity randomization (domain randomization) ----
        if self.randomize:
            _n = len(envs_idx)
            # Add small random joint velocities to arm DOFs (±0.01 rad/s)
            qvel_noise = (torch.rand(_n, 7, device=self.device) * 2.0 - 1.0) * 0.01
            qvel = torch.zeros(_n, 9, device=self.device)
            qvel[:, :7] = qvel_noise
            self.franka.set_dofs_velocity(qvel, envs_idx=envs_idx)

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

        new_z_vel = actions[:, 0].clamp(-1.0, 1.0) * self.Z_VEL_MAX  # (N,)
        # Limit acceleration: |Δv| ≤ Z_ACC_MAX * target_period
        max_dv = self.Z_ACC_MAX * self.target_period
        new_z_vel = new_z_vel.clamp(self.target_z_vel - max_dv, self.target_z_vel + max_dv)
        gripper_raw = actions[:, 1:].clamp(-1.0, 1.0)  # (N, 2)
        gripper_pos = self.gripper_pos_min + (gripper_raw + 1.0) * 0.5 * (
            self.gripper_pos_max - self.gripper_pos_min
        )  # (N, 2)

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
        _gp_avg = gripper_pos.mean(dim=-1)  # (N,)
        # A pulse is blocked while the lockout runs and during the opening steps of an
        # episode. The clamp below holds the fingers shut outside pulses, so neither
        # block can be sidestepped by commanding the gripper open directly.
        _pulse_blocked = (self._steps_since_pulse < self._pulse_lockout_steps) | (
            self.episode_length_buf < self.PULSE_START_MIN_STEPS
        )  # (N,)
        _rising = (
            (self._prev_gripper_avg < _gp_mid)
            & (_gp_avg >= _gp_mid)
            & (self._gripper_pulse_steps == 0)
            & (~_pulse_blocked)
        )  # (N,) rising-edge crossing, no active pulse, nothing blocking
        self._gripper_pulse_steps = torch.where(_rising, _pulse_start, self._gripper_pulse_steps)
        # Trigger restarts the clock the lockout and the magnified window both read.
        self._steps_since_pulse = torch.where(
            _rising, torch.zeros_like(self._steps_since_pulse), self._steps_since_pulse
        )
        _gp_max_b = self.gripper_pos_max.unsqueeze(0).expand(self.num_envs, -1)  # (N, 2)
        _gp_min_b = self.gripper_pos_min.unsqueeze(0).expand(self.num_envs, -1)  # (N, 2)
        _in_open = (self._gripper_pulse_steps >= 2) & (self._gripper_pulse_steps <= self._gripper_pulse_lengths)
        _in_close = self._gripper_pulse_steps == 1
        # Clamp: whenever no pulse is running the fingers are held fully closed, whatever
        # the policy asks. The finger action is therefore only a pulse trigger: it can
        # neither open the gripper directly nor loosen the squeeze below full force.
        _locked_out = self._gripper_pulse_steps == 0  # (N,)
        gripper_pos = torch.where(
            _in_open.unsqueeze(-1),
            _gp_max_b,
            torch.where((_in_close | _locked_out).unsqueeze(-1), _gp_min_b, gripper_pos),
        )
        # Edge detection remembers the EFFECTIVE command, not the raw request. A policy that
        # holds "open" across the whole lockout therefore still produces a rising edge the
        # moment the lockout expires, instead of silently opening the fingers with no pulse.
        self._prev_gripper_avg = gripper_pos.mean(dim=-1)
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
            tz, tz_vel = self._sample_target_z(t)  # (N,), (N,)
            self._control_once(tz, tz_vel)
            self.scene.step(
                update_visualizer=update_visualizer,
                refresh_visualizer=refresh_visualizer,
            )

        self.sim_step += self.target_update_every
        self.episode_length_buf += 1
        done, reward, timeout = self._compute_done_and_reward()
        done_idx = done.nonzero(as_tuple=False).squeeze(-1)
        if done_idx.numel() > 0:
            self._reset_idx(done_idx)
        self.rew_buf = reward
        self.reset_buf = done
        self.extras["time_outs"] = timeout.float()
        self._update_obs_buf()
        return self.get_observations(), self.rew_buf, self.reset_buf, self.extras

    # ------------------------------------------------------------------ #
    # Observations                                                        #
    # ------------------------------------------------------------------ #

    def _update_obs_buf(self):
        """Compute observations and write into self.obs_buf."""
        ee_pos = self.ee_link.get_pos()  # (N, 3)
        ee_vel = self.ee_link.get_vel()  # (N, 3)
        cuboid_pos = self.cuboid.get_pos()  # (N, 3)
        left_ft = self._fingertip_pos(self.left_finger)  # (N, 3)
        right_ft = self._fingertip_pos(self.right_finger)  # (N, 3)

        link_forces = self.franka.get_links_net_contact_force()  # (N, n_links, 3)
        left_force = link_forces[:, self.left_finger.idx_local, :]  # (N, 3)
        right_force = link_forces[:, self.right_finger.idx_local, :]  # (N, 3)
        left_force_mag = left_force.norm(dim=-1, keepdim=True)  # (N, 1)
        right_force_mag = right_force.norm(dim=-1, keepdim=True)  # (N, 1)

        finger_mid = (left_ft + right_ft) / 2.0  # (N, 3)
        cuboid_rel = self._world_vector_to_task(cuboid_pos - finger_mid)
        ee_task_z = self._task_position_z(ee_pos)
        ee_task_vel = self._world_vector_to_task(ee_vel)
        _noise_range = 0.003 if self.randomize else 0.0
        _N = self.num_envs

        def _unoise(shape):
            return (torch.rand(shape, device=self.device) * 2.0 - 1.0) * _noise_range

        self.obs_buf = torch.cat(
            [
                ee_task_z.unsqueeze(-1) + _unoise((_N, 1)),  # [0]    ee_pos_z
                ee_task_vel[:, 2:3] + _unoise((_N, 1)),  # [1]    ee_vel_z
                self.target_z_vel.unsqueeze(-1),  # [2]    target_z_vel
                self.target_z_acc.unsqueeze(-1),  # [3]    target_z_acc
                left_force_mag,  # [4]    left_force_mag
                right_force_mag,  # [5]    right_force_mag
                cuboid_rel[:, 2:3] + _unoise((_N, 1)),  # [6]    cuboid_rel_z
                cuboid_rel[:, 0:1] + _unoise((_N, 1)),  # [7]    cuboid_rel_x
                cuboid_rel[:, 1:2] + _unoise((_N, 1)),  # [8]    cuboid_rel_y
                self.desired_rel_z.unsqueeze(-1),  # [9]    desired_rel_z
                # TODO: Future observations to be appended here:
                # [10] last_pulse_gap_z_improve: LAST PULSE GAP z improvement
                # [11] pulse_gap_duration: duration of gap derived from finger pos
                # [12] acc_drop_to_open_delay: delay (ms) from EE acceleration going below -10.0 to EE open (can be +/-)
            ],
            dim=-1,
        )  # (N, 10)

        if self.normalize:
            if not hasattr(self, "_obs_scale_t"):
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

    def fail_penalty(self) -> float:
        """Smallest-plus-margin fail cost that is always worse than timing out.

        Failing at step t costs ALIVE*(t-1) + F, timing out costs ALIVE*(L-1) + TIMEOUT.
        F > TIMEOUT + ALIVE*L makes the first larger for every t >= 1.
        """
        return self.TIMEOUT_PENALTY + self.ALIVE_PENALTY * self.max_episode_length + self.FAIL_PENALTY_MARGIN

    def _compute_done_and_reward(self) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Simple 3-term reward:
          1. Terminal:       success +SUCCESS_REWARD (minus time penalty), timeout
                             -TIMEOUT_PENALTY, fail -fail_penalty() (always worse than timeout)
          2. Regrasp bonus:  up to +250 per regrasp, ONLY if z_error improved; zero otherwise
          3. Jerk penalty:   -0.5 * (Δv / Z_VEL_MAX)^2 per step

        Opening the gripper alone gives no reward — only completing release→regrasp
        that moves the cuboid closer to desired_rel_z is rewarded.
        Recommended gamma: 0.99 (terminal signal meaningful up to ~200 steps out).
        """
        cuboid_pos = self.cuboid.get_pos()  # (N, 3)
        ee_pos = self.ee_link.get_pos()  # (N, 3)
        ee_vel = self.ee_link.get_vel()  # (N, 3)
        left_ft = self._fingertip_pos(self.left_finger)  # (N, 3)
        right_ft = self._fingertip_pos(self.right_finger)  # (N, 3)
        finger_mid = (left_ft + right_ft) / 2.0  # (N, 3)
        fingertip_dist = (left_ft - right_ft).norm(dim=-1)  # (N,)

        link_forces = self.franka.get_links_net_contact_force()  # (N, n_links, 3)
        left_force_mag = link_forces[:, self.left_finger.idx_local, :].norm(dim=-1)  # (N,)
        right_force_mag = link_forces[:, self.right_finger.idx_local, :].norm(dim=-1)  # (N,)
        avg_force = (left_force_mag + right_force_mag) * 0.5  # (N,)

        cuboid_rel = self._world_vector_to_task(cuboid_pos - finger_mid)
        cuboid_rel_x, cuboid_rel_y, cuboid_rel_z = cuboid_rel.unbind(dim=-1)
        ee_z = self._task_position_z(ee_pos)
        ee_vel_z = self._world_vector_to_task(ee_vel)[:, 2]

        # ---- done conditions ----
        timeout = self.episode_length_buf >= self.max_episode_length  # (N,)

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
            & (ee_vel_z.abs() < self.SUCCESS_EE_VEL_MAX)
            & (self._firm_grasp_steps >= 3)
            & (ee_z >= self.SUCCESS_EE_Z_MIN)
            & (ee_z <= self.SUCCESS_EE_Z_MAX)
        )
        # Named so the training table can report which one ends each episode.
        fail_rel_x = cuboid_rel_x.abs() > 0.04
        fail_rel_y = cuboid_rel_y.abs() > 0.04
        fail_fingertip = fingertip_dist < 0.01
        fail_rel_z = cuboid_rel_z.abs() > 0.15
        fail_ee_low = ee_z < 0.6
        fail_ee_high = ee_z > 0.96
        fail = fail_rel_x | fail_rel_y | fail_fingertip | fail_rel_z | fail_ee_low | fail_ee_high

        # ---- regrasp event tracking ----
        firm_grasp = firm_grasp_now  # (N,)
        fully_released = avg_force < self.FORCE_FREE_THRESHOLD  # (N,)

        # Snapshot cuboid_rel_z at the start of each release window
        release_start = fully_released & (~self._in_release)
        self._release_start_step = torch.where(release_start, self.episode_length_buf, self._release_start_step)
        self._release_cuboid_rel_z = torch.where(release_start, cuboid_rel_z, self._release_cuboid_rel_z)

        # A regrasp event = was in release → now firm grasp
        regrasp_event = self._in_release & firm_grasp  # (N,)
        self._in_release = (self._in_release | fully_released) & (~regrasp_event)

        # Optional: terminate episode after too many regrasps
        # Always terminate as failure on the REGRASP_TERMINATION_COUNT-th regrasp
        limit_fail = regrasp_event & (self._regrasp_count + 1 >= self.REGRASP_TERMINATION_COUNT)
        fail = fail | limit_fail
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
        # All distances are along the fixed grasp axis. Gravity in these coordinates
        # is the projection of world gravity; no vertical free-fall estimate is used.
        z_err_before = (self._release_cuboid_rel_z - self.desired_rel_z).abs()  # (N,)
        z_err_after = (cuboid_rel_z - self.desired_rel_z).abs()  # (N,)
        z_improvement = z_err_before - z_err_after  # (N,) positive = closer
        raw_regrasp_bonus = (z_improvement.clamp(min=-0.05) * 15000.0).clamp(max=250.0)
        raw_regrasp_bonus = torch.where(
            raw_regrasp_bonus < 0.0,
            raw_regrasp_bonus * 5.0,
            raw_regrasp_bonus,
        )
        # Bonus only for the first REGRASP_BONUS_MAX_COUNT regrasps. _regrasp_count was
        # already incremented above, so the k-th regrasp event sees _regrasp_count == k.
        bonus_eligible = regrasp_event & (self._regrasp_count <= self.REGRASP_BONUS_MAX_COUNT)
        regrasp_bonus = raw_regrasp_bonus * bonus_eligible.float()  # (N,)

        # ---- proximity reward: dense, exponential, never negative ----
        # Distance along the fixed grasp axis between where the cuboid sits in the
        # fingers and where it was asked to sit. Paid every step, so it shapes the
        # approach instead of only scoring the regrasp that caused it.
        proximity_gap = (cuboid_rel_z - self.desired_rel_z).abs()  # (N,) metres
        _k = self.PROXIMITY_REWARD_SHARPNESS
        _floor = math.exp(-_k)
        _decay = torch.exp(-_k * proximity_gap / self.PROXIMITY_REWARD_RANGE)
        proximity_reward = self.PROXIMITY_REWARD_MAX * (_decay - _floor) / (1.0 - _floor)
        proximity_reward = proximity_reward.clamp(0.0, self.PROXIMITY_REWARD_MAX)  # (N,)

        # ---- jerk penalty: penalise jerky EE velocity commands ----
        jerk = (self.target_z_vel - self.prev_target_z_vel) / self.Z_VEL_MAX  # (N,)
        # jerk_penalty = torch.where(fully_released, torch.zeros_like(jerk), -1.0 * jerk.pow(2))  # (N,)
        jerk_penalty = -0.2 * jerk.pow(2)

        # ---- commanded-velocity sign flips ----
        # Charged at every step the commanded z velocity reverses, in proportion to how far
        # apart the two samples are. torch.sign is 0 at exactly zero, so a product below zero
        # means both samples are non-zero and point opposite ways.
        _sign_flip = (torch.sign(self.prev_target_z_vel) * torch.sign(self.target_z_vel)) < 0
        _flip_jump = (self.target_z_vel - self.prev_target_z_vel).abs()  # (N,) m/s
        self.direction_change_count = self.direction_change_count + _sign_flip.long()
        vel_sign_flip_penalty = -self.VEL_SIGN_FLIP_PENALTY_PER_MPS * _flip_jump * _sign_flip.float()  # (N,)

        # ---- grasp-axis acceleration penalty ----
        # Only the commanded acceleration beyond Z_ACC_PENALTY_THRESHOLD costs anything,
        # normalised by Z_ACC_MAX so the term is dimensionless like the jerk penalty.
        # Same weight at every step, with or without the pulse lockout.
        _acc_excess = (self.target_z_acc.abs() - self.Z_ACC_PENALTY_THRESHOLD).clamp(min=0.0) / self.Z_ACC_MAX  # (N,)
        z_acc_penalty = -self.Z_ACC_PENALTY_WEIGHT * _acc_excess.pow(2)  # (N,)

        # Advance the trigger clock, capped one past the lockout so the counter cannot
        # grow without bound.
        self._steps_since_pulse = (self._steps_since_pulse + 1).clamp(max=self._pulse_lockout_steps + 1)

        # ---- terminal reward (dominates with gamma=0.99) ----
        ep = self.episode_length_buf.float()
        base_reward = torch.where(
            success,
            self.SUCCESS_REWARD - ep * self.SUCCESS_TIME_PENALTY,
            torch.where(
                fail,  # checked before timeout: fail wins
                torch.full_like(ee_z, -self.fail_penalty()),
                torch.where(
                    timeout,
                    torch.full_like(ee_z, -self.TIMEOUT_PENALTY),
                    torch.full_like(ee_z, -self.ALIVE_PENALTY),  # urgency to finish
                ),
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
            -(vel_err * self.POST_PULSE_HOLD_PENALTY_VEL) - (z_err * self.POST_PULSE_HOLD_PENALTY_Z),
            torch.zeros_like(ee_z),
        )  # (N,)

        # Decrement the countdowns
        self._post_pulse_hold_countdown = torch.where(
            (~_in_delay_window) & (self._post_pulse_hold_countdown > 0),
            self._post_pulse_hold_countdown - 1,
            self._post_pulse_hold_countdown,
        )
        self._post_pulse_delay_countdown = (self._post_pulse_delay_countdown - 1).clamp(min=0)

        _w = self.REWARD_TERM_WEIGHTS
        reward = (
            _w["base_reward"] * base_reward
            + _w["proximity_reward"] * proximity_reward
            + _w["regrasp_bonus"] * regrasp_bonus
            + _w["z_acc_penalty"] * z_acc_penalty
        )

        self.last_reward_terms = {
            "base_reward": base_reward.detach().clone(),
            "proximity_reward": proximity_reward.detach().clone(),
            "regrasp_bonus": regrasp_bonus.detach().clone(),
            "z_acc_penalty": z_acc_penalty.detach().clone(),
            "proximity_gap": proximity_gap.detach().clone(),
            "direction_change_count": self.direction_change_count.float().detach().clone(),
            "z_improvement": z_improvement.detach().clone(),
            "avg_force": avg_force.detach().clone(),
            "regrasp_event": regrasp_event.float().detach().clone(),
            "success_candidate": success_candidate.float().detach().clone(),
            "success_steps": self._success_steps.float().detach().clone(),
            # ep_* keys describe how an episode ended. They are read by the training
            # table only on the step an env terminates, never averaged per step.
            "ep_done": done.float(),
            "ep_success": success.float(),
            "ep_fail": fail.float(),
            "ep_timeout": (timeout & ~fail & ~success).float(),
            "ep_length": self.episode_length_buf.float(),
            "ep_fail_rel_x": fail_rel_x.float(),
            "ep_fail_rel_y": fail_rel_y.float(),
            "ep_fail_fingertip": fail_fingertip.float(),
            "ep_fail_rel_z": fail_rel_z.float(),
            "ep_fail_ee_low": fail_ee_low.float(),
            "ep_fail_ee_high": fail_ee_high.float(),
            "ep_fail_regrasp_limit": limit_fail.float(),
        }

        return done, reward, timeout

    def _reset_idx(self, envs_idx: torch.Tensor):
        """Reset a subset of envs in-place; sim_step continues uninterrupted."""
        q = self.q_home.unsqueeze(0).expand(len(envs_idx), -1)
        self.franka.set_qpos(q, envs_idx=envs_idx, zero_velocity=True)
        self.franka.control_dofs_position(q, envs_idx=envs_idx)
        self._reset_task_frame(envs_idx)
        self._reset_cuboid_home_pose(envs_idx)

        self.target_center[envs_idx] = self.task_origin[envs_idx]
        self.target_quat[envs_idx] = self.task_quat[envs_idx]
        self.target_z[envs_idx] = self.HOME_TASK_Z
        self.target_z_vel[envs_idx] = 0.0
        self.target_z_acc[envs_idx] = 0.0
        self.prev_target_z_vel[envs_idx] = 0.0
        self.direction_change_count[envs_idx] = 0
        _n = len(envs_idx)
        _mag = 0.01 + torch.rand(_n, device=self.device) * 0.01  # uniform in [0.02, 0.04]
        if self.mix:
            _sign = torch.where(
                torch.rand(_n, device=self.device) < 0.5,
                torch.ones(_n, device=self.device),
                torch.full((_n,), -1.0, device=self.device),
            )
            _mag = _sign * _mag  # randomly flip sign per env
        elif self.solid_up:
            _mag = -_mag  # negative desired_rel_z → solid-up training regime
        self.desired_rel_z[envs_idx] = _mag
        self.episode_length_buf[envs_idx] = 0
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
        self._randomize_finger_gains(envs_idx)
        self._prev_gripper_avg[envs_idx] = self.gripper_pos_min.mean()
        self._post_pulse_delay_countdown[envs_idx] = 0
        self._post_pulse_hold_countdown[envs_idx] = 0
        self._steps_since_pulse[envs_idx] = self._pulse_lockout_steps + 1
        self._randomize_friction(envs_idx)

        # ---- initial velocity randomization (domain randomization) ----
        if self.randomize:
            _n = len(envs_idx)
            qvel_noise = (torch.rand(_n, 7, device=self.device) * 2.0 - 1.0) * 0.01
            qvel = torch.zeros(_n, 9, device=self.device)
            qvel[:, :7] = qvel_noise
            self.franka.set_dofs_velocity(qvel, envs_idx=envs_idx)

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

    def _randomize_finger_gains(self, envs_idx: torch.Tensor):
        """Sample one shared finger value per env and apply it with preserved signs."""
        n = len(envs_idx)
        value = self.FINGER_GAIN_RANDOM_MIN + torch.rand(n, device=self.device) * (
            self.FINGER_GAIN_RANDOM_MAX - self.FINGER_GAIN_RANDOM_MIN
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
        # Fixed straight line in world space; scalar speed retains its m/s scale.
        target_pos = self._task_target_position(target_z)
        # 2nd-order low-pass filter: H(s) = 5625 / (s² + 49.5 s + 5625) at 1000 Hz
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
        target_vel = self.task_axes[:, :, 2] * target_z_vel.unsqueeze(-1)

        # EE state
        ee_pos = self.ee_link.get_pos()  # (N, 3)
        ee_quat = self.ee_link.get_quat()  # (N, 4)

        # Cartesian error
        error_pos = target_pos - ee_pos  # (N, 3)
        axis = self.task_axes[:, :, 2]
        axial_error = (error_pos * axis).sum(dim=-1, keepdim=True) * axis
        # Stronger transverse feedback resists gravity-induced sideways drift;
        # the axial gain and filtered action dynamics retain their original values.
        position_feedback = self.pos_gain * axial_error + self.transverse_pos_gain * (error_pos - axial_error)
        rel_quat = _tc_quat_mul(self.target_quat, _tc_inv_quat(ee_quat))  # (N, 4)
        error_rotvec = _tc_quat_to_rotvec(rel_quat)  # (N, 3)

        # ee_velocity_cmd: (N, 6)
        ee_vel_cmd = torch.cat(
            [
                target_vel + position_feedback,  # (N, 3)
                self.rot_gain * error_rotvec,  # (N, 3)
            ],
            dim=-1,
        )

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
            0.0,
            self.target_period,
        )
        s = (local_t / self.target_period).clamp(0.0, 1.0)  # (N,)
        s2, s3 = s * s, s * s * s

        z0, zv0 = self._seg_start[:, 0], self._seg_start[:, 1]
        z1, zv1 = self._seg_end[:, 0], self._seg_end[:, 1]
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

    @staticmethod
    def _joint5_for_tilt(tilt_deg: float, joint_axis_z: float) -> float:
        """Invert Rodrigues' formula for rotating vertical about joint 5's axis."""
        if not math.isfinite(tilt_deg) or not 0.0 <= tilt_deg <= 45.0:
            raise ValueError("tilt_deg must be a finite angle between 0 and 45 degrees")
        # z dot R(axis, q) z = axis_z**2 + (1-axis_z**2) * cos(q).
        axis_z_sq = joint_axis_z**2
        if axis_z_sq >= 1.0 - 1e-8:
            raise ValueError("Joint 5 is parallel to vertical and cannot produce the requested tilt")
        cosine = (math.cos(math.radians(tilt_deg)) - axis_z_sq) / (1.0 - axis_z_sq)
        if not -1.0 <= cosine <= 1.0 + 1e-8:
            raise ValueError("Requested tilt is unreachable by rotating joint 5")
        return -math.acos(max(-1.0, min(1.0, cosine)))

    def _configure_home_pose(self):
        """Calculate home once from the model; reset reuses this fixed pose/frame."""
        from genesis.utils.geom import transform_by_quat

        self.franka.set_qpos(self.q_home.expand(self.num_envs, -1), zero_velocity=True)
        baseline_quat = self.ee_link.get_quat()[0].clone()
        # The MJCF joint5 is a hinge around link5 local +Z.
        joint_quat = self.franka.get_link("link5").get_quat()[0]
        joint_axis = transform_by_quat(torch.tensor([0.0, 0.0, 1.0], device=self.device), joint_quat)
        self.q_home[4] = self._joint5_for_tilt(self.tilt_deg, float(joint_axis[2]))
        self.franka.set_qpos(self.q_home.expand(self.num_envs, -1), zero_velocity=True)
        self._home_frame_quat = _tc_quat_mul(self.ee_link.get_quat()[0], _tc_inv_quat(baseline_quat))
        if self.tilt_deg == 0.0:
            self._home_frame_quat = torch.tensor([1.0, 0.0, 0.0, 0.0], device=self.device)
        self.cfg["home_arm_q"] = self.q_home[:7].tolist()

    def _reset_task_frame(self, envs_idx: torch.Tensor):
        """Cache the nominal FK frame before warmup; only reset selected rows."""
        from genesis.utils.geom import transform_by_quat

        if not hasattr(self, "task_origin"):
            self.task_origin = torch.zeros(self.num_envs, 3, device=self.device)
            self.task_quat = torch.zeros(self.num_envs, 4, device=self.device)
            self.task_axes = torch.zeros(self.num_envs, 3, 3, device=self.device)
        self.task_origin[envs_idx] = self.ee_link.get_pos()[envs_idx]
        self.task_quat[envs_idx] = self.ee_link.get_quat()[envs_idx]
        # Rotate the ORIGINAL world task frame by the home orientation change.
        # Unlike using raw hand axes, this gives exactly world XYZ at zero tilt.
        frame_quat = self._home_frame_quat.expand(len(envs_idx), -1)
        for column in range(3):
            local_axis = torch.eye(3, device=self.device)[column].expand(len(envs_idx), -1)
            self.task_axes[envs_idx, :, column] = transform_by_quat(local_axis, frame_quat)

    def _world_vector_to_task(self, vector: torch.Tensor) -> torch.Tensor:
        """Project world displacements/velocities into the fixed home frame."""
        return torch.einsum("nji,nj->ni", self.task_axes, vector)

    def _task_position_z(self, world_pos: torch.Tensor) -> torch.Tensor:
        return self.HOME_TASK_Z + self._world_vector_to_task(world_pos - self.task_origin)[:, 2]

    def _task_target_position(self, target_z: torch.Tensor) -> torch.Tensor:
        return self.target_center + self.task_axes[:, :, 2] * (target_z - self.HOME_TASK_Z).unsqueeze(-1)

    def _fingertip_pos(self, finger_link) -> torch.Tensor:
        """Compute fingertip world position (N, 3) from link pose."""
        pos = finger_link.get_pos()  # (N, 3)
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

        hand_pos = self.ee_link.get_pos()[envs_idx]  # (|idx|, 3)
        hand_quat = self.ee_link.get_quat()[envs_idx]  # (|idx|, 4)

        local_offset = torch.tensor([0.0, 0.0, 0.1029], device=self.device)
        # Keep the original grasp alignment relative to the hand. Its long axis
        # is world +Z at zero tilt and follows the rotated task +Z at other angles.
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
        kp_motors = torch.tensor([4500, 4500, 3500, 3500, 2000, 2000, 2000], dtype=torch.float32, device=self.device)
        # 0.35 x the nominal [450 450 350 350 200 200 200], tuned for real-arm-like lag
        kv_motors = torch.tensor(
            [157.5, 157.5, 122.5, 122.5, 70.0, 70.0, 70.0], dtype=torch.float32, device=self.device
        )
        f_lo = torch.tensor([-87, -87, -87, -87, -12, -12, -12], dtype=torch.float32, device=self.device)
        f_hi = torch.tensor([87, 87, 87, 87, 12, 12, 12], dtype=torch.float32, device=self.device)

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
# Canonical name supported by the existing training/evaluation module loaders.
FrankaEnvParallel = FrankaEnvParallelTilted

# Minimal smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-B", "--num_envs", type=int, default=16)
    parser.add_argument("--vis", action="store_true")
    parser.add_argument("--tilt-deg", type=float, default=FrankaEnvParallel.DEFAULT_TILT_DEG)
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--limit-regrasp", action="store_true")
    args = parser.parse_args()

    gs.init(backend=gs.gpu, precision="32", logging_level="warning")

    env = FrankaEnvParallel(
        num_envs=args.num_envs, vis=args.vis, limit_regrasp=args.limit_regrasp, tilt_deg=args.tilt_deg
    )
    obs_td = env.reset()
    print("obs shape:", obs_td["policy"].shape)
    print("obs_dim:", FrankaEnvParallel.OBS_DIM)

    for i in range(args.steps):
        actions = torch.zeros(args.num_envs, 3, device=gs.device)
        obs_td, rew_buf, reset_buf, extras = env.step(actions)

    print("Done. grasp-axis EE position mean:", obs_td["policy"][:, FrankaEnvParallel.OBS_EE_POS_Z].mean().item())

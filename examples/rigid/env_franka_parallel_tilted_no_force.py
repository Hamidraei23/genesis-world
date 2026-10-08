"""Franka regrasp with a configurable fixed grasp-axis tilt from 0 to 45 degrees,
without the two finger force observations.

Derived from env_franka_parallel_tilted: same control path and task frame, but the
observation vector drops left_force_mag and right_force_mag (OBS_DIM is 8 instead of
10) and the reward is its own -- a progress-based task reward plus style costs shaped
around one reciprocating stroke; see the reward constants on the class. Finger contact
force is still measured inside the reward -- it is privileged simulator state, not a
policy input -- so release, regrasp and firm-grasp detection do not need the
observations.

``tilt_deg`` is inclination from world vertical, toward the pictured joint-5
homing direction (approximately world +Y), not an absolute Euler pitch angle.
The joint-5 rotation is calculated from forward kinematics. Zero reproduces the
original joint home, object alignment, and world-coordinate task axes.

All Z names refer to the fixed tilted grasp axis. Position coordinates are
HOME_TASK_Z plus displacement along that axis from home. Observations, reward
formulas, success/failure distances and hold targets use this same frame.
The tilt is fixed for the run and is not an additional observation. Policies
trained here read 8 channels, so they are not interchangeable with the 10-channel
env_franka_parallel_tilted. Gravity stays world-down.
Transverse and orientation feedback hold the line and home quaternion; axial
position gain, action filter, reward weights and action scales are retained.

Train:
    python examples/rigid/train_franka_ppo.py --env env_franka_parallel_tilted_no_force --tilt-deg 20 -e franka-tilted20-nf
Preview:
    python examples/rigid/env_franka_parallel_tilted_no_force.py -B 1 --vis --tilt-deg 45
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


class FrankaEnvParallelTiltedNoForce:
    """
    Vectorised tilted Franka environment without finger force observations.

    Z is the fixed home grasp axis; it does not rotate with tracking errors.
    EE positions use HOME_TASK_Z + displacement from home along that axis.
    Cuboid relative X/Y/Z and EE velocities are projected into that same frame.

    Observations: flat tensor (N, OBS_DIM=8)
        [0]    ee_pos_z
        [1]    ee_vel_z
        [2]    target_z_vel
        [3]    target_z_acc
        [4]    cuboid_rel_z
        [5]    cuboid_rel_x
        [6]    cuboid_rel_y
        [7]    desired_rel_z

        Not observed: left_force_mag, right_force_mag. The reward still reads
        finger contact force from the simulator; the policy does not see it.

        Future Observations (To be added):
        [8]    last_pulse_gap_z_improve (LAST PULSE GAP z improvement)
        [9]    pulse_gap_duration (Duration of gap derived from finger pos)
        [10]   acc_drop_to_open_delay (Delay from the moment acceleration of EE goes below -10.0 to the moment end effector opens, in ms. Can be positive or negative)

    Actions (per env, shape (N, action_dim=3)):
        [0]   target_z_vel
        [1:3] gripper_pos (left, right finger)

    Reward: progress towards the commanded offset plus success / fail / time, and style
    costs (upward acceleration, jerk, motion during the wait, refused pulse requests) that
    the intended stroke does not pay. Described in full next to the reward constants.
    """

    DEFAULT_TILT_DEG = 30.0
    HOME_ARM_Q = (0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78)
    # Original environment's nominal FK hand height, retained as task origin value.
    HOME_TASK_Z = 0.854586497

    # Observation layout constants
    OBS_DIM = 8
    OBS_EE_POS_Z = 0
    OBS_EE_VEL_Z = 1
    OBS_TARGET_Z_VEL = 2
    OBS_TARGET_Z_ACC = 3
    OBS_CUBOID_REL_Z = 4
    OBS_CUBOID_REL_X = 5
    OBS_CUBOID_REL_Y = 6
    OBS_DESIRED_REL_Z = 7
    # Future Observations (To be added):
    # OBS_LAST_PULSE_GAP_Z_IMPROVE = 8
    # OBS_PULSE_GAP_DURATION       = 9
    # OBS_ACC_DROP_TO_OPEN_DELAY   = 10

    # Fixed observation normalization scales (divide raw obs by these)
    # Order: ee_pos_z, ee_vel_z, target_z_vel, target_z_acc,
    #        cuboid_rel_z, cuboid_rel_x, cuboid_rel_y, desired_rel_z
    #        (Future: last_pulse_gap_z_improve, pulse_gap_duration, acc_drop_to_open_delay)
    # Same scales as env_franka_parallel_tilted with the two 5.0 force entries removed.
    OBS_SCALE = [1.0, 0.6, 0.6, 15.0, 0.05, 0.05, 0.05, 0.05]

    # ------------------------------------------------------------------ #
    # Reward                                                              #
    # ------------------------------------------------------------------ #
    # One stroke of the intended motion: hold still for WAIT_DURATION, climb at no more
    # than ACC_UP_LIMIT, fire the pulse and reverse hard so the object slides up in the
    # open fingers, keep descending until they have closed again, brake gently to rest.
    #
    # Task terms pay for the outcome:
    #   progress_reward  PROGRESS_REWARD x the fraction of the starting gap closed. Banked
    #                    whenever the fingers hold the object, so a stroke is paid on the
    #                    regrasp; moving away is charged at the same rate. Closing the
    #                    whole gap is worth PROGRESS_REWARD however far the target is.
    #   regrasp_bonus    A learning aid on top of progress, meant to be switched off (weight
    #                    0) once the stroke is learned. Paid on a regrasp that leaves the
    #                    object closer to the target than at any earlier point of the
    #                    episode, and exponentially more for a bigger gain: 100 for 1 mm
    #                    of new ground, 1000 for 5 mm, at most REGRASP_BONUS_CAP. Only new
    #                    ground counts, so sliding the object away and back earns nothing
    #                    a second time.
    #                    The bonus is then multiplied by a factor set by the hand height
    #                    (ee_z) at which the pulse behind that regrasp fired: 0.5 at
    #                    0.65 m, rising in a straight line to 1.0 at 0.85 m, and flat
    #                    outside that range.
    #   success_reward   SUCCESS_REWARD once, on settling inside the success tolerance.
    #   fail_penalty     FAIL_PENALTY once, on any failure termination.
    #   time_penalty     TIME_PENALTY every step, so finishing beats loitering.
    #   wait_penalty     WAIT_PENALTY x (commanded speed / Z_VEL_MAX)^2, only in the wait
    #                    window: the first PULSE_START_MIN_STEPS of an episode, and from
    #                    STROKE_SETTLE_DURATION after each pulse until the lockout ends.
    #                    Holding still there is part of the task, so it is charged at
    #                    full weight. A whole episode of it must stay cheaper than
    #                    FAIL_PENALTY, or leaving the range becomes the way to stop paying.
    #                    With ENFORCE_WAIT_HOLD the env already holds the hand there, so
    #                    all this can still charge is the short braking ramp into the hold.
    # Style terms cost nothing along the intended stroke and are all scaled by
    # STYLE_SCALE. They are computed on the commanded motion, so they also charge the
    # policy's own exploration noise: measured on an idle policy over one 450-step
    # episode at scale 1, the acceleration + jerk terms cost about 30 at an action std
    # of 0.05, 210 at 0.1, 5600 at 0.3 and 7700 at 1.0 -- against roughly 1750 for a
    # perfect episode. So STYLE_SCALE is a curriculum knob: keep it small while the
    # policy is still noisy and finding the task, and raise it towards 1.0 (edit, then
    # --resume) once successes are common and the action std has come down:
    #   acc_up_penalty   commanded upward acceleration above ACC_UP_LIMIT; ACC_UP_PENALTY
    #                    per step at the full +Z_ACC_MAX. The reversal is downward: free.
    #                    With ENFORCE_ACC_UP_LIMIT the env caps the command at the limit,
    #                    so this term stays at zero.
    #   jerk_penalty     JERK_PENALTY x (change in commanded acceleration / Z_ACC_MAX)^2.
    #   blocked_pulse_penalty  BLOCKED_PULSE_PENALTY per pulse request the env refuses.
    PROGRESS_REWARD = 900.0
    PROGRESS_MIN = -0.5  # progress is clipped here, so one bad release costs at most half
    # Two (gain in metres, bonus) points the regrasp bonus passes through. From the small
    # point upwards it is the exponential through both (x10 every 4 mm with these values),
    # capped at the top; below the small point it falls in a straight line to zero.
    REGRASP_BONUS_SMALL = (0.001, 100.0)
    REGRASP_BONUS_LARGE = (0.005, 1000.0)
    REGRASP_BONUS_RATE = math.log(REGRASP_BONUS_LARGE[1] / REGRASP_BONUS_SMALL[1]) / (
        REGRASP_BONUS_LARGE[0] - REGRASP_BONUS_SMALL[0]
    )  # 1/m
    REGRASP_BONUS_CAP = 4000.0  # reached at about 7.4 mm of new ground in one regrasp
    # Multiplier on the regrasp bonus from the hand height (ee_z, metres) at which the pulse
    # fired: a straight line through the two (height, multiplier) points, flat outside them.
    REGRASP_BONUS_HEIGHT_LOW = (0.65, 0.5)
    REGRASP_BONUS_HEIGHT_HIGH = (0.85, 1.0)
    SUCCESS_REWARD = 1000.0
    FAIL_PENALTY = 2000.0  # keep above what a whole episode of wait_penalty can cost
    TIME_PENALTY = 0.5
    ACC_UP_LIMIT = 5.0  # m/s^2
    ACC_UP_PENALTY = 100.0
    JERK_PENALTY = 2.0
    WAIT_PENALTY = 5.0  # per step at full speed; not scaled by STYLE_SCALE
    BLOCKED_PULSE_PENALTY = 5.0
    STYLE_SCALE = 0.25 # stage 1; the weights above are sized for 1.0

    # Weights applied to each term in _compute_done_and_reward; also read by
    # reward_table.RewardTermTracker for the per-iteration table.
    REWARD_TERM_WEIGHTS = {
        "progress_reward": 1.0,
        "regrasp_bonus": 2.0,  # set to 0 to switch the bonus off
        "success_reward": 1.0,
        "fail_penalty": 1.0,
        "time_penalty": 1.0,
        "acc_up_penalty": STYLE_SCALE,
        "jerk_penalty": STYLE_SCALE,
        "wait_penalty": 1.0,
        "blocked_pulse_penalty": STYLE_SCALE,
    }

    # Action scaling constants
    Z_VEL_MAX = 0.45
    Z_ACC_MAX = 15.00  # m/s² — hard limit on target-velocity rate of change
    GRIPPER_CLOSED = 0.000251
    GRIPPER_OPEN = 0.0130  # per finger; gap 26.0 mm clears the 25 mm block by 1.0 mm
    # Release-to-regrasp detection
    FORCE_FREE_THRESHOLD = 0.15  # N: avg finger force below this -> fully released
    REGRASP_FORCE_THRESHOLD = 0.75  # N: avg finger force at/above this -> firm grasp
    REGRASP_TERMINATION_COUNT = 7  # fail on the 7th regrasp if not successful by then
    # Success: object within SUCCESS_GAP_TOLERANCE of the commanded offset, held firmly,
    # hand inside the Z band and slower than SUCCESS_EE_VEL_MAX, for SUCCESS_REQUIRED_STEPS.
    SUCCESS_GAP_TOLERANCE = 0.005  # m
    SUCCESS_EE_Z_MIN = 0.7
    SUCCESS_EE_Z_MAX = 0.86
    SUCCESS_EE_VEL_MAX = 0.05  # m/s: |ee_vel_z| must be below this to count as settled
    SUCCESS_REQUIRED_STEPS = 2
    # Fixed at 2 steps = 40 ms, with or without --randomize: the delay is not a
    # domain-randomised quantity, so every episode sees the same command-to-open lag.
    PULSE_DELAY_STEPS = 2  # target-period steps to wait before the open window begins
    # Read by the pulse sweep tooling (eval_test_franka_delays, test_gripper_pulse_plot),
    # which captures and pins these. They do not drive _sample_gripper_pulse_delays;
    # they are held at PULSE_DELAY_STEPS so a captured "native range" is the truth.
    PULSE_DELAY_RANDOM_MIN = 2  # 40 ms
    PULSE_DELAY_RANDOM_MAX = 2  # 40 ms
    # steps: PULSE_LENGTH - 1 forced-open steps, then 1 forced-close step, then back to
    # policy control. 7 puts the forced-open window at 6 steps = 120 ms.
    PULSE_LENGTH = 7
    # The policy is still commanding "open" through the delay (that command is what
    # triggered the pulse), so the object is loose for PULSE_DELAY + PULSE_LENGTH - 1 =
    # 8 steps = 160 ms, of which the last 120 ms are forced open whatever the policy asks.
    PULSE_LENGTH_RANDOM_MIN = 7
    PULSE_LENGTH_RANDOM_MAX = 7
    # Pulse availability, which is also what defines the wait. No pulse can fire in the
    # first PULSE_START_MIN_STEPS of an episode, nor until PULSE_LOCKOUT_DURATION after the
    # previous trigger. The lockout is the time a stroke is given to finish after its
    # trigger (reversal, descent until the fingers close, braking) plus the wait itself.
    STROKE_SETTLE_DURATION = 0.3  # s
    WAIT_DURATION = 1.0  # s of stillness between strokes
    PULSE_START_MIN_STEPS = 50  # the opening wait, in steps: WAIT_DURATION at target_dt 0.02
    PULSE_LOCKOUT_DURATION = STROKE_SETTLE_DURATION + WAIT_DURATION  # s since the trigger
    # Enforced stillness. With this on, the env itself holds the hand during the wait window
    # (the opening PULSE_START_MIN_STEPS, and from STROKE_SETTLE_DURATION after each trigger
    # until the lockout ends): the policy's velocity action is ignored there, the commanded
    # speed is brought to zero at no more than HOLD_BRAKE_ACC and then kept at zero. The
    # policy sees the result in its target_z_vel / target_z_acc observations. A robot
    # controller running this policy has to apply exactly the same rule. Set to False for
    # the old behaviour, where stillness is only encouraged by wait_penalty.
    ENFORCE_WAIT_HOLD = True
    HOLD_BRAKE_ACC = ACC_UP_LIMIT  # m/s^2; at the upward limit, so the braking is never penalised
    # Enforced acceleration limit. With this on, the env caps the upward commanded acceleration
    # (speeding up, or braking out of a descent) at ACC_UP_LIMIT, whatever the policy asks for;
    # downward acceleration keeps Z_ACC_MAX, so the reversal at the top of a stroke stays
    # sharp. acc_up_penalty can then never fire. A robot controller running this policy has
    # to apply the same asymmetric cap. Set to False for the old behaviour (penalty only).
    ENFORCE_ACC_UP_LIMIT = True
    FINGER_GAIN_RANDOM_MIN = 100.0
    FINGER_GAIN_RANDOM_MAX = 500.0
    # Friction domain randomisation: effective contact μ sampled each episode
    FRICTION_BASE = 0.75  # sliding friction in the MJCF files (dominant value)
    FRICTION_MIN = 0.2  # minimum desired effective contact friction
    FRICTION_RANDOM_MIN = 0.2  # replaces FRICTION_MIN when randomize is on (same range now)
    FRICTION_MAX = 0.5  # maximum desired effective contact friction

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
        # Set by run_policy --interactive: success, timeout and the regrasp limit no longer
        # end an episode (the geometric fails still do).
        self.interactive = False
        # External push on the cuboid, set by push_cuboid(). Genesis clears applied forces
        # after every scene.step, so the substep loop re-applies it while steps remain.
        self._push_force = None  # (N, 3) world-frame force
        self._push_sim_steps_left = 0
        self.solid_up = solid_up
        self.mix = mix
        self.randomize = randomize
        self.normalize = normalize
        self.extras: dict = {}
        self.cfg = {
            "environment": "env_franka_parallel_tilted_no_force",
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
            "fail_penalty": self.FAIL_PENALTY,
            "progress_reward": self.PROGRESS_REWARD,
            "time_penalty": self.TIME_PENALTY,
            "acc_up_limit": self.ACC_UP_LIMIT,
            "acc_up_penalty": self.ACC_UP_PENALTY,
            "jerk_penalty": self.JERK_PENALTY,
            "wait_penalty": self.WAIT_PENALTY,
            "blocked_pulse_penalty": self.BLOCKED_PULSE_PENALTY,
            "style_scale": self.STYLE_SCALE,
            "wait_duration": self.WAIT_DURATION,
            "enforce_wait_hold": self.ENFORCE_WAIT_HOLD,
            "enforce_acc_up_limit": self.ENFORCE_ACC_UP_LIMIT,
            "hold_brake_acc": self.HOLD_BRAKE_ACC,
            "stroke_settle_duration": self.STROKE_SETTLE_DURATION,
            "success_required_steps": self.SUCCESS_REQUIRED_STEPS,
            "pulse_delay": self.PULSE_DELAY_STEPS,
            "pulse_delay_random_min": self.PULSE_DELAY_RANDOM_MIN,
            "pulse_delay_random_max": self.PULSE_DELAY_RANDOM_MAX,
            "pulse_length": self.PULSE_LENGTH,
            "pulse_length_random_min": self.PULSE_LENGTH_RANDOM_MIN,
            "pulse_length_random_max": self.PULSE_LENGTH_RANDOM_MAX,
            "pulse_start_min_steps": self.PULSE_START_MIN_STEPS,
            "pulse_lockout_duration": self.PULSE_LOCKOUT_DURATION,
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
            # Regrasps this episode that ended further from the commanded offset.
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
            # Pulses actually fired this episode. Zero at the terminal step means the task
            # was never attempted; see no_pulse_penalty.
            self._pulse_count = torch.zeros(N, dtype=torch.long, device=self.device)
            # Previous RAW requested finger command, and whether the step just executed
            # carried a trigger request that a block refused. Written by step(), read by
            # the reward as blocked_pulse_penalty.
            self._prev_gripper_req = torch.full((N,), self.gripper_pos_min.mean().item(), device=self.device)
            self._blocked_pulse_now = torch.zeros(N, dtype=torch.bool, device=self.device)
            self._gripper_pulse_delays = torch.full((N,), self.PULSE_DELAY_STEPS, dtype=torch.long, device=self.device)
            self._gripper_pulse_lengths = torch.full((N,), self.PULSE_LENGTH, dtype=torch.long, device=self.device)
            self._prev_gripper_avg = torch.full((N,), self.gripper_pos_min.mean().item(), device=self.device)
            # Steps since the last pulse trigger. Gates the lockout; starts "expired" so the
            # first pulse can fire at once.
            self._pulse_lockout_steps = max(1, int(round(self.PULSE_LOCKOUT_DURATION / self.target_period)))
            self._steps_since_pulse = torch.full(
                (N,), self._pulse_lockout_steps + 1, dtype=torch.long, device=self.device
            )
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
        self._reset_reward_state(envs_idx)
        self.direction_change_count[envs_idx] = 0
        _n = len(envs_idx)
        _mag = 0.01 + torch.rand(_n, device=self.device) * 0.025  # uniform in [0.01, 0.02]
        # _mag = 0.04
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
        self._pulse_count[envs_idx] = 0
        self._prev_gripper_req[envs_idx] = self.gripper_pos_min.mean()
        self._blocked_pulse_now[envs_idx] = False
        self._sample_gripper_pulse_delays(envs_idx)
        self._sample_gripper_pulse_lengths(envs_idx)
        self._randomize_finger_gains(envs_idx)
        self._prev_gripper_avg[envs_idx] = self.gripper_pos_min.mean()
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
        # Limit acceleration: downward |Δv| ≤ Z_ACC_MAX * target_period; upward Δv ≤ ACC_UP_LIMIT *
        # target_period when ENFORCE_ACC_UP_LIMIT is on, else the same Z_ACC_MAX
        max_dv_down = self.Z_ACC_MAX * self.target_period
        max_dv_up = (self.ACC_UP_LIMIT if self.ENFORCE_ACC_UP_LIMIT else self.Z_ACC_MAX) * self.target_period
        new_z_vel = new_z_vel.clamp(self.target_z_vel - max_dv_down, self.target_z_vel + max_dv_up)
        if self.ENFORCE_WAIT_HOLD:
            # The same window wait_penalty uses, read at the moment the action is applied
            # (before this step's trigger logic touches the clock). Inside it the policy's
            # velocity action is dropped and the command steps towards zero.
            settle_steps = int(round(self.STROKE_SETTLE_DURATION / self.target_period))
            in_hold = (self.episode_length_buf < self.PULSE_START_MIN_STEPS) | (
                (self._steps_since_pulse >= settle_steps) & (self._steps_since_pulse < self._pulse_lockout_steps)
            )  # (N,)
            hold_dv = self.HOLD_BRAKE_ACC * self.target_period
            hold_z_vel = self.target_z_vel - self.target_z_vel.clamp(-hold_dv, hold_dv)
            new_z_vel = torch.where(in_hold, hold_z_vel, new_z_vel)
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
        self._pulse_fired = _rising
        self._pulse_count += _rising.long()
        # The same upward crossing, but measured on the raw request and refused by a block.
        # Using _prev_gripper_req (raw) rather than _prev_gripper_avg (effective, clamped
        # shut while blocked) charges one distinct request once instead of every step the
        # policy keeps asking. Reads _gripper_pulse_steps before the update below, exactly
        # as _rising does.
        self._blocked_pulse_now = (
            (self._prev_gripper_req < _gp_mid)
            & (_gp_avg >= _gp_mid)
            & (self._gripper_pulse_steps == 0)
            & _pulse_blocked
        )  # (N,)
        self._prev_gripper_req = _gp_avg
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
        self.prev_target_z_acc = self.target_z_acc.clone()
        self.target_z = new_z
        self.target_z_vel = new_z_vel
        self.target_z_acc = new_z_acc

        # Control gripper (same pos for all substeps within this target period)
        self.franka.control_dofs_position(gripper_pos, dofs_idx_local=self.fingers_dof)

        for local_step in range(self.target_update_every):
            t = (self.sim_step + local_step) * self.dt
            tz, tz_vel = self._sample_target_z(t)  # (N,), (N,)
            self._control_once(tz, tz_vel)
            if self._push_sim_steps_left > 0:
                self.scene.sim.rigid_solver.apply_links_external_force(
                    self._push_force, links_idx=[self.cuboid.base_link_idx], ref="link_com"
                )
                self._push_sim_steps_left -= 1
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

        # No contact force is read here: the policy does not observe it. The reward
        # still samples it in _compute_done_and_reward.
        finger_mid = (left_ft + right_ft) / 2.0  # (N, 3)
        cuboid_rel = self._world_vector_to_task(cuboid_pos - finger_mid)
        ee_task_z = self._task_position_z(ee_pos)
        ee_task_vel = self._world_vector_to_task(ee_vel)
        _noise_range = 0.001 if self.randomize else 0.0
        _N = self.num_envs

        def _unoise(shape):
            return (torch.rand(shape, device=self.device) * 2.0 - 1.0) * _noise_range

        self.obs_buf = torch.cat(
            [
                ee_task_z.unsqueeze(-1) + _unoise((_N, 1)),  # [0]    ee_pos_z
                ee_task_vel[:, 2:3] + _unoise((_N, 1)),  # [1]    ee_vel_z
                self.target_z_vel.unsqueeze(-1),  # [2]    target_z_vel
                self.target_z_acc.unsqueeze(-1),  # [3]    target_z_acc
                cuboid_rel[:, 2:3] + _unoise((_N, 1)),  # [4]    cuboid_rel_z
                cuboid_rel[:, 0:1] + _unoise((_N, 1)),  # [5]    cuboid_rel_x
                cuboid_rel[:, 1:2] + _unoise((_N, 1)),  # [6]    cuboid_rel_y
                self.desired_rel_z.unsqueeze(-1),  # [7]    desired_rel_z
                # TODO: Future observations to be appended here:
                # [8]  last_pulse_gap_z_improve: LAST PULSE GAP z improvement
                # [9]  pulse_gap_duration: duration of gap derived from finger pos
                # [10] acc_drop_to_open_delay: delay (ms) from EE acceleration going below -10.0 to EE open (can be +/-)
            ],
            dim=-1,
        )  # (N, 8)

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

    def _reset_reward_state(self, envs_idx: torch.Tensor):
        """Clear the per-episode history the reward reads."""
        if not hasattr(self, "_banked_progress"):
            self.prev_target_z_acc = torch.zeros(self.num_envs, device=self.device)
            self._pulse_fired = torch.zeros(self.num_envs, dtype=torch.bool, device=self.device)
            self._banked_progress = torch.zeros(self.num_envs, device=self.device)
            self._best_gap = torch.zeros(self.num_envs, device=self.device)
            self._pulse_ee_z = torch.zeros(self.num_envs, device=self.device)
        self.prev_target_z_acc[envs_idx] = 0.0
        self._pulse_fired[envs_idx] = False
        self._banked_progress[envs_idx] = 0.0
        self._best_gap[envs_idx] = 0.0  # set from the real gap on the episode's first step
        self._pulse_ee_z[envs_idx] = 0.0  # hand height at the last pulse; 0 until one fires

    def _regrasp_height_factor(self, pulse_ee_z: torch.Tensor) -> torch.Tensor:
        """Regrasp bonus multiplier for the hand height at which the pulse fired."""
        z_low, factor_low = self.REGRASP_BONUS_HEIGHT_LOW
        z_high, factor_high = self.REGRASP_BONUS_HEIGHT_HIGH
        frac = ((pulse_ee_z - z_low) / (z_high - z_low)).clamp(0.0, 1.0)
        return factor_low + (factor_high - factor_low) * frac

    def _compute_done_and_reward(self) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Termination, then the reward described next to the reward constants.

        Task terms: progress_reward, regrasp_bonus, success_reward, fail_penalty, time_penalty,
        wait_penalty.
        Style terms (x STYLE_SCALE): acc_up_penalty, jerk_penalty, blocked_pulse_penalty.
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
        if self.interactive:
            timeout = torch.zeros_like(timeout)

        # Track consecutive steps in firm grasp (reset when grasp is lost)
        firm_grasp_now = avg_force >= self.REGRASP_FORCE_THRESHOLD
        self._firm_grasp_steps = torch.where(
            firm_grasp_now,
            self._firm_grasp_steps + 1,
            torch.zeros_like(self._firm_grasp_steps),
        )

        success_candidate = (
            (~timeout)
            & ((cuboid_rel_z - self.desired_rel_z).abs() <= self.SUCCESS_GAP_TOLERANCE)
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

        # Grasp-axis error against the commanded offset, before the release that led to
        # this regrasp and after it. Computed here rather than with the regrasp bonus
        # below because the bad-regrasp termination needs it while fail is still open.
        z_err_before = (self._release_cuboid_rel_z - self.desired_rel_z).abs()  # (N,)
        z_err_after = (cuboid_rel_z - self.desired_rel_z).abs()  # (N,)
        z_improvement = z_err_before - z_err_after  # (N,) positive = closer

        # Optional: terminate episode after too many regrasps
        # Always terminate as failure on the REGRASP_TERMINATION_COUNT-th regrasp
        limit_fail = regrasp_event & (self._regrasp_count + 1 >= self.REGRASP_TERMINATION_COUNT)
        if self.interactive:
            limit_fail = torch.zeros_like(limit_fail)
        fail = fail | limit_fail
        success_candidate = success_candidate & ~limit_fail

        success_candidate = success_candidate & (~fail)
        self._success_steps = torch.where(
            success_candidate,
            self._success_steps + 1,
            torch.zeros_like(self._success_steps),
        )
        success = self._success_steps >= self.SUCCESS_REQUIRED_STEPS

        done = timeout | fail
        if not self.interactive:
            done = done | success
        self._regrasp_count += regrasp_event.long()

        # ================================ reward ================================
        # ---- task ----
        # Progress is 0 where the object started and 1 on target. It is banked only while
        # the fingers hold the object, so the swing of a free object mid-pulse is never
        # paid; the regrasp pays the stroke's net result in one go.
        gap = (cuboid_rel_z - self.desired_rel_z).abs()  # (N,) metres
        start_gap = self.desired_rel_z.abs().clamp(min=1e-4)  # the object starts at rel_z = 0
        progress = (1.0 - gap / start_gap).clamp(min=self.PROGRESS_MIN)  # (N,)
        first_step = self.episode_length_buf == 1
        self._banked_progress = torch.where(first_step, progress, self._banked_progress)
        progress_reward = self.PROGRESS_REWARD * (progress - self._banked_progress) * firm_grasp_now.float()
        self._banked_progress = torch.where(firm_grasp_now, progress, self._banked_progress)

        # Regrasp bonus: only for ground the object had not reached before in this episode,
        # measured at the regrasp. Exponential in the gain from the small anchor point up,
        # so a larger gain is worth disproportionately more.
        self._best_gap = torch.where(first_step, gap, self._best_gap)
        new_ground = (self._best_gap - gap).clamp(min=0.0) * regrasp_event.float()  # (N,) metres
        gain_ref, bonus_ref = self.REGRASP_BONUS_SMALL
        regrasp_bonus = torch.where(
            new_ground >= gain_ref,
            bonus_ref * torch.exp(self.REGRASP_BONUS_RATE * (new_ground - gain_ref)),
            bonus_ref * new_ground / gain_ref,
        ).clamp(max=self.REGRASP_BONUS_CAP)
        # Remember the hand height on the step a pulse is accepted; the regrasp that follows
        # is paid in proportion to it.
        self._pulse_ee_z = torch.where(self._pulse_fired, ee_z, self._pulse_ee_z)
        regrasp_bonus = regrasp_bonus * self._regrasp_height_factor(self._pulse_ee_z)
        self._best_gap = torch.where(regrasp_event, torch.minimum(self._best_gap, gap), self._best_gap)

        success_reward = self.SUCCESS_REWARD * success.float()
        fail_penalty = -self.FAIL_PENALTY * fail.float()
        time_penalty = torch.full_like(gap, -self.TIME_PENALTY)

        # ---- style ----
        # Upward acceleration beyond the limit. Braking out of the descent is upward too,
        # so the same limit keeps the stop gentle; the reversal is downward and is free.
        acc = self.target_z_acc  # (N,) commanded, m/s^2
        acc_up_excess = (acc - self.ACC_UP_LIMIT).clamp(min=0.0) / (self.Z_ACC_MAX - self.ACC_UP_LIMIT)
        acc_up_penalty = -self.ACC_UP_PENALTY * acc_up_excess.square()

        # Change in commanded acceleration from one step to the next.
        jerk = (acc - self.prev_target_z_acc) / self.Z_ACC_MAX
        jerk_penalty = -self.JERK_PENALTY * jerk.square()

        # Commanded motion inside the wait window. Reward runs after episode_length_buf
        # advances and before the trigger clock does, so both are stepped back to the
        # values that gated the action just executed.
        settle_steps = int(round(self.STROKE_SETTLE_DURATION / self.target_period))
        in_wait = (self.episode_length_buf - 1 < self.PULSE_START_MIN_STEPS) | (
            (self._steps_since_pulse >= settle_steps) & (self._steps_since_pulse < self._pulse_lockout_steps)
        )  # (N,)
        wait_penalty = -self.WAIT_PENALTY * in_wait.float() * (self.target_z_vel / self.Z_VEL_MAX).square()

        # A pulse requested while the env is refusing them; see the raw-edge detection in step().
        blocked_pulse_penalty = -self.BLOCKED_PULSE_PENALTY * self._blocked_pulse_now.float()

        # Advance the trigger clock, capped one past the lockout so the counter cannot
        # grow without bound.
        self._steps_since_pulse = (self._steps_since_pulse + 1).clamp(max=self._pulse_lockout_steps + 1)

        # ---- diagnostics (not rewarded) ----
        _sign_flip = (torch.sign(self.prev_target_z_vel) * torch.sign(self.target_z_vel)) < 0
        self.direction_change_count = self.direction_change_count + _sign_flip.long()
        _no_pulse = done & (self._pulse_count == 0)  # episode ended without ever firing a pulse

        terms = {
            "progress_reward": progress_reward,
            "regrasp_bonus": regrasp_bonus,
            "success_reward": success_reward,
            "fail_penalty": fail_penalty,
            "time_penalty": time_penalty,
            "acc_up_penalty": acc_up_penalty,
            "jerk_penalty": jerk_penalty,
            "wait_penalty": wait_penalty,
            "blocked_pulse_penalty": blocked_pulse_penalty,
        }
        reward = sum(self.REWARD_TERM_WEIGHTS[name] * value for name, value in terms.items())

        self.last_reward_terms = {
            **{name: value.detach().clone() for name, value in terms.items()},
            "proximity_gap": gap.detach().clone(),
            "progress": progress.detach().clone(),
            "in_wait": in_wait.float(),
            "direction_change_count": self.direction_change_count.float().detach().clone(),
            "z_improvement": z_improvement.detach().clone(),
            "avg_force": avg_force.detach().clone(),
            "regrasp_event": regrasp_event.float().detach().clone(),
            "pulse_ee_z": self._pulse_ee_z.detach().clone(),
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
            "ep_no_pulse": _no_pulse.float(),
            "ep_pulse_count": self._pulse_count.float(),
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
        self._reset_reward_state(envs_idx)
        self.direction_change_count[envs_idx] = 0
        _n = len(envs_idx)
        _mag = 0.01 + torch.rand(_n, device=self.device) * 0.025  # uniform in [0.01, 0.02]
        # _mag = 0.04
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
        self._pulse_count[envs_idx] = 0
        self._prev_gripper_req[envs_idx] = self.gripper_pos_min.mean()
        self._blocked_pulse_now[envs_idx] = False
        self._sample_gripper_pulse_delays(envs_idx)
        self._sample_gripper_pulse_lengths(envs_idx)
        self._randomize_finger_gains(envs_idx)
        self._prev_gripper_avg[envs_idx] = self.gripper_pos_min.mean()
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

    def push_cuboid(self, force: float, duration: float):
        """Push the cuboid along the grasp axis in every env: `force` N (+ = up the axis,
        - = down) at its COM for `duration` s of sim time, starting at the next step()."""
        self._push_force = self.task_axes[:, :, 2] * force  # (N, 3)
        self._push_sim_steps_left = max(1, int(round(duration / self.dt)))

    def _randomize_friction(self, envs_idx: torch.Tensor):
        """
        Sample a fresh friction ratio for each env in envs_idx and apply it
        to all franka + cuboid geoms.

        Effective contact friction = FRICTION_BASE * ratio, sampled uniformly so that the
        result lies in [FRICTION_MIN, FRICTION_MAX], or [FRICTION_RANDOM_MIN, FRICTION_MAX]
        with randomize.
        """
        n = len(envs_idx)
        friction_min = self.FRICTION_RANDOM_MIN if self.randomize else self.FRICTION_MIN
        ratio_min = friction_min / self.FRICTION_BASE  # 0.2 / 0.75 = 0.267, randomize or not
        ratio_max = self.FRICTION_MAX / self.FRICTION_BASE  # 0.5 / 0.75 = 0.667
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
        """Set per-env pulse delays. Fixed at PULSE_DELAY_STEPS, randomize or not."""
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
FrankaEnvParallel = FrankaEnvParallelTiltedNoForce

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

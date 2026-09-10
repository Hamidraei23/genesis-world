"""Torque-aware Z regrasping with the original 14 observations and three actions.

The scene, action scaling, pulse generator, randomization and observations come
from env_franka_parallel. This variant replaces the reward and Cartesian servo.
See README_franka_regrasp.md for equations, limits and training commands.
"""

import math

import torch

try:
    from .env_franka_parallel import (
        FrankaEnvParallel as _OriginalEnv,
        _tc_inv_quat,
        _tc_quat_mul,
        _tc_quat_to_rotvec,
    )
except ImportError:
    from env_franka_parallel import (
        FrankaEnvParallel as _OriginalEnv,
        _tc_inv_quat,
        _tc_quat_mul,
        _tc_quat_to_rotvec,
    )


def motion_costs(acceleration, torque, torque_scale, acceleration_limit=15.0):
    """Dimensionless per-sample costs; +Z is opposite the scene's gravity.

    Penalize actual actuator torque, not net generalized force (which also
    contains other forces). Squaring before averaging preserves torque peaks.
    Squared hinges have continuous first derivatives at their boundaries.

    There is no band below the limit any more: the servo now commands right up
    to it, so anything measured above it is tracking error or contact, not a
    choice the policy made.
    """
    magnitude = acceleration.norm(dim=-1)
    return {
        "torque": (torque / torque_scale).square().mean(dim=-1),
        "acceleration": (magnitude / acceleration_limit).square(),
        "acceleration_excess": ((magnitude - acceleration_limit).clamp(min=0.0) / 3.0).square(),
        # A preference, not a proxy for full robot inverse dynamics. Downward
        # acceleration still pays the symmetric acceleration and torque costs.
        "against_gravity": (acceleration[..., 2].clamp(min=0.0) / acceleration_limit).square(),
    }


class FrankaEnvParallel(_OriginalEnv):
    """Drop-in PPO environment; existing MLP checkpoints keep compatible shapes."""

    REWARD_VERSION = "regrasp-v3.7"
    HANDLES_ZERO_HOLD = True
    REWARD_GAMMA = 0.99  # Must equal PPO gamma for potential-based shaping.
    # The servo now commands right up to the measured limit. The hard direction
    # needs the hand to outrun gravity, and every m/s² below 15 comes straight
    # off the achievable slip: 12 gives 3.9 mm per pulse, 15 gives 16.6 mm.
    CARTESIAN_ACC_MAX = 15.0

    # Longer open window. Slip grows with its square, so 60 ms to 80 ms is worth
    # more than the acceleration increase. Beyond 80 ms the +-0.6 m/s action
    # range runs out before the acceleration does, so there is nothing further
    # to gain here without changing the action scale.
    PULSE_LENGTH = 5            # open steps = length - 1, so 4 steps of 20 ms
    PULSE_LENGTH_RANDOM_MIN = 4
    PULSE_LENGTH_RANDOM_MAX = 7
    # The policy reaches 9 consecutive qualifying steps and needs 10, so it has
    # never once seen the success payout and cannot learn that holding pays.
    # Halved to break that deadlock; raise it again once successes are routine.
    SUCCESS_HOLD_SECONDS = 0.10
    # Judged per axis. Z is what the policy commands. Lateral velocity is servo
    # tracking noise sitting at 0.02 m/s on its own, so a shared 3-D budget of
    # 0.02 was spending most of itself on motion the policy never asked for.
    SUCCESS_EE_VZ_MAX = 0.02
    SUCCESS_EE_LATERAL_VEL_MAX = 0.05
    GRASP_CONFIRM_SECONDS = 0.06

    # Two-scale grasp quality. The coarse width guides the approach; the fine
    # width matches the success tolerance so the potential is not flat exactly
    # where precision decides the outcome.
    ERROR_SCALE = 0.025
    ERROR_SCALE_FINE = 0.005
    FINE_QUALITY_WEIGHT = 1.0
    # Kept small on purpose. Potential shaping leaks weight * (1 - gamma) * phi
    # every step, which at a 450-step horizon is 4.5x the useful telescoped
    # signal, and it charges most while the object sits closest to the target.
    # At weight 20 that leak was -46 per episode, the largest term in the reward,
    # paying the policy to keep moving rather than settle.
    PROGRESS_WEIGHT = 4.0

    # Dense credit for dwelling in the goal state, ratcheted on the longest run
    # reached this episode. Without it the hold is a cliff: four qualifying steps
    # in a row pay nothing and five pay a hundred, so nothing pulls a policy that
    # tops out at four across the gap.
    SETTLE_DWELL_REWARD = 4.0

    # Terminal accounting, in proportions taken from the reward that trained.
    # A success is the unit; failure stays above the worst case of running the
    # clock out, so ending an episode early never beats surviving it.
    SUCCESS_BONUS = 100.0
    # The height gate is gone from the success test. Instead the payout scales
    # with how close the hand finished to its home pose, so a sloppy posture
    # costs reward rather than costing the success outright. The floor keeps a
    # badly posed success clearly worth having.
    SUCCESS_POSE_SCALE = 0.06
    SUCCESS_POSE_FLOOR = 0.5
    FAIL_PENALTY = 25.0
    TIMEOUT_PENALTY = 0.0
    TIME_COST = 1.5  # per second; 13.5 over a full episode carries the urgency

    # The one achievement bonus. Pays for the error a single release/regrasp
    # cycle actually removed, never for proximity the policy did not produce.
    # Superlinear, so one decisive slip beats a chain of nudges. Backsliding is
    # charged at a multiple of the same curve, so releasing high to regrasp low
    # cannot be farmed. Eligibility is capped per episode.
    REGRASP_BONUS_WEIGHT = 40.0
    REGRASP_REFERENCE_SLIP = 0.015  # what one good pulse can deliver at 15 m/s²
    REGRASP_BACKSLIDE_MULTIPLIER = 2.0
    REGRASP_BONUS_MAX_EVENTS = 4

    # The post-regrasp settle window only arms once the object is close enough
    # that settling is the right move. Arming it after every regrasp taxes the
    # velocity build-up that the next pulse in a chain requires.
    HOLD_ARM_TOLERANCE = 0.010

    # Hold the hand near its home height. Free inside a small band so a pulse
    # can move, quadratic outside it, and steep well before the hard bounds at
    # 0.6 and 0.96 m that nothing else pulls the arm back from.
    EE_Z_HOME = 0.75
    EE_Z_FREE_BAND = 0.03
    EE_Z_MARGIN = 0.08
    WORKSPACE_WEIGHT = 20.0

    # Measured peak Cartesian speed within the policy step. Slow motion is free;
    # the cost is quadratic in the excess, so the maneuver is pushed toward the
    # slowest command that still produces the slip.
    SPEED_FREE = 0.20
    SPEED_MARGIN = 0.40
    SPEED_WEIGHT = 1.0

    # Effort weights. Upward acceleration is charged well above the symmetric
    # acceleration cost: it fights gravity and draws more joint torque.
    TORQUE_WEIGHT = 5.0
    JERK_WEIGHT = 0.015
    GRAVITY_WEIGHT = 2.0
    CENTERING_WEIGHT = 0.5
    VEL_CMD_WEIGHT = 0.2
    # Ceiling on the summed efficiency costs per step, expressed as a fraction
    # of one success spread over a full-length episode. When the sum would
    # exceed it every term is scaled down together, so no combination of effort
    # penalties can ever outweigh the reward for doing the task.
    EFFORT_BUDGET_FRACTION = 0.05

    # Exploration bonus for a completed release/regrasp cycle, annealed away.
    # Without it, holding still dominates every attempt the policy can make
    # before it is competent.
    ATTEMPT_BONUS = 8.0
    ATTEMPT_BONUS_MAX_EVENTS = 4

    # Soft bound on how far the object may slide before the hard cutoff at
    # 0.15 m, which previously arrived with no warning in the reward.
    REL_Z_FREE = 0.06
    REL_Z_MARGIN = 0.09
    SLIP_BOUND_WEIGHT = 20.0

    # A single 1 kHz sample above the limit is usually the contact impulse of
    # the gripper closing, which the policy cannot avoid. Only a sustained
    # excursion or a high step RMS is a real servo runaway worth terminating.
    ACC_VIOLATION_FAIL_STEPS = 5
    ACC_EXCESS_WEIGHT = 30.0
    SUCCESS_ACC_RMS_MAX = 4.0
    # Termination needs headroom above what the servo is allowed to command,
    # or a correctly executed pulse at the limit would terminate itself.
    ACC_FAIL_LIMIT = 18.0

    # Effort costs ramp in from EFFORT_ANNEAL_FLOOR so the task is learned
    # before it is optimized. The attempt bonus fades on the same schedule.
    EFFORT_ANNEAL_START_ITER = 100
    EFFORT_ANNEAL_END_ITER = 500
    EFFORT_ANNEAL_FLOOR = 0.1
    # Default to the fully annealed weights, which is what replay and
    # evaluation want; the trainer supplies a live iteration source.
    _reward_iteration = EFFORT_ANNEAL_END_ITER
    _reward_iteration_source = None

    # Terminal conditions, reported separately so a run says why it is dying.
    FAIL_CAUSES = ("lateral", "slip_out", "gripper_shut", "ee_low", "ee_high",
                   "acceleration", "regrasp_limit")

    EP_LOG_TERMS = (
        "total", "base", "progress", "regrasp", "dwell", "attempt", "torque", "acceleration",
        "gravity", "jerk", "speed", "vel_cmd", "hold", "centering", "workspace", "grip",
    )
    REWARD_PLOT_KEYS = (
        "base_reward", "progress_reward", "regrasp_bonus", "dwell_reward", "attempt_bonus",
        "torque_penalty", "z_acc_penalty", "gravity_penalty", "jerk_penalty",
        "speed_penalty", "post_pulse_hold_penalty", "workspace_penalty", "grip_penalty",
    )

    def __init__(self, num_envs=1, **kwargs):
        dt = kwargs.get("dt", 0.001)
        target_dt = kwargs.get("target_dt", 0.02)
        if dt <= 0 or target_dt < dt or not math.isclose(target_dt / dt, round(target_dt / dt)):
            raise ValueError("target_dt must be a positive integer multiple of dt")
        super().__init__(num_envs=num_envs, **kwargs)
        self.cfg.update(
            reward_version=self.REWARD_VERSION,
            effort_step_cap=self._effort_cap(),
            regrasp_bonus_weight=self.REGRASP_BONUS_WEIGHT,
            regrasp_reference_slip=self.REGRASP_REFERENCE_SLIP,
            ee_z_home=self.EE_Z_HOME,
            reward_gamma=self.REWARD_GAMMA,
            measured_acceleration_limit=self.Z_ACC_MAX,
            cartesian_acceleration_limit=self.CARTESIAN_ACC_MAX,
            acceleration_fail_limit=self.ACC_FAIL_LIMIT,
            pulse_open_steps=self.PULSE_LENGTH - 1,
            success_hold_seconds=self.SUCCESS_HOLD_SECONDS,
            success_ee_vz_max=self.SUCCESS_EE_VZ_MAX,
            success_ee_lateral_vel_max=self.SUCCESS_EE_LATERAL_VEL_MAX,
            success_pose_scale=self.SUCCESS_POSE_SCALE,
            success_pose_floor=self.SUCCESS_POSE_FLOOR,
            success_required_steps=math.ceil(self.SUCCESS_HOLD_SECONDS / self.target_period),
            grasp_confirm_steps=math.ceil(self.GRASP_CONFIRM_SECONDS / self.target_period),
            ee_hold_z_target="height_at_confirmed_regrasp",
            ee_hold_required_steps=math.ceil(self.SUCCESS_HOLD_SECONDS / self.target_period),
            error_scale_coarse=self.ERROR_SCALE,
            error_scale_fine=self.ERROR_SCALE_FINE,
            attempt_bonus=self.ATTEMPT_BONUS,
            hold_arm_tolerance=self.HOLD_ARM_TOLERANCE,
            workspace_weight=self.WORKSPACE_WEIGHT,
            speed_free=self.SPEED_FREE,
            attempt_bonus_max_events=self.ATTEMPT_BONUS_MAX_EVENTS,
            acceleration_fail_steps=self.ACC_VIOLATION_FAIL_STEPS,
            success_acceleration_rms_max=self.SUCCESS_ACC_RMS_MAX,
            effort_anneal_iters=(self.EFFORT_ANNEAL_START_ITER, self.EFFORT_ANNEAL_END_ITER),
            effort_anneal_floor=self.EFFORT_ANNEAL_FLOOR,
        )

    # ------------------------------------------------------------------ #
    # Reward annealing                                                    #
    # ------------------------------------------------------------------ #

    def set_reward_iteration_source(self, source):
        """Read the PPO iteration live, so annealing survives resume and phases."""
        self._reward_iteration_source = source

    def set_reward_iteration(self, iteration):
        """Pin the annealing iteration; ignored while a live source is set."""
        self._reward_iteration = int(iteration)

    def _effort_cap(self):
        """Per-step ceiling on the efficiency costs: one success spread thin.

        Bounds their episode total at EFFORT_BUDGET_FRACTION of a success, so no
        combination of torque, jerk, speed or posture costs can outweigh the
        reward for completing the task.
        """
        return self.EFFORT_BUDGET_FRACTION * self.SUCCESS_BONUS / self.max_episode_length

    def _anneal_progress(self):
        iteration = (
            self._reward_iteration_source() if self._reward_iteration_source is not None
            else self._reward_iteration
        )
        span = max(1, self.EFFORT_ANNEAL_END_ITER - self.EFFORT_ANNEAL_START_ITER)
        return min(1.0, max(0.0, (iteration - self.EFFORT_ANNEAL_START_ITER) / span))

    def _relative_position(self):
        midpoint = 0.5 * (self._fingertip_pos(self.left_finger) + self._fingertip_pos(self.right_finger))
        return self.cuboid.get_pos() - midpoint

    def _quality(self, error):
        coarse = torch.exp(-0.5 * (error / self.ERROR_SCALE).square())
        fine = torch.exp(-0.5 * (error / self.ERROR_SCALE_FINE).square())
        return (coarse + self.FINE_QUALITY_WEIGHT * fine) / (1.0 + self.FINE_QUALITY_WEIGHT)

    def _reset_reward_state(self, idx):
        if not hasattr(self, "_previous_potential"):
            self._previous_potential = torch.zeros(self.num_envs, device=self.device)
            self._previous_command_acc = torch.zeros_like(self._previous_potential)
            self._servo_linear_velocity = torch.zeros(self.num_envs, 3, device=self.device)
            self._hold_reference_z = torch.zeros_like(self._previous_potential)
            self._hold_remaining = torch.zeros(self.num_envs, dtype=torch.long, device=self.device)
            self._ep_best_improvement = torch.zeros_like(self._previous_potential)
            self._best_success_run = torch.zeros(self.num_envs, dtype=torch.long, device=self.device)
            self._attempt_bonus_paid = torch.zeros(self.num_envs, dtype=torch.long, device=self.device)
            self._acc_violation_steps = torch.zeros(self.num_envs, dtype=torch.long, device=self.device)
            self._ep_peak_acceleration = torch.zeros_like(self._previous_potential)
            self._ep_peak_speed = torch.zeros_like(self._previous_potential)
            self._ep_peak_torque_ratio = torch.zeros_like(self._previous_potential)
            self._ep_acceleration_violations = torch.zeros_like(self._previous_potential)
            self._ep_torque_cost = torch.zeros_like(self._previous_potential)
            self._ep_sampled_steps = torch.zeros_like(self._previous_potential)
            low, high = self.franka.get_dofs_force_range(self.motors_dof)
            self._torque_scale = torch.maximum(low.abs(), high.abs()).clamp(min=1e-6)
        rel_z = self._relative_position()[:, 2]
        quality = self._quality(rel_z - self.desired_rel_z)
        self._previous_potential[idx] = quality[idx]
        self._previous_command_acc[idx] = 0.0
        self._prev_cuboid_rel_z[idx] = rel_z[idx]
        self._servo_linear_velocity[idx] = self.ee_link.get_vel()[idx]
        self._hold_reference_z[idx] = self.ee_link.get_pos()[idx, 2]
        self._hold_remaining[idx] = 0
        self._ep_best_improvement[idx] = 0.0
        self._best_success_run[idx] = 0
        self._attempt_bonus_paid[idx] = 0
        self._acc_violation_steps[idx] = 0
        self._ep_peak_acceleration[idx] = 0.0
        self._ep_peak_speed[idx] = 0.0
        self._ep_peak_torque_ratio[idx] = 0.0
        self._ep_acceleration_violations[idx] = 0.0
        self._ep_torque_cost[idx] = 0.0
        self._ep_sampled_steps[idx] = 0.0

    def reset(self, envs_idx=None, warmup_steps=100):
        # A selective reset must not step every other environment through warmup
        # or discard its active reference segment/global simulation clock.
        if envs_idx is not None and hasattr(self, "_previous_potential"):
            self._reset_idx(envs_idx)
            self._update_obs_buf()
            return self.get_observations()
        obs = super().reset(envs_idx=envs_idx, warmup_steps=warmup_steps)
        idx = torch.arange(self.num_envs, device=self.device) if envs_idx is None else envs_idx
        self._reset_reward_state(idx)
        return obs

    def _reset_idx(self, envs_idx):
        super()._reset_idx(envs_idx)
        self._reset_reward_state(envs_idx)

    def step(self, actions, *, update_visualizer=True, refresh_visualizer=True):
        if actions.shape != (self.num_envs, self.num_actions):
            raise ValueError(f"actions must be ({self.num_envs}, {self.num_actions}), got {actions.shape}")
        self._motion_sums = {
            key: torch.zeros(self.num_envs, device=self.device)
            for key in ("torque", "acceleration", "acceleration_excess", "against_gravity")
        }
        self._peak_acceleration = torch.zeros(self.num_envs, device=self.device)
        self._peak_torque_ratio = torch.zeros_like(self._peak_acceleration)
        self._last_physics_velocity = self.ee_link.get_vel().clone()
        self._peak_speed = self._last_physics_velocity.norm(dim=-1)
        self._physics_samples = 0
        self._last_policy_acc = self._previous_command_acc.clone()
        # Preserve the existing --zero pulse/direction-change curriculum. Reward
        # its raw policy output too, so forced pauses do not hide bad commands.
        self._raw_velocity_action = actions[:, 0].clamp(-1.0, 1.0)
        return super().step(
            actions, update_visualizer=update_visualizer, refresh_visualizer=refresh_visualizer
        )

    def _sample_target_z(self, t):
        # The original trapezoidal endpoint integration describes a constant-
        # acceleration segment exactly. This form avoids float32 cancellation
        # in the cubic Hermite derivative at 1 kHz.
        if self._seg_end is None:
            return self.target_z.clone(), self.target_z_vel.clone()
        elapsed = (t - self._seg_t0).clamp(0.0, self.target_period)
        z0, v0 = self._seg_start[:, 0], self._seg_start[:, 1]
        acceleration = (self._seg_end[:, 1] - v0) / self.target_period
        return z0 + v0 * elapsed + 0.5 * acceleration * elapsed.square(), v0 + acceleration * elapsed

    def _record_acceleration(self):
        velocity = self.ee_link.get_vel()
        acceleration = (velocity - self._last_physics_velocity) / self.dt
        self._last_physics_velocity = velocity.clone()
        # Torque is sampled separately before integration, after issuing the
        # command: get_dofs_control_force recomputes the PD actuator force.
        costs = motion_costs(
            acceleration, torch.zeros_like(self._torque_scale), self._torque_scale, self.Z_ACC_MAX
        )
        for key in self._motion_sums:
            if key != "torque":
                self._motion_sums[key] += costs[key]
        self._peak_acceleration = torch.maximum(self._peak_acceleration, acceleration.norm(dim=-1))
        self._peak_speed = torch.maximum(self._peak_speed, velocity.norm(dim=-1))

    def _control_once(self, target_z, target_z_vel):
        if self._physics_samples:
            self._record_acceleration()
        target_pos = self.target_center.clone()
        target_pos[:, 2] = target_z
        linear_velocity = self.pos_gain * (target_pos - self.ee_link.get_pos())
        linear_velocity[:, 2] += target_z_vel
        # Bound the complete Cartesian command, including position correction.
        # The inherited underdamped filter can overshoot a bounded input.
        delta = linear_velocity - self._servo_linear_velocity
        factor = (self.CARTESIAN_ACC_MAX * self.dt / delta.norm(dim=-1).clamp(min=1e-9)).clamp(max=1.0)
        self._servo_linear_velocity += delta * factor.unsqueeze(-1)
        rotation_error = _tc_quat_to_rotvec(_tc_quat_mul(self.target_quat, _tc_inv_quat(self.ee_link.get_quat())))
        velocity = torch.cat((self._servo_linear_velocity, self.rot_gain * rotation_error), dim=-1)
        jacobian = self.franka.get_jacobian(link=self.ee_link)[:, :, self.motors_dof]
        system = jacobian @ jacobian.transpose(-1, -2) + self.jacobian_regularizer.unsqueeze(0)
        solution = torch.linalg.solve(system, velocity.unsqueeze(-1))
        joint_velocity = (jacobian.transpose(-1, -2) @ solution).squeeze(-1)
        self.franka.control_dofs_velocity(joint_velocity, dofs_idx_local=self.motors_dof)
        torque = self.franka.get_dofs_control_force(self.motors_dof)
        ratio = torque / self._torque_scale
        self._motion_sums["torque"] += ratio.square().mean(dim=-1)
        self._peak_torque_ratio = torch.maximum(self._peak_torque_ratio, ratio.abs().amax(dim=-1))
        self._physics_samples += 1

    def _compute_done_and_reward(self):
        self._record_acceleration()  # Include the final physics step of this action.
        costs = {key: value / self._physics_samples for key, value in self._motion_sums.items()}
        # Root-mean-square over the policy step. Unlike the peak, a millisecond
        # contact impulse barely moves it, so it can gate success and failure.
        acceleration_rms = costs["acceleration"].sqrt() * self.Z_ACC_MAX
        effort_scale = self.EFFORT_ANNEAL_FLOOR + (1.0 - self.EFFORT_ANNEAL_FLOOR) * self._anneal_progress()
        attempt_scale = 1.0 - self._anneal_progress()
        rel = self._relative_position()
        ee_z = self.ee_link.get_pos()[:, 2]
        ee_velocity = self.ee_link.get_vel()
        forces = self.franka.get_links_net_contact_force()
        left_force = forces[:, self.left_finger.idx_local].norm(dim=-1)
        right_force = forces[:, self.right_finger.idx_local].norm(dim=-1)
        # Both fingers must have contact. An average can mistake one-sided
        # collision force for a grasp.
        firm = torch.minimum(left_force, right_force) >= self.REGRASP_FORCE_THRESHOLD
        released = torch.maximum(left_force, right_force) < self.FORCE_FREE_THRESHOLD
        self._firm_grasp_steps = torch.where(firm, self._firm_grasp_steps + 1, 0)
        confirmed = self._firm_grasp_steps >= math.ceil(self.GRASP_CONFIRM_SECONDS / self.target_period)
        release_start = released & ~self._in_release
        self._release_cuboid_rel_z = torch.where(release_start, rel[:, 2], self._release_cuboid_rel_z)
        self._release_start_step = torch.where(release_start, self.episode_length_buf, self._release_start_step)
        regrasp = self._in_release & confirmed
        self._in_release = (self._in_release | released) & ~regrasp
        self._regrasp_count += regrasp.long()
        # Error removed by this one release/regrasp cycle, measured against the
        # position snapshotted when the fingers opened.
        improvement = (self._release_cuboid_rel_z - self.desired_rel_z).abs() - (rel[:, 2] - self.desired_rel_z).abs()
        self._push_history(self._z_improve_hist, improvement, regrasp)

        # Only ask for a settle once the object is close enough that settling is
        # the right move. Armed after every regrasp, this window covered most of
        # the episode and taxed the velocity build-up each further pulse needs.
        settle = regrasp & ((rel[:, 2] - self.desired_rel_z).abs() <= self.HOLD_ARM_TOLERANCE)
        self._hold_reference_z = torch.where(settle, ee_z, self._hold_reference_z)
        hold_steps = math.ceil(self.SUCCESS_HOLD_SECONDS / self.target_period)
        self._hold_remaining = torch.where(settle, hold_steps, self._hold_remaining)
        policy_hold = self._hold_remaining > 0
        hold_active = policy_hold | (self._zero_hold_countdown > 0)
        slip_speed = (rel[:, 2] - self._prev_cuboid_rel_z) / self.target_period
        self._prev_cuboid_rel_z = rel[:, 2].clone()
        error = rel[:, 2] - self.desired_rel_z
        quality = self._quality(error)
        acceleration_violation = self._peak_acceleration > self.ACC_FAIL_LIMIT
        self._acc_violation_steps = torch.where(
            acceleration_violation, self._acc_violation_steps + 1, torch.zeros_like(self._acc_violation_steps)
        )
        # Terminate only when the servo really is out of bounds, not when a
        # single impulse sample is. The excess cost carries the soft limit.
        acceleration_runaway = (
            self._acc_violation_steps >= self.ACC_VIOLATION_FAIL_STEPS
        ) | (acceleration_rms > self.ACC_FAIL_LIMIT)
        # Kept separately so the training log names the condition that fired.
        causes = {
            "lateral": rel[:, :2].abs().amax(dim=-1) > 0.04,
            "slip_out": rel[:, 2].abs() > 0.15,
            "gripper_shut": self.get_fingertip_distance() < 0.01,
            "ee_low": ee_z < 0.6,
            "ee_high": ee_z > 0.96,
            "acceleration": acceleration_runaway,
            "regrasp_limit": (
                self._regrasp_count >= self.REGRASP_TERMINATION_COUNT
                if self.limit_regrasp else torch.zeros_like(acceleration_runaway)
            ),
        }
        self._fail_causes = causes
        fail = torch.zeros_like(acceleration_runaway)
        for condition in causes.values():
            fail = fail | condition
        # No two-finger force gate and no height gate. Over the 200 ms hold the
        # slip-speed condition already implies the object is held: nothing in
        # free fall keeps a near-zero speed relative to the hand for that long.
        # Height is graded into the payout below instead of gating it.
        candidate = (
            (error.abs() <= 0.0075) & (rel[:, :2].norm(dim=-1) <= 0.015)
            & (ee_velocity[:, 2].abs() < self.SUCCESS_EE_VZ_MAX)
            & (ee_velocity[:, :2].norm(dim=-1) < self.SUCCESS_EE_LATERAL_VEL_MAX)
            & (slip_speed.abs() < 0.01)
            & (self._regrasp_count > 0)
            & (acceleration_rms <= self.SUCCESS_ACC_RMS_MAX) & ~fail
        )
        self._success_steps = torch.where(candidate, self._success_steps + 1, 0)
        success = self._success_steps >= hold_steps
        # Pay each new longest qualifying run, once per episode, so the hold is a
        # ramp instead of a cliff. Re-entering the goal region pays nothing until
        # the policy holds it longer than it ever has this episode.
        dwell_reward = self.SETTLE_DWELL_REWARD * (
            self._success_steps - self._best_success_run
        ).clamp(min=0).float()
        self._best_success_run = torch.maximum(self._best_success_run, self._success_steps)
        # Timeout keeps its truncation/bootstrap semantics. Success or true
        # failure takes precedence on the last step.
        timeout = (self.episode_length_buf >= self.max_episode_length) & ~success & ~fail
        done = success | fail | timeout
        terminal_potential = torch.where(success | fail, 0.0, quality)
        progress = self.PROGRESS_WEIGHT * (self.REWARD_GAMMA * terminal_potential - self._previous_potential)
        self._previous_potential = quality.clone()

        # The one achievement bonus, and the only one the original working
        # reward had: pay for the error a single cycle actually removed, never
        # for proximity the policy did not produce. Superlinear, so one decisive
        # slip beats a chain of nudges. Backsliding is charged at a multiple of
        # the same curve, so releasing high to regrasp low cannot be farmed.
        # Eligibility is capped per episode, matching the parent environment.
        eligible = regrasp & ~fail & (self._regrasp_count <= self.REGRASP_BONUS_MAX_EVENTS)
        gain = (improvement / self.REGRASP_REFERENCE_SLIP).clamp(-1.0, 1.0)
        regrasp_reward = self.REGRASP_BONUS_WEIGHT * eligible * torch.where(
            gain >= 0.0, gain.square(), -self.REGRASP_BACKSLIDE_MULTIPLIER * gain.square()
        )
        # Bootstrap exploration: a completed cycle pays even when it did not
        # improve, for the first few attempts, fading as the policy learns. It is
        # never replaced by a per-event cost; after the anneal an attempt is free.
        pay_attempt = regrasp & ~fail & (self._attempt_bonus_paid < self.ATTEMPT_BONUS_MAX_EVENTS)
        attempt_reward = (self.ATTEMPT_BONUS * attempt_scale) * pay_attempt
        self._attempt_bonus_paid += pay_attempt.long()
        period = self.target_period
        # Graded on the hand's distance from its home height, which is the only
        # pose axis the policy controls; the servo holds the rest.
        pose_quality = self.SUCCESS_POSE_FLOOR + (1.0 - self.SUCCESS_POSE_FLOOR) * torch.exp(
            -0.5 * ((ee_z - self.EE_Z_HOME) / self.SUCCESS_POSE_SCALE).square()
        )
        base = (
            self.SUCCESS_BONUS * pose_quality * success
            - self.FAIL_PENALTY * fail
            - self.TIMEOUT_PENALTY * timeout
            - self.TIME_COST * period
        )
        torque_penalty = -self.TORQUE_WEIGHT * effort_scale * period * costs["torque"]
        # The excess term is the standing acceleration bound now that a single
        # sample no longer terminates, so it is never annealed or capped.
        acceleration_soft = -effort_scale * period * 0.3 * costs["acceleration"]
        acceleration_bound = -period * self.ACC_EXCESS_WEIGHT * costs["acceleration_excess"]
        # Direction matters. Upward acceleration fights gravity and draws more
        # joint torque, so it costs several times the symmetric acceleration cost
        # at the same magnitude. Downward acceleration pays only the symmetric one.
        gravity_penalty = -self.GRAVITY_WEIGHT * effort_scale * period * costs["against_gravity"]
        # True command jerk: change of acceleration divided by time, m/s³.
        jerk = (self.target_z_acc - self._last_policy_acc) / period
        jerk_penalty = -self.JERK_WEIGHT * effort_scale * period * (jerk / 150.0).square()
        self._previous_command_acc = self.target_z_acc.clone()
        # Measured peak Cartesian speed over the step, not the raw command, so a
        # servo that overshoots its reference is charged for what it actually did.
        speed_excess = (self._peak_speed - self.SPEED_FREE).clamp(min=0.0) / self.SPEED_MARGIN
        speed_penalty = -self.SPEED_WEIGHT * effort_scale * period * speed_excess.square()
        velocity_penalty = (
            -self.VEL_CMD_WEIGHT * effort_scale * period * self._raw_velocity_action.square()
            if self.minimize_vel else torch.zeros_like(ee_z)
        )
        # The forced zero hold overrides the action and its duration is not
        # observable, so only the post-regrasp settle charges the raw command.
        hold_penalty = -period * (
            hold_active * (
                2.0 * (ee_velocity[:, 2] / 0.1).square()
                + ((ee_z - self._hold_reference_z) / 0.025).square()
            )
            + 0.5 * policy_hold * self._raw_velocity_action.square()
        )
        centering_penalty = (
            -self.CENTERING_WEIGHT * effort_scale * period * (rel[:, :2] / 0.02).square().sum(dim=-1)
        )
        # Pull the hand back to its home height. Free inside a small band so a
        # pulse can move, and steep well before the hard bounds.
        outside = ((ee_z - self.EE_Z_HOME).abs() - self.EE_Z_FREE_BAND).clamp(min=0.0)
        workspace_penalty = -self.WORKSPACE_WEIGHT * period * (outside / self.EE_Z_MARGIN).square()
        # Same treatment for the object's grip on the fingers. Losing it was the
        # only failure cause left standing, and the hard bound at 0.15 gave no
        # warning on the way there.
        slipping = (rel[:, 2].abs() - self.REL_Z_FREE).clamp(min=0.0)
        grip_penalty = -self.SLIP_BOUND_WEIGHT * period * (slipping / self.REL_Z_MARGIN).square()
        # No combination of efficiency costs may outweigh doing the task. When
        # their sum would exceed the per-step ceiling, scale them down together
        # so each keeps its share and the reward still equals the sum of terms.
        capped = (torque_penalty, acceleration_soft, gravity_penalty, jerk_penalty,
                  speed_penalty, velocity_penalty, centering_penalty)
        effort_scale_down = (
            self._effort_cap() / sum(capped).abs().clamp(min=1e-9)
        ).clamp(max=1.0)
        (torque_penalty, acceleration_soft, gravity_penalty, jerk_penalty,
         speed_penalty, velocity_penalty, centering_penalty) = (
            term * effort_scale_down for term in capped
        )
        acceleration_penalty = acceleration_soft + acceleration_bound
        terms = dict(
            base=base, progress=progress, regrasp=regrasp_reward, dwell=dwell_reward,
            attempt=attempt_reward,
            torque=torque_penalty, acceleration=acceleration_penalty, gravity=gravity_penalty,
            jerk=jerk_penalty, speed=speed_penalty, vel_cmd=velocity_penalty,
            hold=hold_penalty, centering=centering_penalty, workspace=workspace_penalty,
            grip=grip_penalty,
        )
        reward = sum(terms.values())
        for key, value in {"total": reward, **terms}.items():
            self._ep_term_sums[key] += value
        self._ep_peak_acceleration = torch.maximum(self._ep_peak_acceleration, self._peak_acceleration)
        self._ep_peak_speed = torch.maximum(self._ep_peak_speed, self._peak_speed)
        self._ep_peak_torque_ratio = torch.maximum(self._ep_peak_torque_ratio, self._peak_torque_ratio)
        self._ep_acceleration_violations += acceleration_violation
        self._ep_torque_cost += costs["torque"]
        self._ep_sampled_steps += 1
        self._ep_best_improvement = torch.maximum(self._ep_best_improvement, improvement * regrasp)
        self._hold_remaining = (self._hold_remaining - 1).clamp(min=0)
        self._effort_scale = effort_scale
        self._attempt_scale = attempt_scale
        self.last_reward_terms = {
            key: value.detach().clone() for key, value in dict(
                base_reward=base, progress_reward=progress, regrasp_bonus=regrasp_reward,
                dwell_reward=dwell_reward, attempt_bonus=attempt_reward,
                speed_penalty=speed_penalty, best_success_run=self._best_success_run.float(),
                peak_speed=self._peak_speed, effort_scale_down=effort_scale_down,
                torque_penalty=torque_penalty, z_acc_penalty=acceleration_penalty,
                gravity_penalty=gravity_penalty, jerk_penalty=jerk_penalty,
                vel_cmd_penalty=velocity_penalty, post_pulse_hold_penalty=hold_penalty,
                centering=centering_penalty, workspace_penalty=workspace_penalty,
                grip_penalty=grip_penalty, z_improvement=improvement,
                avg_force=0.5 * (left_force + right_force), regrasp_event=regrasp.float(),
                success_pose_quality=pose_quality,
                success=success.float(), fail=fail.float(), timeout=timeout.float(), done=done.float(),
                success_candidate=candidate.float(), success_steps=self._success_steps.float(),
                peak_acceleration=self._peak_acceleration, peak_torque_ratio=self._peak_torque_ratio,
                acceleration_rms=acceleration_rms, torque_cost=costs["torque"],
                acceleration_violation=acceleration_violation.float(),
                acceleration_runaway=acceleration_runaway.float(),
            ).items()
        }
        return done, reward, timeout

    def _update_episode_extras(self, done_idx):
        super()._update_episode_extras(done_idx)
        if done_idx.numel() == 0:
            return
        self.extras["episode"].update(
            peak_acceleration=self._ep_peak_acceleration[done_idx].mean(),
            peak_speed=self._ep_peak_speed[done_idx].mean(),
            peak_torque_ratio=self._ep_peak_torque_ratio[done_idx].mean(),
            acceleration_violation_rate=(self._ep_acceleration_violations[done_idx] > 0).float().mean(),
            torque_rms_fraction=(
                self._ep_torque_cost[done_idx] / self._ep_sampled_steps[done_idx].clamp(min=1)
            ).sqrt().mean(),
            best_improvement=self._ep_best_improvement[done_idx].mean(),
            best_success_run=self._best_success_run[done_idx].float().mean(),
            **{f"fail_{name}": self._fail_causes[name][done_idx].float().mean()
               for name in self.FAIL_CAUSES},
            effort_scale=torch.tensor(self._effort_scale, device=self.device),
            attempt_scale=torch.tensor(self._attempt_scale, device=self.device),
        )

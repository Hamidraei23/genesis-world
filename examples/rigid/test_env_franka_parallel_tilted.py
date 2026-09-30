"""Geometry and reward regression checks, runnable without building a scene.

python -m unittest discover -s examples/rigid -p test_env_franka_parallel_tilted.py
"""

import math
import unittest
from types import SimpleNamespace

import torch

from env_franka_parallel import FrankaEnvParallel as OriginalEnv
from env_franka_parallel_tilted import FrankaEnvParallelTilted as TiltedEnv


class TiltedFrameTests(unittest.TestCase):
    def make_env(self, cls, angle=30.0):
        env = cls.__new__(cls)
        env.device = torch.device("cpu")
        env.num_envs = 4
        env.normalize = False
        env.randomize = False
        env.max_episode_length = 450
        env.task_origin = torch.tensor([[0.3, 0.03, 0.85]]).repeat(4, 1)
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        env.task_axes = torch.tensor([[1.0, 0.0, 0.0], [0.0, c, s], [0.0, -s, c]]).repeat(4, 1, 1)
        env.target_center = env.task_origin.clone()
        for name in (
            "_firm_grasp_steps",
            "_success_steps",
            "_regrasp_count",
            "_release_start_step",
            "_steps_since_pulse",
            "_post_pulse_delay_countdown",
            "direction_change_count",
        ):
            setattr(env, name, torch.zeros(4, dtype=torch.long))
        env._pulse_lockout_steps = 60
        env._initial_gap = torch.full((4,), -1.0)
        env._progress_potential = torch.zeros(4)
        env.target_period = 0.02
        env._gripper_pulse_steps = torch.zeros(4, dtype=torch.long)
        env._in_release = torch.ones(4, dtype=torch.bool)
        env._release_cuboid_rel_z = torch.full((4,), -0.01)
        env._post_pulse_hold_countdown = torch.full((4,), 3, dtype=torch.long)
        env.episode_length_buf = torch.tensor([80, 80, 450, 80])
        env._firm_grasp_steps.fill_(3)
        env._success_steps.fill_(4)
        env.desired_rel_z = torch.full((4,), 0.02)
        env.target_z_vel = torch.tensor([0.0, 0.1, -0.1, 0.2])
        env.prev_target_z_vel = -env.target_z_vel
        env.target_z_acc = torch.tensor([0.0, 5.0, 8.0, 10.0])
        if cls is TiltedEnv:
            env._reset_smoothness_state(torch.arange(env.num_envs))
        # Success, transverse drop, timeout, and improving regrasp/hold penalty.
        ee_task = torch.tensor([[0.0, 0.0, 0.8]]).repeat(4, 1)
        vel_task = torch.tensor([[0.0, 0.0, 0.0], [0.0, 0.0, 0.1], [0.0, 0.0, -0.1], [0.0, 0.0, 0.2]])
        rel_task = torch.tensor([[0.0, 0.0, 0.02], [0.05, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.01]])
        left_task = ee_task + torch.tensor([0.0, 0.0125, 0.0])
        right_task = ee_task - torch.tensor([0.0, 0.0125, 0.0])

        def world(v, position=False):
            if cls is OriginalEnv:
                return v
            if position:
                v = v - torch.tensor([0.0, 0.0, TiltedEnv.HOME_TASK_Z])
            result = torch.einsum("nij,nj->ni", env.task_axes, v)
            return result + env.task_origin if position else result

        env.ee_link = SimpleNamespace(get_pos=lambda: world(ee_task, True), get_vel=lambda: world(vel_task))
        env.cuboid = SimpleNamespace(get_pos=lambda: world(ee_task + rel_task, True))
        env.left_finger = SimpleNamespace(idx_local=0, pos=world(left_task, True))
        env.right_finger = SimpleNamespace(idx_local=1, pos=world(right_task, True))
        env._fingertip_pos = lambda finger: finger.pos
        forces = torch.zeros(4, 2, 3)
        forces[:, :, 1] = 1.0
        env.franka = SimpleNamespace(get_links_net_contact_force=lambda: forces)
        return env

    def test_command_is_straight_and_preserves_speed(self):
        env = self.make_env(TiltedEnv)
        z = torch.full((4,), env.HOME_TASK_Z + 0.1)
        displacement = env._task_target_position(z) - env.task_origin
        torch.testing.assert_close(displacement.norm(dim=-1), torch.full((4,), 0.1))
        torch.testing.assert_close(env._task_position_z(env._task_target_position(z)), z)
        torch.testing.assert_close((displacement * env.task_axes[:, :, 1]).sum(-1), torch.zeros(4))

    def test_observations_and_rewards_match_rotated_original(self):
        for angle in (0.0, 15.0, 30.0, 45.0):
            with self.subTest(angle=angle):
                original, tilted = self.make_env(OriginalEnv), self.make_env(TiltedEnv, angle)
                # Compare frame behavior with matching settings where defaults differ.
                original.SUCCESS_REWARD = tilted.SUCCESS_REWARD
                original.VEL_SIGN_FLIP_PENALTY_PER_MPS = tilted.VEL_SIGN_FLIP_PENALTY_PER_MPS
                original._update_obs_buf()
                tilted._update_obs_buf()
                torch.testing.assert_close(tilted.obs_buf, original.obs_buf)
                # done and timeout must match exactly; rew_buf deliberately does not,
                # because the tilted env sums a different set of terms.
                actual_done, _, actual_timeout = tilted._compute_done_and_reward()
                expected_done, _, expected_timeout = original._compute_done_and_reward()
                torch.testing.assert_close(actual_done, expected_done)
                torch.testing.assert_close(actual_timeout, expected_timeout)
                # Unchanged terms must agree; regrasp and smoothness now use different formulas.
                shared = tilted.last_reward_terms.keys() & original.last_reward_terms.keys()
                self.assertIn("regrasp_bonus", shared)
                self.assertIn("z_acc_penalty", shared)
                for name in shared - {"regrasp_bonus", "z_acc_penalty", "jerk_penalty"}:
                    torch.testing.assert_close(
                        tilted.last_reward_terms[name],
                        original.last_reward_terms[name],
                        atol=0.005,
                        rtol=1e-5,
                    )
                self.assertEqual(tilted.last_reward_terms["ep_success"].tolist(), [1, 0, 0, 0])
                self.assertEqual(tilted.last_reward_terms["ep_fail_rel_x"].tolist(), [0, 1, 0, 0])
                original.normalize = tilted.normalize = True
                original._update_obs_buf()
                tilted._update_obs_buf()
                torch.testing.assert_close(tilted.obs_buf, original.obs_buf)

    def test_control_holds_orientation_and_commands_axis_velocity(self):
        env = self.make_env(TiltedEnv)
        env.target_quat = torch.tensor([[1.0, 0.0, 0.0, 0.0]]).repeat(4, 1)
        env.ee_link.get_quat = lambda: env.target_quat.clone()
        env.ee_link.get_pos = lambda: env.task_origin.clone()
        env.pos_gain, env.rot_gain = 8.0, 4.0
        env.transverse_pos_gain = 80.0
        env.motors_dof = torch.arange(7)
        env.jacobian_regularizer = torch.zeros(6, 6)
        env.franka.get_jacobian = lambda **kwargs: torch.eye(6, 7).repeat(4, 1, 1)
        commands = []
        env.franka.control_dofs_velocity = lambda qvel, **kwargs: commands.append(qvel)
        env._filt_b0 = 1.0
        env._filt_b1 = env._filt_b2 = env._filt_a1 = env._filt_a2 = 0.0
        env._filt_u1 = env._filt_u2 = env._filt_y1 = env._filt_y2 = torch.zeros(4)
        env._control_once(torch.full((4,), env.HOME_TASK_Z), torch.full((4,), 0.2))
        torch.testing.assert_close(commands[0][:, :3], env.task_axes[:, :, 2] * 0.2)
        torch.testing.assert_close(commands[0][:, 3:], torch.zeros(4, 4))
        # A sideways displacement and a yaw error must produce restoring commands
        # without changing the commanded grasp-axis speed or reference quaternion.
        env.ee_link.get_pos = lambda: env.task_origin + env.task_axes[:, :, 1] * 0.001
        env.ee_link.get_quat = lambda: torch.tensor([[math.cos(0.05), 0.0, 0.0, math.sin(0.05)]]).repeat(4, 1)
        env._control_once(torch.full((4,), env.HOME_TASK_Z), torch.full((4,), 0.2))
        linear_task = env._world_vector_to_task(commands[1][:, :3])
        torch.testing.assert_close(linear_task[:, 1], torch.full((4,), -0.08), atol=2e-6, rtol=1e-5)
        torch.testing.assert_close(linear_task[:, 2], torch.full((4,), 0.2))
        torch.testing.assert_close(commands[1][:, 5], torch.full((4,), -0.4))
        torch.testing.assert_close(env.target_quat[:, 0], torch.ones(4))

    def _progress_stepper(self, env):
        """Drive the env's reward at a chosen grasp-axis gap and read the payout back."""
        env.desired_rel_z.zero_()
        midpoint = (env.left_finger.pos + env.right_finger.pos) / 2
        gap = torch.zeros(4)
        zeros = torch.zeros(4)
        env.cuboid = SimpleNamespace(get_pos=lambda: midpoint + torch.stack([zeros, zeros, gap], dim=-1))

        def step(values):
            gap.copy_(torch.as_tensor(values, dtype=torch.float32))
            env._compute_done_and_reward()
            return env.last_reward_terms["progress_reward"].clone()

        return step

    def test_progress_reward_shares_the_budget_by_fraction_of_initial_error(self):
        env = self.make_env(TiltedEnv, angle=0.0)
        step = self._progress_stepper(env)
        budget = TiltedEnv.PROGRESS_REWARD_BUDGET
        # Deliberately different initial errors: the total must not depend on the draw.
        initial = torch.tensor([0.0125, 0.0175, 0.02, 0.0225])

        # The first step of an episode only captures the denominator; it pays nothing.
        torch.testing.assert_close(step(initial), torch.zeros(4))
        torch.testing.assert_close(env._initial_gap, initial)
        # Half the error closed is half the budget, for every initial error.
        torch.testing.assert_close(step(initial / 2), torch.full((4,), budget / 2))
        torch.testing.assert_close(env.last_reward_terms["progress_frac"], torch.full((4,), 0.5))
        # Closing the rest pays the remainder, so reaching zero error pays exactly the
        # budget in total -- and every bit of it arrived on a step, not at termination.
        torch.testing.assert_close(step(torch.zeros(4)), torch.full((4,), budget / 2))
        torch.testing.assert_close(env.last_reward_terms["progress_frac"], torch.ones(4))
        # Sitting on the target pays nothing further: loitering cannot be farmed.
        torch.testing.assert_close(step(torch.zeros(4)), torch.zeros(4))
        torch.testing.assert_close(step(torch.zeros(4)), torch.zeros(4))

    def test_progress_reward_charges_backsliding_and_totals_path_independently(self):
        env = self.make_env(TiltedEnv, angle=0.0)
        step = self._progress_stepper(env)
        budget = TiltedEnv.PROGRESS_REWARD_BUDGET
        initial = torch.tensor([0.0125, 0.0175, 0.02, 0.0225])

        step(initial)
        total = torch.zeros(4)
        # Losing half the initial gap again costs exactly what closing it would pay.
        total += step(initial * 1.5)
        torch.testing.assert_close(total, torch.full((4,), -budget / 2))
        # Returning to the start refunds it, so an in-and-out cycle nets zero.
        total += step(initial)
        torch.testing.assert_close(total, torch.zeros(4))
        # The floor bounds the debt at one budget, reached at twice the initial gap.
        total += step(initial * 2.0)
        torch.testing.assert_close(total, torch.full((4,), -budget))
        # Beyond the floor the term goes flat rather than unbounded.
        torch.testing.assert_close(step(initial * 5.0), torch.zeros(4))
        torch.testing.assert_close(step(initial * 20.0), torch.zeros(4))
        # Whatever the detour, arriving at zero error leaves the episode total at the
        # budget: the payouts telescope to PHI(end) - PHI(start).
        total += step(torch.zeros(4))
        torch.testing.assert_close(total, torch.full((4,), budget))

    def test_progress_reward_resets_its_denominator_per_episode(self):
        env = self.make_env(TiltedEnv, angle=0.0)
        step = self._progress_stepper(env)
        budget = TiltedEnv.PROGRESS_REWARD_BUDGET

        step(torch.full((4,), 0.02))
        step(torch.full((4,), 0.01))
        # A reset re-arms the sentinel, so the next episode measures its own gap and
        # does not pay out the jump from the previous episode's final position.
        env._initial_gap.fill_(-1.0)
        env._progress_potential.zero_()
        torch.testing.assert_close(step(torch.full((4,), 0.005)), torch.zeros(4))
        torch.testing.assert_close(env._initial_gap, torch.full((4,), 0.005))
        # The new, smaller error is still worth the full budget to close.
        torch.testing.assert_close(step(torch.zeros(4)), torch.full((4,), budget))

    def test_progress_reward_guards_a_degenerate_initial_gap(self):
        env = self.make_env(TiltedEnv, angle=0.0)
        step = self._progress_stepper(env)
        # An env that resets already on target cannot divide by zero, and must not be
        # handed free reward for a gap it never had to close.
        torch.testing.assert_close(step(torch.zeros(4)), torch.zeros(4))
        torch.testing.assert_close(env._initial_gap, torch.full((4,), TiltedEnv.PROGRESS_MIN_INITIAL_GAP))
        torch.testing.assert_close(step(torch.zeros(4)), torch.zeros(4))

    def test_reward_sums_only_the_weighted_terms(self):
        env = self.make_env(TiltedEnv)
        _, reward, _ = env._compute_done_and_reward()
        weights = TiltedEnv.REWARD_TERM_WEIGHTS
        self.assertEqual(
            set(weights),
            {
                "base_reward",
                "progress_reward",
                "regrasp_bonus",
                "z_acc_penalty",
                "jerk_penalty",
                "vel_sign_flip_penalty",
                "blockade_motion_penalty",
                "speed_penalty",
            },
        )
        expected = sum(weights[k] * env.last_reward_terms[k] for k in weights)
        torch.testing.assert_close(reward, expected)
        # Dropped terms must not linger in the table, or its total would double-count.
        self.assertNotIn("post_pulse_hold_penalty", env.last_reward_terms)

    def test_regrasp_scale_cap_and_event_eligibility(self):
        env = self.make_env(TiltedEnv, angle=0.0)
        env.desired_rel_z.zero_()
        midpoint = (env.left_finger.pos + env.right_finger.pos) / 2
        env.cuboid.get_pos = lambda: midpoint
        env._release_cuboid_rel_z = torch.tensor([0.01, 0.03, 0.03, 0.03])
        env._in_release[2] = False  # no release -> regrasp event
        env._regrasp_count[3] = env.REGRASP_BONUS_MAX_COUNT
        env._compute_done_and_reward()
        # 10 mm improvement uses the new 7500 coefficient; 30 mm hits the raw cap.
        torch.testing.assert_close(env.last_reward_terms["regrasp_bonus"], torch.tensor([177.77778, 10000.0, 0.0, 0.0]))

    def test_regrasp_worsening_keeps_negative_multiplier(self):
        env = self.make_env(TiltedEnv, angle=0.0)
        env.desired_rel_z.zero_()
        env._release_cuboid_rel_z.zero_()
        midpoint = (env.left_finger.pos + env.right_finger.pos) / 2
        env.cuboid.get_pos = lambda: midpoint + torch.tensor([0.0, 0.0, 0.01])
        env._compute_done_and_reward()
        torch.testing.assert_close(
            env.last_reward_terms["regrasp_bonus"], torch.full((4,), -888.88889), rtol=1e-4, atol=0.001
        )

    def test_joint_angle_produces_requested_inclination(self):
        # The home joint-5 axis is not horizontal, so simply setting q5=-tilt
        # would not yield the requested inclination of the original world Z axis.
        axis = torch.tensor([-math.sin(1.36), 0.0, math.cos(1.36)], dtype=torch.float64)
        vertical = torch.tensor([0.0, 0.0, 1.0], dtype=torch.float64)
        for angle in (0.0, 0.1, 15.0, 30.0, 45.0):
            q5 = TiltedEnv._joint5_for_tilt(angle, float(axis[2]))
            rotated = (
                vertical * math.cos(q5)
                + torch.linalg.cross(axis, vertical) * math.sin(q5)
                + axis * torch.dot(axis, vertical) * (1.0 - math.cos(q5))
            )
            actual = math.degrees(math.acos(float(rotated[2])))
            self.assertAlmostEqual(actual, angle, places=7)
            self.assertLessEqual(q5, 0.0)
        for angle in (-1.0, 45.01, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                TiltedEnv._joint5_for_tilt(angle, float(axis[2]))

    def test_partial_frame_reset_preserves_other_environments(self):
        env = self.make_env(TiltedEnv)
        env.task_quat = torch.tensor([[1.0, 0.0, 0.0, 0.0]]).repeat(4, 1)
        env.ee_link.get_quat = lambda: env.task_quat.clone()
        origins, axes = env.task_origin.clone(), env.task_axes.clone()
        env._home_frame_quat = torch.tensor([1.0, 0.0, 0.0, 0.0])
        env._reset_task_frame(torch.tensor([1]))
        torch.testing.assert_close(env.task_origin[[0, 2, 3]], origins[[0, 2, 3]])
        torch.testing.assert_close(env.task_axes[[0, 2, 3]], axes[[0, 2, 3]])
        torch.testing.assert_close(env.task_axes[1].T @ env.task_axes[1], torch.eye(3))
        torch.testing.assert_close(torch.linalg.det(env.task_axes[1]), torch.tensor(1.0))
        torch.testing.assert_close(env.task_axes[1], torch.eye(3))


class PulseSmoothnessTests(unittest.TestCase):
    def make_env(self):
        env = TiltedEnv.__new__(TiltedEnv)
        env.num_envs = 2
        env.device = torch.device("cpu")
        env.target_z_acc = torch.zeros(2)
        env._reset_smoothness_state(torch.arange(2))
        return env

    def advance(self, env, acceleration, fired=(False, False)):
        env.prev_target_z_acc = env.target_z_acc.clone()
        env.target_z_acc = torch.tensor(acceleration, dtype=torch.float32)
        env._pulse_fired = torch.tensor(fired)
        return torch.stack(env._compute_smoothness_penalties(), dim=-1)

    def test_thirty_step_worst_case_and_constant_acceleration(self):
        env = self.make_env()
        total = torch.zeros(2)
        for step in range(30):
            terms = self.advance(env, [15.0 * (-1) ** step, 15.0])
            total += terms.sum(dim=-1)
            if step > 0:
                self.assertEqual(terms[0].sum().item(), -50.0)
                self.assertEqual(terms[1, 1].item(), 0.0)
        # First acceleration change is 0 -> 15, not -15 -> 15: 18.75 below the bound.
        torch.testing.assert_close(total, torch.tensor([-1481.25, -756.25]))
        env = self.make_env()
        env.target_z_acc.fill_(-15.0)
        total = 0.0
        for step in range(30):
            total += self.advance(env, [15.0 * (-1) ** step, 0.0])[0].sum().item()
        self.assertEqual(total, -1500.0)

    def test_refund_last_six_steps_and_exact_exemption_boundary(self):
        env = self.make_env()
        history = []
        for step in range(10):
            value = (6.0 + step) * (-1) ** step
            history.append(self.advance(env, [value, value]))
        refund = self.advance(env, [15.0, 15.0], fired=(True, False))
        torch.testing.assert_close(refund[0], -torch.stack(history[-6:])[:, 0].sum(dim=0))
        self.assertEqual(refund[1].sum().item(), -50.0)
        for step in range(8):
            value = -15.0 * (-1) ** step
            terms = self.advance(env, [value, value])
            torch.testing.assert_close(terms[0], torch.zeros(2))
            self.assertEqual(terms[1].sum().item(), -50.0)
        terms = self.advance(env, [-15.0, -15.0])
        torch.testing.assert_close(terms, torch.full((2, 2), -25.0))

    def test_refunded_and_exempt_steps_cannot_be_refunded_again(self):
        env = self.make_env()
        self.advance(env, [15.0, 15.0])
        first = self.advance(env, [-15.0, -15.0], fired=(True, False))
        torch.testing.assert_close(first[0], torch.tensor([25.0, 6.25]))
        second = self.advance(env, [15.0, 15.0], fired=(True, False))
        torch.testing.assert_close(second[0], torch.zeros(2))

    def test_partial_reset_clears_history_exemption_and_previous_acceleration(self):
        env = self.make_env()
        self.advance(env, [15.0, 15.0])
        self.advance(env, [-15.0, -15.0], fired=(True, False))
        history = env._smoothness_history[1].clone()
        env._reset_smoothness_state(torch.tensor([0]))
        self.assertEqual(env.prev_target_z_acc[0].item(), 0.0)
        self.assertFalse(env._pulse_fired[0].item())
        self.assertEqual(env._smoothness_free_steps[0].item(), 0)
        torch.testing.assert_close(env._smoothness_history[0], torch.zeros_like(history))
        torch.testing.assert_close(env._smoothness_history[1], history)
        self.assertEqual(env.prev_target_z_acc[1].item(), 15.0)

    def test_only_accepted_trigger_refunds_in_actual_step(self):
        env = TiltedFrameTests().make_env(TiltedEnv)
        env.target_z = torch.full((4,), env.HOME_TASK_Z)
        env.target_z_vel.zero_()
        env.target_z_acc.zero_()
        env.gripper_pos_min = torch.full((2,), env.GRIPPER_CLOSED)
        env.gripper_pos_max = torch.full((2,), env.GRIPPER_OPEN)
        env._prev_gripper_avg = torch.full((4,), env.GRIPPER_CLOSED)
        env._gripper_pulse_delays = torch.full((4,), env.PULSE_DELAY_STEPS)
        env._gripper_pulse_lengths = torch.full((4,), env.PULSE_LENGTH)
        env._post_pulse_delay_total = env._post_pulse_hold_total = 25
        # Accepted, blocked by lockout, blocked at episode start, already active.
        env._steps_since_pulse = torch.tensor([61, 0, 61, 61])
        env.episode_length_buf = torch.tensor([50, 50, 49, 50])
        env._gripper_pulse_steps = torch.tensor([0, 0, 0, 2])
        env._smoothness_history.fill_(-10.0)
        env.sim_step = 0
        env.dt = 0.001
        env.target_update_every = 0  # exercise real step/reward logic without physics
        env.num_actions = 3
        env.fingers_dof = torch.tensor([7, 8])
        env.franka.control_dofs_position = lambda *args, **kwargs: None
        env._reset_idx = lambda indices: None
        env._update_obs_buf = lambda: None
        env.get_observations = lambda: None
        env.extras = {}
        env.step(torch.ones(4, 3))
        self.assertEqual(env._pulse_fired.tolist(), [True, False, False, False])
        self.assertEqual(env._smoothness_free_steps.tolist(), [8, 0, 0, 0])
        torch.testing.assert_close(env.last_reward_terms["z_acc_penalty"], torch.tensor([60.0, -25.0, -25.0, -25.0]))
        torch.testing.assert_close(env.last_reward_terms["jerk_penalty"], torch.tensor([60.0, -6.25, -6.25, -6.25]))


if __name__ == "__main__":
    unittest.main()

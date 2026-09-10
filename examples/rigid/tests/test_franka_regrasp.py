"""Reward contract tests; run with python -m unittest discover -s examples/rigid/tests."""

import math
import unittest
from unittest.mock import Mock

import torch

from examples.rigid.env_franka_regrasp import FrankaEnvParallel, motion_costs


def reward_fixture():
    """Exercise the real reward/state machine with controlled physical states."""
    env = object.__new__(FrankaEnvParallel)
    env.num_envs = 1
    env.device = "cpu"
    env.target_period = 0.02
    env.max_episode_length = 450
    env.limit_regrasp = False
    env.minimize_vel = False
    env.desired_rel_z = torch.tensor([0.03])
    env.target_z_acc = torch.zeros(1)
    env.episode_length_buf = torch.ones(1, dtype=torch.long)
    env._relative_position = Mock(return_value=torch.tensor([[0.0, 0.0, 0.0]]))
    env.ee_link = Mock()
    env.ee_link.get_pos.return_value = torch.tensor([[0.0, 0.0, 0.8]])
    env.ee_link.get_vel.return_value = torch.zeros(1, 3)
    env.franka = Mock()
    env.franka.get_links_net_contact_force.return_value = torch.tensor([[[0.0, 1.0, 0.0], [0.0, -1.0, 0.0]]])
    env.left_finger = Mock(idx_local=0)
    env.right_finger = Mock(idx_local=1)
    env.get_fingertip_distance = Mock(return_value=torch.tensor([0.025]))
    env._record_acceleration = Mock()
    env._physics_samples = 1
    env._motion_sums = {
        key: torch.zeros(1) for key in motion_costs(torch.zeros(1, 3), torch.zeros(1, 7), torch.ones(7))
    }
    for name in (
        "_peak_acceleration", "_peak_torque_ratio", "_release_cuboid_rel_z", "_hold_reference_z",
        "_prev_cuboid_rel_z", "_last_policy_acc", "_raw_velocity_action", "_previous_command_acc",
        "_ep_peak_acceleration", "_ep_peak_torque_ratio", "_ep_acceleration_violations",
        "_ep_torque_cost", "_ep_sampled_steps", "_peak_speed", "_ep_peak_speed", "_ep_best_improvement",
    ):
        setattr(env, name, torch.zeros(1))
    for name in (
        "_firm_grasp_steps", "_regrasp_count", "_release_start_step", "_hold_remaining",
        "_zero_hold_countdown", "_success_steps", "_attempt_bonus_paid",
        "_acc_violation_steps", "_best_success_run",
    ):
        setattr(env, name, torch.zeros(1, dtype=torch.long))
    env._in_release = torch.zeros(1, dtype=torch.bool)
    env._z_improve_hist = torch.zeros(1, 2)
    env._previous_potential = env._quality(-env.desired_rel_z)
    env._ep_term_sums = {key: torch.zeros(1) for key in env.EP_LOG_TERMS}
    return env


def regrasp_from(env, release_at, land_at, calls=3):
    """One release/regrasp cycle whose improvement is release_at -> land_at."""
    env._release_cuboid_rel_z[:] = release_at
    env._relative_position.return_value[:, 2] = land_at
    env._in_release[:] = True
    for _ in range(calls):
        env._compute_done_and_reward()


def confirm_regrasp(env, calls=3):
    """Drive the release/confirm state machine to a confirmed regrasp event."""
    env._in_release[:] = True
    for _ in range(calls):
        env._compute_done_and_reward()


class MotionCostsTest(unittest.TestCase):
    def test_gravity_preference_and_torque_scaling(self):
        acceleration = torch.tensor([[0.0, 0.0, -10.0], [0.0, 0.0, 10.0]])
        costs = motion_costs(acceleration, torch.tensor([[1.0] * 7, [2.0] * 7]), torch.ones(7))
        self.assertEqual(costs["acceleration"][0], costs["acceleration"][1])
        self.assertLess(costs["against_gravity"][0], costs["against_gravity"][1])
        self.assertEqual(costs["torque"][1], 4.0 * costs["torque"][0])

    def test_limit_uses_all_axes_and_cost_is_smooth(self):
        acceleration = torch.tensor([[0.0, 0.0, 15.0], [12.0, 0.0, 10.0]], requires_grad=True)
        costs = motion_costs(acceleration, torch.zeros(2, 7), torch.ones(7))
        # A pulse at exactly the limit is free; only the 15.6 m/s2 diagonal pays.
        self.assertEqual(costs["acceleration_excess"][0], 0.0)
        self.assertGreater(costs["acceleration_excess"][1], 0.0)
        sum(cost.sum() for cost in costs.values()).backward()
        self.assertTrue(torch.isfinite(acceleration.grad).all())

    def test_commanding_the_full_limit_is_not_charged_as_excess(self):
        # The servo commands up to 15 m/s2 now, so that must cost nothing extra.
        costs = motion_costs(torch.tensor([[0.0, 0.0, -15.0]]), torch.zeros(1, 7), torch.ones(7))
        self.assertEqual(costs["acceleration_excess"].item(), 0.0)
        self.assertEqual(costs["against_gravity"].item(), 0.0)
        self.assertAlmostEqual(costs["acceleration"].item(), 1.0, places=6)


class QualityTest(unittest.TestCase):
    def test_fine_scale_keeps_a_gradient_inside_the_success_tolerance(self):
        env = reward_fixture()
        errors = torch.tensor([0.0, 0.002, 0.005], requires_grad=True)
        quality = env._quality(errors)
        self.assertAlmostEqual(quality[0].item(), 1.0, places=6)
        self.assertTrue((quality[:-1] > quality[1:]).all())
        quality.sum().backward()
        # The single coarse Gaussian is nearly flat here; the fine one is not.
        coarse_only = torch.exp(-0.5 * (errors.detach() / env.ERROR_SCALE).square())
        coarse_slope = (coarse_only[2] - coarse_only[1]) / 0.003
        self.assertGreater(abs(errors.grad[2].item()), 5.0 * abs(coarse_slope.item()))


class RewardTest(unittest.TestCase):
    def test_opening_and_single_sided_contact_do_not_pay(self):
        env = reward_fixture()
        env.franka.get_links_net_contact_force.return_value.zero_()
        env._compute_done_and_reward()
        self.assertTrue(env._in_release.item())
        self.assertEqual(env.last_reward_terms["regrasp_bonus"], 0.0)
        env.franka.get_links_net_contact_force.return_value[0, 0, 1] = 10.0
        for _ in range(5):
            env._compute_done_and_reward()
        self.assertEqual(env._regrasp_count, 0)
        self.assertEqual(env.last_reward_terms["success"], 0.0)

    def test_a_confirmed_cycle_pays_for_what_it_moved(self):
        env = reward_fixture()
        env._relative_position.return_value[:, 2] = 0.02
        confirm_regrasp(env)
        self.assertEqual(env._regrasp_count, 1)
        gain = 0.02 / env.REGRASP_REFERENCE_SLIP  # 20 mm error removed, at the reference
        self.assertAlmostEqual(env.last_reward_terms["regrasp_bonus"].item(),
                               env.REGRASP_BONUS_WEIGHT * min(gain, 1.0) ** 2, places=4)
        self.assertAlmostEqual(env._z_improve_hist[0, -1].item(), 0.02, places=6)
        # Holding the same position is not a new cycle and pays nothing more.
        env._compute_done_and_reward()
        self.assertEqual(env.last_reward_terms["regrasp_bonus"], 0.0)

    def test_success_requires_regrasp_and_continuous_stable_hold(self):
        env = reward_fixture()
        env._relative_position.return_value[:, 2] = 0.03
        for _ in range(20):
            env._compute_done_and_reward()
        self.assertEqual(env.last_reward_terms["success"], 0.0)
        env._in_release[:] = True
        hold = math.ceil(env.SUCCESS_HOLD_SECONDS / env.target_period)
        for _ in range(hold - 1):
            env._compute_done_and_reward()
        self.assertEqual(env.last_reward_terms["success"], 0.0)
        env.ee_link.get_vel.return_value[:, 2] = 0.03
        env._compute_done_and_reward()
        self.assertEqual(env._success_steps, 0)
        env.ee_link.get_vel.return_value.zero_()
        for step in range(hold):
            if step == hold - 1:
                env.episode_length_buf[:] = env.max_episode_length
            done, _, timeout = env._compute_done_and_reward()
        self.assertTrue(done.item())
        self.assertEqual(env.last_reward_terms["success"], 1.0)
        self.assertFalse(timeout.item())
        quality = env.last_reward_terms["success_pose_quality"].item()
        self.assertAlmostEqual(env.last_reward_terms["base_reward"].item(),
                               env.SUCCESS_BONUS * quality - env.TIME_COST * env.target_period,
                               places=4)

    def test_timeout_bootstraps_and_gets_its_own_penalty(self):
        env = reward_fixture()
        env.episode_length_buf[:] = 450
        done, _, timeout = env._compute_done_and_reward()
        self.assertTrue(done.item() and timeout.item())
        self.assertEqual(env.last_reward_terms["fail"], 0.0)
        self.assertAlmostEqual(env.last_reward_terms["base_reward"].item(), -0.03, places=5)

    def test_surviving_an_episode_beats_ending_it_early(self):
        # A policy that cannot succeed yet must still prefer the timeout to a
        # deliberate failure, or it learns to drop the object immediately.
        env = reward_fixture()
        timeout_cost = env.TIMEOUT_PENALTY + env.TIME_COST * env.target_period * env.max_episode_length
        self.assertGreater(env.FAIL_PENALTY, timeout_cost)

    def test_attempting_beats_holding_still_before_annealing(self):
        # Four attempt bonuses must outweigh the time cost and timeout penalty,
        # otherwise freezing is the optimum and no regrasp is ever explored.
        env = reward_fixture()
        explore = env.ATTEMPT_BONUS * env.ATTEMPT_BONUS_MAX_EVENTS
        freeze = env.TIMEOUT_PENALTY + env.TIME_COST * env.target_period * env.max_episode_length
        self.assertGreater(explore, freeze)


class SuccessGateTest(unittest.TestCase):
    def _hold_on_target(self, env, steps=None):
        """Sit exactly on target long enough for the hold to complete."""
        env._relative_position.return_value[:, 2] = env.desired_rel_z.item()
        env._prev_cuboid_rel_z[:] = env.desired_rel_z.item()  # already settled, no slip speed
        env._regrasp_count[:] = 1
        if steps is None:
            steps = math.ceil(env.SUCCESS_HOLD_SECONDS / env.target_period)
        for _ in range(steps):
            done, _, _ = env._compute_done_and_reward()
        return env

    def test_success_no_longer_needs_two_finger_force(self):
        env = reward_fixture()
        env.franka.get_links_net_contact_force.return_value.zero_()  # no contact at all
        self._hold_on_target(env)
        self.assertEqual(env.last_reward_terms["success"], 1.0)

    def test_success_no_longer_needs_a_height_window(self):
        env = reward_fixture()
        env.ee_link.get_pos.return_value = torch.tensor([[0.0, 0.0, 0.65]])  # below the old gate
        env._hold_reference_z[:] = 0.65
        self._hold_on_target(env)
        self.assertEqual(env.last_reward_terms["success"], 1.0)

    def test_the_payout_grades_on_distance_from_the_home_pose(self):
        payouts = {}
        for height in (0.75, 0.80, 0.88, 0.62):
            env = reward_fixture()
            env.ee_link.get_pos.return_value = torch.tensor([[0.0, 0.0, height]])
            env._hold_reference_z[:] = height
            self._hold_on_target(env)
            self.assertEqual(env.last_reward_terms["success"], 1.0)
            payouts[height] = env.last_reward_terms["success_pose_quality"].item()
        self.assertAlmostEqual(payouts[0.75], 1.0, places=6)
        self.assertGreater(payouts[0.75], payouts[0.80])
        self.assertGreater(payouts[0.80], payouts[0.88])
        # Never worthless: a badly posed success still clearly beats no success.
        self.assertGreaterEqual(payouts[0.62], FrankaEnvParallel.SUCCESS_POSE_FLOOR)

    def test_the_hold_must_still_be_continuous(self):
        env = reward_fixture()
        need = math.ceil(env.SUCCESS_HOLD_SECONDS / env.target_period)
        self._hold_on_target(env, need - 1)
        self.assertEqual(env.last_reward_terms["success"], 0.0)
        env.ee_link.get_vel.return_value[:, 2] = 0.5  # one bad step resets the run
        env._compute_done_and_reward()
        self.assertEqual(env._success_steps, 0)


class DwellRampTest(unittest.TestCase):
    """The hold must be a ramp, not a cliff at the final step."""

    def _dwell(self, env, steps):
        env._relative_position.return_value[:, 2] = env.desired_rel_z.item()
        env._prev_cuboid_rel_z[:] = env.desired_rel_z.item()
        env._regrasp_count[:] = 1
        paid = []
        for _ in range(steps):
            env._compute_done_and_reward()
            paid.append(env.last_reward_terms["dwell_reward"].item())
        return paid

    def test_every_step_toward_the_hold_pays(self):
        env = reward_fixture()
        need = math.ceil(env.SUCCESS_HOLD_SECONDS / env.target_period)
        paid = self._dwell(env, need - 1)
        # Four qualifying steps used to pay nothing at all before the fifth.
        self.assertEqual(len(paid), need - 1)
        for amount in paid:
            self.assertAlmostEqual(amount, env.SETTLE_DWELL_REWARD, places=5)

    def test_leaving_and_re_entering_the_goal_cannot_farm_it(self):
        env = reward_fixture()
        first = sum(self._dwell(env, 3))
        env.ee_link.get_vel.return_value[:, 2] = 0.5      # break the run
        env._compute_done_and_reward()
        self.assertEqual(env._success_steps, 0)
        env.ee_link.get_vel.return_value.zero_()
        again = sum(self._dwell(env, 3))                   # same depth, no new record
        self.assertGreater(first, 0.0)
        self.assertEqual(again, 0.0)

    def test_going_deeper_than_before_pays_only_the_new_ground(self):
        env = reward_fixture()
        self._dwell(env, 2)
        env.ee_link.get_vel.return_value[:, 2] = 0.5
        env._compute_done_and_reward()
        env.ee_link.get_vel.return_value.zero_()
        paid = self._dwell(env, 4)
        self.assertEqual(paid[0], 0.0)                     # step 1, already banked
        self.assertEqual(paid[1], 0.0)                     # step 2, already banked
        self.assertAlmostEqual(paid[2], env.SETTLE_DWELL_REWARD, places=5)
        self.assertAlmostEqual(paid[3], env.SETTLE_DWELL_REWARD, places=5)


class ShapingLeakTest(unittest.TestCase):
    def test_the_shaping_leak_stays_small_against_the_task_reward(self):
        # Potential shaping bleeds weight * (1 - gamma) * phi every step. Over a
        # full episode that must not rival the reward for completing the task.
        env = reward_fixture()
        leak = env.PROGRESS_WEIGHT * (1.0 - env.REWARD_GAMMA) * env.max_episode_length
        self.assertLess(leak, 0.25 * env.SUCCESS_BONUS)


class AccelerationLimitTest(unittest.TestCase):
    def test_isolated_peak_does_not_terminate_but_sustained_one_does(self):
        env = reward_fixture()
        env._peak_acceleration[:] = FrankaEnvParallel.ACC_FAIL_LIMIT + 0.01
        for _ in range(env.ACC_VIOLATION_FAIL_STEPS - 1):
            done, _, _ = env._compute_done_and_reward()
            self.assertFalse(done.item())
            self.assertEqual(env.last_reward_terms["acceleration_violation"], 1.0)
            self.assertEqual(env.last_reward_terms["acceleration_runaway"], 0.0)
        done, _, _ = env._compute_done_and_reward()
        self.assertTrue(done.item())
        self.assertEqual(env.last_reward_terms["fail"], 1.0)

    def test_one_clean_step_clears_the_violation_streak(self):
        env = reward_fixture()
        env._peak_acceleration[:] = FrankaEnvParallel.ACC_FAIL_LIMIT + 0.01
        for _ in range(env.ACC_VIOLATION_FAIL_STEPS - 1):
            env._compute_done_and_reward()
        env._peak_acceleration[:] = 1.0
        env._compute_done_and_reward()
        self.assertEqual(env._acc_violation_steps.item(), 0)
        env._peak_acceleration[:] = FrankaEnvParallel.ACC_FAIL_LIMIT + 0.01
        self.assertFalse(env._compute_done_and_reward()[0].item())

    def test_high_step_rms_terminates_immediately(self):
        env = reward_fixture()
        env.episode_length_buf[:] = 450
        env._motion_sums["acceleration"][:] = (19.0 / env.Z_ACC_MAX) ** 2
        done, _, timeout = env._compute_done_and_reward()
        self.assertTrue(done.item())
        self.assertFalse(timeout.item())
        self.assertEqual(env.last_reward_terms["fail"], 1.0)
        self.assertAlmostEqual(env.last_reward_terms["acceleration_rms"].item(), 19.0, places=4)

    def test_success_gate_uses_rms_not_the_impulse_peak(self):
        env = reward_fixture()
        env._relative_position.return_value[:, 2] = 0.03
        confirm_regrasp(env)
        env._peak_acceleration[:] = 17.0  # a contact impulse, well above the old gate
        env._compute_done_and_reward()
        self.assertEqual(env.last_reward_terms["success_candidate"], 1.0)
        env._motion_sums["acceleration"][:] = (5.0 / env.Z_ACC_MAX) ** 2
        env._compute_done_and_reward()
        self.assertEqual(env.last_reward_terms["success_candidate"], 0.0)


class RegraspBonusTest(unittest.TestCase):
    """The one achievement bonus: paid for error a single cycle actually removed."""

    def test_one_big_slip_pays_far_more_than_many_small_ones(self):
        big = reward_fixture()
        regrasp_from(big, release_at=0.0, land_at=0.020)  # 20 mm removed at once
        single = big.last_reward_terms["regrasp_bonus"].item()

        small = reward_fixture()
        total = 0.0
        for index in range(5):
            regrasp_from(small, release_at=0.004 * index, land_at=0.004 * (index + 1),
                         calls=3 if index == 0 else 1)
            total += small.last_reward_terms["regrasp_bonus"].item()

        # Both routes remove the same 20 mm; the single cycle is worth ~3.5x.
        self.assertAlmostEqual(single, big.REGRASP_BONUS_WEIGHT, places=4)
        self.assertGreater(single, 3.0 * total)

    def test_a_cycle_that_moves_nothing_is_free(self):
        # Never a cost: after the attempt bonus anneals away an attempt must
        # still be free, or the policy simply stops opening the gripper.
        env = reward_fixture()
        env.set_reward_iteration(env.EFFORT_ANNEAL_END_ITER)
        regrasp_from(env, release_at=0.0, land_at=0.0)
        self.assertEqual(env.last_reward_terms["regrasp_bonus"], 0.0)
        self.assertEqual(env.last_reward_terms["attempt_bonus"], 0.0)

    def test_backsliding_costs_more_than_the_same_gain_pays(self):
        forward = reward_fixture()
        regrasp_from(forward, release_at=0.0, land_at=0.020)
        backward = reward_fixture()
        regrasp_from(backward, release_at=0.020, land_at=0.0)
        gained = forward.last_reward_terms["regrasp_bonus"].item()
        lost = backward.last_reward_terms["regrasp_bonus"].item()
        self.assertLess(lost, 0.0)
        self.assertAlmostEqual(lost, -forward.REGRASP_BACKSLIDE_MULTIPLIER * gained, places=4)

    def test_oscillating_to_farm_the_bonus_loses_money(self):
        env = reward_fixture()
        total = 0.0
        for index in range(4):
            release, land = (0.0, 0.020) if index % 2 == 0 else (0.020, 0.0)
            regrasp_from(env, release_at=release, land_at=land, calls=3 if index == 0 else 1)
            total += env.last_reward_terms["regrasp_bonus"].item()
        self.assertLess(total, 0.0)

    def test_only_the_first_few_cycles_are_eligible(self):
        env = reward_fixture()
        for index in range(env.REGRASP_BONUS_MAX_EVENTS + 1):
            regrasp_from(env, release_at=0.0, land_at=0.020, calls=3 if index == 0 else 1)
            paid = env.last_reward_terms["regrasp_bonus"].item()
            if index < env.REGRASP_BONUS_MAX_EVENTS:
                self.assertGreater(paid, 0.0)
            else:
                self.assertEqual(paid, 0.0)


class GripBoundTest(unittest.TestCase):
    def _penalty(self, rel_z):
        env = reward_fixture()
        env._relative_position.return_value[:, 2] = rel_z
        env._compute_done_and_reward()
        return env.last_reward_terms["grip_penalty"].item()

    def test_the_object_is_warned_before_it_slides_out(self):
        self.assertEqual(self._penalty(FrankaEnvParallel.REL_Z_FREE), 0.0)
        self.assertEqual(self._penalty(0.04), 0.0)  # inside the target range
        self.assertLess(self._penalty(0.10), 0.0)
        self.assertLess(self._penalty(0.14), self._penalty(0.10))
        self.assertAlmostEqual(self._penalty(-0.14), self._penalty(0.14), places=6)


class SettleWindowTest(unittest.TestCase):
    def _hold_armed_after_regrasp_at(self, rel_z):
        env = reward_fixture()
        env._relative_position.return_value[:, 2] = rel_z
        confirm_regrasp(env)
        return env._hold_remaining.item() > 0

    def test_settle_arms_only_near_the_target(self):
        # A chain of pulses must be free to build velocity between them.
        self.assertFalse(self._hold_armed_after_regrasp_at(0.010))  # 20 mm out
        self.assertTrue(self._hold_armed_after_regrasp_at(0.028))   # 2 mm out


class HomeHeightTest(unittest.TestCase):
    def _penalty(self, height, iteration=0):
        env = reward_fixture()
        env.set_reward_iteration(iteration)
        env.ee_link.get_pos.return_value = torch.tensor([[0.0, 0.0, height]])
        env._hold_reference_z[:] = height
        env._compute_done_and_reward()
        return env.last_reward_terms["workspace_penalty"].item()

    def test_cost_is_lowest_at_the_home_height(self):
        home = FrankaEnvParallel.EE_Z_HOME
        self.assertEqual(self._penalty(home), 0.0)
        self.assertEqual(self._penalty(home + FrankaEnvParallel.EE_Z_FREE_BAND), 0.0)
        for offset in (0.06, 0.10, 0.15):
            self.assertLess(self._penalty(home - offset), self._penalty(home - offset + 0.03))
            self.assertLess(self._penalty(home + offset), self._penalty(home + offset - 0.03))

    def test_pull_is_symmetric_about_home(self):
        home = FrankaEnvParallel.EE_Z_HOME
        self.assertAlmostEqual(self._penalty(home - 0.10), self._penalty(home + 0.10), places=6)

    def test_pull_is_a_bound_and_is_never_annealed_down(self):
        self.assertAlmostEqual(
            self._penalty(0.62, iteration=0),
            self._penalty(0.62, iteration=FrankaEnvParallel.EFFORT_ANNEAL_END_ITER), places=6,
        )


class FailCauseTest(unittest.TestCase):
    def test_each_terminal_condition_is_reported_by_name(self):
        env = reward_fixture()
        env.ee_link.get_pos.return_value = torch.tensor([[0.0, 0.0, 0.55]])
        done, _, _ = env._compute_done_and_reward()
        self.assertTrue(done.item())
        self.assertEqual(env._fail_causes["ee_low"].item(), True)
        self.assertFalse(any(env._fail_causes[name].item()
                             for name in env.FAIL_CAUSES if name != "ee_low"))

    def test_every_named_cause_exists_on_each_step(self):
        env = reward_fixture()
        env._compute_done_and_reward()
        self.assertEqual(set(env._fail_causes), set(env.FAIL_CAUSES))


class DirectionAndSpeedTest(unittest.TestCase):
    def _reward_for(self, **motion):
        env = reward_fixture()
        env.set_reward_iteration(env.EFFORT_ANNEAL_END_ITER)
        for key, value in motion.items():
            env._motion_sums[key][:] = value
        env._compute_done_and_reward()
        return env

    def test_upward_acceleration_costs_far_more_than_downward(self):
        # Same magnitude: upward also fills against_gravity, downward does not.
        cost = 0.02
        up = self._reward_for(acceleration=cost, against_gravity=cost)
        down = self._reward_for(acceleration=cost)
        self.assertEqual(down.last_reward_terms["gravity_penalty"], 0.0)
        charged = abs(up.last_reward_terms["gravity_penalty"].item())
        symmetric = abs(down.last_reward_terms["z_acc_penalty"].item())
        self.assertGreater(charged, 3.0 * symmetric)

    def test_measured_peak_speed_is_charged_above_a_free_band(self):
        env = reward_fixture()
        env._peak_speed[:] = FrankaEnvParallel.SPEED_FREE
        env._compute_done_and_reward()
        self.assertEqual(env.last_reward_terms["speed_penalty"], 0.0)

        faster = reward_fixture()
        faster._peak_speed[:] = 0.60
        faster._compute_done_and_reward()
        slower = reward_fixture()
        slower._peak_speed[:] = 0.40
        slower._compute_done_and_reward()
        self.assertLess(faster.last_reward_terms["speed_penalty"],
                        slower.last_reward_terms["speed_penalty"])
        self.assertLess(slower.last_reward_terms["speed_penalty"], 0.0)


class EffortCeilingTest(unittest.TestCase):
    def _saturated(self):
        env = reward_fixture()
        env.set_reward_iteration(env.EFFORT_ANNEAL_END_ITER)
        env._motion_sums["torque"][:] = 5.0
        env._motion_sums["acceleration"][:] = 2.0
        env._motion_sums["against_gravity"][:] = 2.0
        env._peak_speed[:] = 0.6
        env.target_z_acc[:] = 15.0
        env._compute_done_and_reward()
        return env

    def test_efficiency_costs_cannot_outweigh_the_task_reward(self):
        env = self._saturated()
        capped = ("torque", "gravity", "jerk", "speed", "vel_cmd", "centering")
        charged = sum(env._ep_term_sums[k].item() for k in capped)
        # The acceleration column also carries the uncapped excess bound.
        self.assertLess(env.last_reward_terms["effort_scale_down"], 1.0)
        self.assertGreaterEqual(charged, -env._effort_cap())
        worst_case = env._effort_cap() * env.max_episode_length
        self.assertLessEqual(worst_case, env.EFFORT_BUDGET_FRACTION * env.SUCCESS_BONUS)

    def test_the_ceiling_scales_every_term_by_the_same_factor(self):
        env = self._saturated()
        factor = env.last_reward_terms["effort_scale_down"].item()
        uncapped = reward_fixture()
        uncapped.set_reward_iteration(uncapped.EFFORT_ANNEAL_END_ITER)
        uncapped._motion_sums["torque"][:] = 5.0
        uncapped._motion_sums["acceleration"][:] = 2.0
        uncapped._motion_sums["against_gravity"][:] = 2.0
        uncapped._peak_speed[:] = 0.6
        uncapped.target_z_acc[:] = 15.0
        uncapped._effort_cap = lambda: 1e9  # lift the ceiling only
        uncapped._compute_done_and_reward()
        for key in ("torque_penalty", "gravity_penalty", "jerk_penalty", "speed_penalty"):
            self.assertAlmostEqual(
                env.last_reward_terms[key].item(),
                uncapped.last_reward_terms[key].item() * factor, places=5,
            )

    def test_the_acceleration_and_height_bounds_are_outside_the_ceiling(self):
        env = self._saturated()
        env._motion_sums["acceleration_excess"][:] = 4.0
        env.ee_link.get_pos.return_value = torch.tensor([[0.0, 0.0, 0.62]])
        env._compute_done_and_reward()
        self.assertLess(env.last_reward_terms["z_acc_penalty"].item(), -env._effort_cap())
        self.assertLess(env.last_reward_terms["workspace_penalty"].item(), 0.0)


class AnnealTest(unittest.TestCase):
    def test_attempt_bonus_is_capped_and_fades(self):
        env = reward_fixture()
        env.set_reward_iteration(0)
        for expected in (env.ATTEMPT_BONUS,) * env.ATTEMPT_BONUS_MAX_EVENTS:
            confirm_regrasp(env, calls=3 if env._regrasp_count == 0 else 1)
            self.assertAlmostEqual(env.last_reward_terms["attempt_bonus"].item(), expected, places=5)
        confirm_regrasp(env, calls=1)
        self.assertEqual(env.last_reward_terms["attempt_bonus"], 0.0)

        late = reward_fixture()
        late.set_reward_iteration(late.EFFORT_ANNEAL_END_ITER)
        confirm_regrasp(late)
        self.assertEqual(late.last_reward_terms["attempt_bonus"], 0.0)

    def test_effort_costs_ramp_in_from_the_floor(self):
        early, late = reward_fixture(), reward_fixture()
        early.set_reward_iteration(0)
        late.set_reward_iteration(late.EFFORT_ANNEAL_END_ITER)
        for env in (early, late):
            env._motion_sums["torque"][:] = 0.05  # small enough that the ceiling does not bind
            env._compute_done_and_reward()
        self.assertEqual(late.last_reward_terms["effort_scale_down"], 1.0)
        self.assertAlmostEqual(
            (early.last_reward_terms["torque_penalty"] / late.last_reward_terms["torque_penalty"]).item(),
            early.EFFORT_ANNEAL_FLOOR, places=5,
        )

    def test_acceleration_excess_cost_is_never_annealed_down(self):
        early, late = reward_fixture(), reward_fixture()
        early.set_reward_iteration(0)
        late.set_reward_iteration(late.EFFORT_ANNEAL_END_ITER)
        for env in (early, late):
            env._motion_sums["acceleration_excess"][:] = 1.0
            env._compute_done_and_reward()
        self.assertLess(early.last_reward_terms["z_acc_penalty"], 0.0)
        self.assertAlmostEqual(
            early.last_reward_terms["z_acc_penalty"].item(),
            late.last_reward_terms["z_acc_penalty"].item(), places=5,
        )

    def test_live_iteration_source_overrides_the_pinned_value(self):
        env = reward_fixture()
        env.set_reward_iteration(0)
        env.set_reward_iteration_source(lambda: env.EFFORT_ANNEAL_END_ITER)
        self.assertAlmostEqual(env._anneal_progress(), 1.0)


class HoldPenaltyTest(unittest.TestCase):
    def _hold_penalty(self, *, policy_hold, forced_hold):
        env = reward_fixture()
        env._hold_reference_z[:] = 0.8
        env._raw_velocity_action[:] = 1.0
        env._hold_remaining[:] = 1 if policy_hold else 0
        env._zero_hold_countdown[:] = 1 if forced_hold else 0
        env._compute_done_and_reward()
        return env.last_reward_terms["post_pulse_hold_penalty"].item()

    def test_forced_zero_hold_does_not_charge_the_unobservable_command(self):
        forced = self._hold_penalty(policy_hold=False, forced_hold=True)
        policy = self._hold_penalty(policy_hold=True, forced_hold=False)
        self.assertEqual(forced, 0.0)
        self.assertAlmostEqual(policy, -0.5 * 0.02, places=6)
        self.assertEqual(self._hold_penalty(policy_hold=False, forced_hold=False), 0.0)


def discounted_return(plan, iteration, horizon=450):
    """Roll a mocked episode and return PPO's objective, plus its outcome.

    The undiscounted sum is misleading here: potential shaping leaks
    -(1 - gamma) * q on every step where the object does not move, and only the
    discounted sum telescopes the way the shaping theorem intends.
    """
    env = reward_fixture()
    env.set_reward_iteration(iteration)
    total = 0.0
    for step in range(1, horizon + 1):
        env.episode_length_buf[:] = step
        plan(env, step)
        done, reward, timeout = env._compute_done_and_reward()
        total += env.REWARD_GAMMA ** (step - 1) * reward.item()
        if done.item():
            if env.last_reward_terms["success"]:
                return total, "success"
            return total, "timeout" if timeout.item() else "fail"
    return total, "unfinished"


def _freeze(env, step):
    pass


def _drop(env, step):
    if step >= 5:
        env._relative_position.return_value[:, 2] = -0.2


def _attempt_without_gain(env, step):
    if step % 20 == 0 and env._regrasp_count.item() < 4:
        env._in_release[:] = True


def _harvest(env, step):
    # Three quick cycles that each close a third of the error, then ride it out.
    if step % 20 == 0 and env._regrasp_count.item() < 3:
        env._in_release[:] = True
        current = env._relative_position.return_value[0, 2].item()
        env._relative_position.return_value[:, 2] = current + (0.03 - current) / 3.0


def _harvest_then_crash(env, step):
    _harvest(env, step)
    if step >= 65:
        env._relative_position.return_value[:, 2] = -0.2


def _converge(env, step):
    # One release/regrasp per 20 steps, each closing a third of the error.
    if step % 20 == 0 and env._regrasp_count.item() < 6:
        env._in_release[:] = True
        current = env._relative_position.return_value[0, 2].item()
        env._relative_position.return_value[:, 2] = current + (0.03 - current) / 3.0
    if env._regrasp_count.item() >= 6:
        env._relative_position.return_value[:, 2] = 0.03


class IncentiveOrderingTest(unittest.TestCase):
    """Whole-episode returns must rank the behaviors the way training needs.

    This is the property reward v2 lacked: freezing was optimal, so PPO never
    explored a regrasp. Any future weight edit that restores that ordering, or
    that makes dropping the object attractive, fails here.
    """

    def _returns(self, iteration):
        return {
            name: discounted_return(plan, iteration)
            for name, plan in (
                ("converge", _converge), ("attempt", _attempt_without_gain),
                ("freeze", _freeze), ("drop", _drop),
            )
        }

    def test_early_training_rewards_exploring_over_standing_still(self):
        results = self._returns(0)
        self.assertEqual(results["converge"][1], "success")
        self.assertGreater(results["converge"][0], results["attempt"][0])
        self.assertGreater(results["attempt"][0], results["freeze"][0])
        self.assertGreater(results["freeze"][0], results["drop"][0])

    def test_banking_bonuses_and_crashing_is_not_profitable(self):
        # The behavior a 314-iteration run actually learned: spam pulses, collect
        # the event bonuses, then let the episode fail.
        kept, kept_outcome = discounted_return(_harvest, 0)
        crashed, crashed_outcome = discounted_return(_harvest_then_crash, 0)
        self.assertEqual(kept_outcome, "timeout")
        self.assertEqual(crashed_outcome, "fail")
        self.assertGreater(kept, crashed)

    def test_annealed_reward_still_ranks_solving_first(self):
        results = self._returns(FrankaEnvParallel.EFFORT_ANNEAL_END_ITER)
        self.assertEqual(results["converge"][1], "success")
        self.assertGreater(results["converge"][0], results["freeze"][0])
        self.assertGreater(results["freeze"][0], results["drop"][0])
        # The attempt bonus is gone, but a cycle that gains nothing must stay
        # free rather than punished, or the policy stops opening the gripper.
        self.assertAlmostEqual(results["attempt"][0], results["freeze"][0], places=6)


class AccountingTest(unittest.TestCase):
    def test_effort_always_lowers_reward_and_terms_sum(self):
        low = reward_fixture()
        high = reward_fixture()
        high._motion_sums["torque"][:] = 0.5
        _, low_reward, _ = low._compute_done_and_reward()
        _, high_reward, _ = high._compute_done_and_reward()
        self.assertGreater(low_reward, high_reward)
        self.assertTrue(torch.allclose(high_reward, sum(v for k, v in high._ep_term_sums.items() if k != "total")))

    def test_potential_shaping_cannot_reward_a_closed_cycle(self):
        env = reward_fixture()
        initial = env._previous_potential.clone()
        env._relative_position.return_value[:, 2] = 0.02
        env._compute_done_and_reward()
        first = env.last_reward_terms["progress_reward"]
        env._relative_position.return_value[:, 2] = 0.0
        env._compute_done_and_reward()
        total = first + env.REWARD_GAMMA * env.last_reward_terms["progress_reward"]
        expected = env.PROGRESS_WEIGHT * (env.REWARD_GAMMA**2 - 1.0) * initial
        self.assertTrue(torch.allclose(total, expected, atol=1e-6))
        self.assertLess(total, 0.0)

    def test_limit_regrasp_is_optional(self):
        env = reward_fixture()
        env._regrasp_count[:] = 7
        self.assertFalse(env._compute_done_and_reward()[0].item())
        env.limit_regrasp = True
        self.assertTrue(env._compute_done_and_reward()[0].item())


if __name__ == "__main__":
    unittest.main()

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
                original._update_obs_buf()
                tilted._update_obs_buf()
                torch.testing.assert_close(tilted.obs_buf, original.obs_buf)
                # done and timeout must match exactly; rew_buf deliberately does not,
                # because the tilted env sums a different set of terms.
                actual_done, _, actual_timeout = tilted._compute_done_and_reward()
                expected_done, _, expected_timeout = original._compute_done_and_reward()
                torch.testing.assert_close(actual_done, expected_done)
                torch.testing.assert_close(actual_timeout, expected_timeout)
                # Terms both envs still publish must agree: same formula, rotated frame.
                shared = tilted.last_reward_terms.keys() & original.last_reward_terms.keys()
                self.assertIn("regrasp_bonus", shared)
                self.assertIn("z_acc_penalty", shared)
                for name in shared:
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

    def test_proximity_reward_is_bounded_positive_and_progressive(self):
        env = self.make_env(TiltedEnv)
        env.desired_rel_z = torch.zeros(4)
        gaps = torch.tensor([0.0, 0.005, 0.02, TiltedEnv.PROXIMITY_REWARD_RANGE])

        def reward_for(gap):
            env.desired_rel_z = torch.zeros(4)
            env.cuboid = SimpleNamespace(get_pos=lambda: env.ee_link.get_pos())
            k = TiltedEnv.PROXIMITY_REWARD_SHARPNESS
            floor = math.exp(-k)
            decay = torch.exp(-k * gap.abs() / TiltedEnv.PROXIMITY_REWARD_RANGE)
            r = TiltedEnv.PROXIMITY_REWARD_MAX * (decay - floor) / (1.0 - floor)
            return r.clamp(0.0, TiltedEnv.PROXIMITY_REWARD_MAX)

        values = reward_for(gaps)
        # Exact endpoints: max at zero gap, exactly zero at the range limit.
        self.assertAlmostEqual(float(values[0]), TiltedEnv.PROXIMITY_REWARD_MAX, places=4)
        self.assertAlmostEqual(float(values[-1]), 0.0, places=4)
        # Strictly decreasing, never negative, never above the cap.
        self.assertTrue(torch.all(values[1:] < values[:-1]))
        self.assertTrue(torch.all(values >= 0.0))
        self.assertTrue(torch.all(values <= TiltedEnv.PROXIMITY_REWARD_MAX))
        # Beyond the range the clamp holds it at zero rather than going negative.
        beyond = reward_for(torch.tensor([0.06, 0.1, 0.5, 1.0]))
        torch.testing.assert_close(beyond, torch.zeros(4))
        # Exponential, not linear: a convex decay sits below the straight line from
        # MAX at d=0 to 0 at d=RANGE, so real reward is only earned close in.
        probes = torch.tensor([0.0125, 0.025, 0.0375])
        linear = TiltedEnv.PROXIMITY_REWARD_MAX * (1.0 - probes / TiltedEnv.PROXIMITY_REWARD_RANGE)
        self.assertTrue(torch.all(reward_for(probes) < linear))
        # Halving the gap must more than double the reward near the target.
        close, twice = reward_for(torch.tensor([0.005, 0.01]))
        self.assertGreater(float(close), float(twice))

    def test_reward_sums_only_the_weighted_terms(self):
        env = self.make_env(TiltedEnv)
        _, reward, _ = env._compute_done_and_reward()
        weights = TiltedEnv.REWARD_TERM_WEIGHTS
        self.assertEqual(set(weights), {"base_reward", "proximity_reward", "regrasp_bonus", "z_acc_penalty"})
        expected = sum(weights[k] * env.last_reward_terms[k] for k in weights)
        torch.testing.assert_close(reward, expected)
        # Dropped terms must not linger in the table, or its total would double-count.
        for gone in ("jerk_penalty", "vel_sign_flip_penalty", "post_pulse_hold_penalty"):
            self.assertNotIn(gone, env.last_reward_terms)

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


if __name__ == "__main__":
    unittest.main()

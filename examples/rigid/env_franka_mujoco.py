import argparse
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
XML_DIR = REPO_ROOT / "genesis/assets/xml/franka_emika_panda"


@dataclass
class FrankaControlRecord:
    sim_step: int
    time: float
    target_pos: np.ndarray
    target_vel: np.ndarray
    target_z_acc: float
    qvel: np.ndarray
    actual_pos: np.ndarray
    actual_vel: np.ndarray
    tau: np.ndarray


class FrankaMuJoCoEnv:
    """MuJoCo version of examples.rigid.env_franka.FrankaEnv.

    The source Genesis environment loads the Panda and cuboid as separate MJCF entities. MuJoCo expects one compiled
    model, so this class composes those existing MJCF files in memory and leaves the XML files untouched.
    """

    OBS_DIM = 10
    OBS_SCALE = np.array([1.0, 0.6, 0.6, 15.0, 5.0, 5.0, 0.05, 0.05, 0.05, 0.05], dtype=np.float32)

    Z_VEL_MAX = 0.6
    Z_ACC_MAX = 15.00
    Z_ACC_PENALTY_THRESHOLD = 13.0
    Z_ACC_PENALTY_WEIGHT = 30.0
    PULSE_DELAY_STEPS = 1   # target-period steps to wait before the open window begins
    EE_Z_TARGET = 0.7
    GRIPPER_CLOSED = 0.000251
    GRIPPER_OPEN = 0.0124
    TRACKING_PERFECT = 20.0 * math.exp(-30.0 * 0.001)
    MAX_EPISODE_LENGTH = 450

    REGRASP_FORCE_THRESHOLD = 0.75
    REGRASP_BONUS_PER_STEP = 10.0

    def __init__(
        self,
        *,
        vis=False,
        dt=0.001,
        target_dt=0.02,
        render_fps=60,
        playback_speed=1.0,
        gripper_pos_min=0.00000000251,
        gripper_pos_max=0.0124,
        solid_up: bool = False,
        gravity_compensation: bool = True,
        debug: bool = False,
        normalize: bool = False,
    ):
        if playback_speed <= 0.0:
            raise ValueError("playback_speed must be greater than 0")

        self.vis = vis
        self.dt = dt
        self.target_dt = target_dt
        self.target_update_every = max(1, int(round(target_dt / dt)))
        self.target_period = self.target_update_every * dt
        self.render_fps = render_fps
        self.render_every = max(1, int(round(1.0 / (render_fps * dt))))
        self.playback_speed = playback_speed
        self.action_dim = 3
        self.z_vel_max = 0.7
        self.solid_up = solid_up
        self.gravity_compensation = gravity_compensation
        self.debug = debug
        self.normalize = normalize

        self.gripper_pos_min = np.broadcast_to(np.asarray(gripper_pos_min, dtype=float), (2,)).copy()
        self.gripper_pos_max = np.broadcast_to(np.asarray(gripper_pos_max, dtype=float), (2,)).copy()
        self.closed_gripper_pos = np.array([-1.0, -1.0])
        self.open_gripper_pos = np.array([1.0, 1.0])

        self.model = _build_franka_lift_model(dt=dt)
        self.data = mujoco.MjData(self.model)
        self.viewer = self._launch_viewer() if vis else None

        self.motors_dof = np.arange(7)
        self.fingers_dof = np.arange(7, 9)
        self.arm_actuators = np.arange(7)
        self.finger_actuators = np.arange(7, 9)
        self.q_home = np.array([0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78, 0.008090, 0.00890])
        self.fingertip_local_pos = np.array([0.0, 0.0055, 0.0445])

        self.hand_body_id = _body_id(self.model, "hand")
        self.left_finger_body_id = _body_id(self.model, "left_finger")
        self.right_finger_body_id = _body_id(self.model, "right_finger")
        self.cuboid_body_id = _body_id(self.model, "cuboid")
        self.cuboid_joint_id = _joint_id(self.model, "cuboid_freejoint")
        self.cuboid_qposadr = int(self.model.jnt_qposadr[self.cuboid_joint_id])
        self.cuboid_dofadr = int(self.model.jnt_dofadr[self.cuboid_joint_id])
        self.left_finger_geoms = _geoms_for_body(self.model, self.left_finger_body_id)
        self.right_finger_geoms = _geoms_for_body(self.model, self.right_finger_body_id)

        self.last_done_reason = None
        self.last_reward_terms = {}
        self.reset()

    def reset(self, warmup_steps=100):
        mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[:9] = self.q_home
        self.data.qvel[:] = 0.0
        self.data.ctrl[:7] = self.q_home[:7]
        self.data.ctrl[7:9] = self.q_home[7:9]
        mujoco.mj_forward(self.model, self.data)
        self._reset_cuboid_home_pose()

        self.target_center = self._body_pos(self.hand_body_id)
        self.target_quat = self._body_quat(self.hand_body_id)
        self.target_z = float(self.target_center[2])
        self.target_z_vel = 0.0
        self.target_z_acc = 0.0
        self.prev_target_z_vel = 0.0

        pos_gain = 8.0
        rot_gain = 4.0
        damping = 1e-4
        jacobian_regularizer = damping * np.eye(6)
        self.controller = MujocoFrankaVelocityController(
            model=self.model,
            data=self.data,
            env=self,
            hand_body_id=self.hand_body_id,
            motors_dof=self.motors_dof,
            arm_actuators=self.arm_actuators,
            target_center=self.target_center,
            target_quat=self.target_quat,
            pos_gain=pos_gain,
            rot_gain=rot_gain,
            jacobian_regularizer=jacobian_regularizer,
            dt=self.dt,
            target_period=self.target_period,
            steps_per_target=self.target_update_every,
            render_every=self.render_every,
            finger_actuators=self.finger_actuators,
            playback_speed=self.playback_speed if self.vis else None,
        )
        self.controller.reset((self.target_z, self.target_z_vel, self.target_z_acc))

        self.sim_step = 0
        self.episode_step = 0
        mag = 0.02 + np.random.uniform() * 0.02
        self.desired_rel_z = -mag if self.solid_up else mag
        self.prev_actual_z_vel = None
        self.prev_torque = None
        self.direction_change_count = 0
        self._in_release = False
        self._release_step_count = 0
        self._regrasp_count = 0
        self._prev_gripper_avg = -1.0
        self._gripper_pulse_steps = 0

        for i in range(warmup_steps):
            self.data.ctrl[:7] = self.q_home[:7]
            self.data.ctrl[7:9] = self.q_home[7:9]
            self._apply_arm_gravity_compensation()
            mujoco.mj_step(self.model, self.data)
            self._sync_viewer(i)

        self.controller.reset_playback_clock()
        self._store_initial_z_error()
        return self.get_observation()

    def step(self, action):
        action = np.asarray(action, dtype=float).reshape(-1)
        if action.shape != (self.action_dim,):
            raise ValueError(f"action must have shape ({self.action_dim},), got {action.shape}")

        target_z_vel = float(np.clip(action[0], -1.0, 1.0)) * self.z_vel_max
        max_dv = self.Z_ACC_MAX * self.target_period
        target_z_vel = float(np.clip(target_z_vel, self.target_z_vel - max_dv, self.target_z_vel + max_dv))

        gripper_raw = np.clip(action[1:], -1.0, 1.0)
        gripper_pos = self.gripper_pos_min + (gripper_raw + 1.0) * 0.5 * (
            self.gripper_pos_max - self.gripper_pos_min
        )

        # --- GRIPPER PULSE LOGIC ---
        _pulse_start = 6 + self.PULSE_DELAY_STEPS
        _gp_mid = (np.mean(self.gripper_pos_min) + np.mean(self.gripper_pos_max)) * 0.5
        _gp_avg = np.mean(gripper_pos)
        _rising = (
            (self._prev_gripper_avg < _gp_mid)
            and (_gp_avg >= _gp_mid)
            and (self._gripper_pulse_steps == 0)
        )
        if _rising:
            self._gripper_pulse_steps = _pulse_start
            
        if 2 <= self._gripper_pulse_steps <= 6:
            gripper_pos = self.gripper_pos_max.copy()
        elif self._gripper_pulse_steps == 1:
            gripper_pos = self.gripper_pos_min.copy()
            
        self._prev_gripper_avg = _gp_avg
        if self._gripper_pulse_steps > 0:
            self._gripper_pulse_steps -= 1
        # ---------------------------

        target_z_acc = (target_z_vel - self.target_z_vel) / self.target_period
        target_z = self.target_z + 0.5 * (self.target_z_vel + target_z_vel) * self.target_period

        self.prev_target_z_vel = self.target_z_vel
        self.target_z = float(target_z)
        self.target_z_vel = target_z_vel
        self.target_z_acc = target_z_acc

        records = self.controller.step(
            target_z=self.target_z,
            target_z_vel=self.target_z_vel,
            target_z_acc=self.target_z_acc,
            start_sim_step=self.sim_step,
            gripper_pos=gripper_pos,
        )
        self.sim_step += len(records)
        self.episode_step += 1

        done, reward = self._compute_done_and_reward()
        if done:
            self.reset(warmup_steps=0)
        return records, reward, done

    def get_observation(self):
        left_force, right_force = self.get_finger_net_contact_forces()
        ee_pos = self._body_pos(self.hand_body_id)
        cuboid_pos = self._body_pos(self.cuboid_body_id)
        left_ft = self.get_fingertip_pos(self.left_finger_body_id)
        right_ft = self.get_fingertip_pos(self.right_finger_body_id)
        finger_mid = (left_ft + right_ft) / 2.0
        return {
            "ee_pos": float(ee_pos[2]),
            "ee_vel": float(self._body_linear_vel(self.hand_body_id)[2]),
            "target_z_vel": self.target_z_vel,
            "target_z_acc": self.target_z_acc,
            "left_force_mag": float(np.linalg.norm(left_force)),
            "right_force_mag": float(np.linalg.norm(right_force)),
            "cuboid_rel_z": float(cuboid_pos[2] - finger_mid[2]),
            "cuboid_rel_x": float(cuboid_pos[0] - finger_mid[0]),
            "cuboid_rel_y": float(cuboid_pos[1] - finger_mid[1]),
            "desired_rel_z": self.desired_rel_z,
        }

    def get_obs_flat(self) -> np.ndarray:
        left_force, right_force = self.get_finger_net_contact_forces()
        ee_pos = self._body_pos(self.hand_body_id)
        ee_vel_z = float(self._body_linear_vel(self.hand_body_id)[2])
        cuboid_pos = self._body_pos(self.cuboid_body_id)
        left_ft = self.get_fingertip_pos(self.left_finger_body_id)
        right_ft = self.get_fingertip_pos(self.right_finger_body_id)
        finger_mid = (left_ft + right_ft) / 2.0
        ft_dist = float(np.linalg.norm(left_ft - right_ft))

        left_force_mag = float(np.linalg.norm(left_force))
        right_force_mag = float(np.linalg.norm(right_force))

        obs_array = np.array(
            [
                ee_pos[2],
                ee_vel_z,
                self.target_z_vel,
                self.target_z_acc,
                left_force_mag,
                right_force_mag,
                cuboid_pos[2] - finger_mid[2],
                cuboid_pos[0] - finger_mid[0],
                cuboid_pos[1] - finger_mid[1],
                self.desired_rel_z,
            ],
            dtype=np.float32,
        )
        if self.normalize:
            obs_array = obs_array / self.OBS_SCALE
        if self.episode_step == 0:
            print(f"DEBUG OBS at step 0: {obs_array.tolist()}")
        return obs_array

    def get_fingertip_pos(self, finger_body_id):
        return self._body_pos(finger_body_id) + _rotate_by_quat(
            self.fingertip_local_pos,
            self._body_quat(finger_body_id),
        )

    def get_fingertip_distance(self):
        left_fingertip_pos = self.get_fingertip_pos(self.left_finger_body_id)
        right_fingertip_pos = self.get_fingertip_pos(self.right_finger_body_id)
        return np.linalg.norm(left_fingertip_pos - right_fingertip_pos)

    def get_finger_net_contact_forces(self):
        left_force = np.zeros(3)
        right_force = np.zeros(3)
        contact_force = np.zeros(6)

        for i in range(self.data.ncon):
            contact = self.data.contact[i]
            geom1 = int(contact.geom1)
            geom2 = int(contact.geom2)
            if geom1 < 0 or geom2 < 0:
                continue

            body1 = self.model.geom_bodyid[geom1]
            body2 = self.model.geom_bodyid[geom2]
            name1 = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY, body1)
            name2 = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY, body2)

            # if geom1 in self.left_finger_geoms or geom2 in self.left_finger_geoms or geom1 in self.right_finger_geoms or geom2 in self.right_finger_geoms:
            #     print(f"DEBUG CONTACT: {name1} collided with {name2}")

            if (body1 != 0 and body1 != self.cuboid_body_id) and (body2 != 0 and body2 != self.cuboid_body_id):
                continue

            mujoco.mj_contactForce(self.model, self.data, i, contact_force)
            frame = np.asarray(contact.frame).reshape(3, 3)
            world_force = frame.T @ contact_force[:3]

            if geom1 in self.left_finger_geoms:
                left_force += world_force
            elif geom2 in self.left_finger_geoms:
                left_force -= world_force

            if geom1 in self.right_finger_geoms:
                right_force += world_force
            elif geom2 in self.right_finger_geoms:
                right_force -= world_force

        return left_force, right_force

    def print_finger_status(self, label):
        fingertip_distance = self.get_fingertip_distance()
        left_force, right_force = self.get_finger_net_contact_forces()
        print(f"{label}: fingertip distance = {fingertip_distance:.6f} m")
        print("left finger force:", left_force, "norm:", np.linalg.norm(left_force))
        print("right finger force:", right_force, "norm:", np.linalg.norm(right_force))
        cuboid_pos = self._body_pos(self.cuboid_body_id)
        finger_mid = (
            self.get_fingertip_pos(self.left_finger_body_id) + self.get_fingertip_pos(self.right_finger_body_id)
        ) / 2.0
        print("relative z pos of cuboid and finger_mid:", cuboid_pos[2] - finger_mid[2])
        print("relative x pos of cuboid and finger_mid:", cuboid_pos[0] - finger_mid[0])
        print("relative y pos of cuboid and finger_mid:", cuboid_pos[1] - finger_mid[1])

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None

    def _compute_done_and_reward(self) -> tuple[bool, float]:
        cuboid_pos = self._body_pos(self.cuboid_body_id)
        ee_pos = self._body_pos(self.hand_body_id)
        ee_vel = self._body_linear_vel(self.hand_body_id)
        left_ft = self.get_fingertip_pos(self.left_finger_body_id)
        right_ft = self.get_fingertip_pos(self.right_finger_body_id)
        finger_mid = (left_ft + right_ft) / 2.0
        cuboid_rel_z = float(cuboid_pos[2] - finger_mid[2])
        cuboid_rel_x = float(cuboid_pos[0] - finger_mid[0])
        cuboid_rel_y = float(cuboid_pos[1] - finger_mid[1])
        fingertip_dist = float(np.linalg.norm(left_ft - right_ft))
        ee_z = float(ee_pos[2])
        ee_vel_z = float(ee_vel[2])

        timeout = self.episode_step >= self.MAX_EPISODE_LENGTH
        success = (not timeout) and (abs(cuboid_rel_z - self.desired_rel_z) <= 0.01 and abs(ee_vel_z) < 0.04)
        fail = (
            abs(cuboid_rel_x) > 0.015
            or abs(cuboid_rel_y) > 0.015
            or fingertip_dist < 0.02
            or abs(cuboid_rel_z) > 0.15
            or ee_z < 0.6
            or ee_z > 0.96
        )

        if self.debug:
            print(
                " why failed: "
                f"cuboid_rel_x={cuboid_rel_x:+.4f}  "
                f"cuboid_rel_y={cuboid_rel_y:+.4f}  "
                f"fingertip_dist={fingertip_dist:.4f}  "
                f"cuboid_rel_z={cuboid_rel_z:+.4f}  "
                f"ee_z={ee_z:.4f}  "
                f"ee_vel_z={ee_vel_z:+.4f}  "
                f"timeout={float(timeout):.2f}  "
                f"success={float(success):.2f}  "
                f"fail={float(fail):.2f}"
            )

        fail_reasons = []
        if abs(cuboid_rel_x) > 0.015:
            fail_reasons.append(f"cuboid_rel_x={cuboid_rel_x:+.4f}")
        if abs(cuboid_rel_y) > 0.015:
            fail_reasons.append(f"cuboid_rel_y={cuboid_rel_y:+.4f}")
        if fingertip_dist < 0.02:
            fail_reasons.append(f"fingertip_dist={fingertip_dist:.4f}")
        if abs(cuboid_rel_z) > 0.15:
            fail_reasons.append(f"cuboid_rel_z={cuboid_rel_z:+.4f}")
        if ee_z < 0.76:
            fail_reasons.append(f"ee_z_low={ee_z:.4f}")
        if ee_z > 0.96:
            fail_reasons.append(f"ee_z_high={ee_z:.4f}")

        if success:
            self.last_done_reason = "success"
        elif fail_reasons:
            self.last_done_reason = ", ".join(fail_reasons)
        elif timeout:
            self.last_done_reason = "timeout"
        else:
            self.last_done_reason = None

        z_error = abs(cuboid_rel_z - self.desired_rel_z)
        tracking_raw = 20.0 * math.exp(-30.0 * z_error)
        denom = max(self.TRACKING_PERFECT - self.initial_tracking, 1e-6)
        tracking = max(0.0, min(1.0, (tracking_raw - self.initial_tracking) / denom))
        if self.debug:
            print(f"z_error={z_error:.4f}  tracking={tracking:.2f}")

        jerk = (self.target_z_vel - self.prev_target_z_vel) / self.Z_VEL_MAX
        jerk_penalty = -0.2 * jerk**2

        commanded_z_acc = (self.target_z_vel - self.prev_target_z_vel) / self.target_period
        z_acc_excess = max(0.0, abs(commanded_z_acc) - self.Z_ACC_PENALTY_THRESHOLD)
        z_acc_penalty = -self.Z_ACC_PENALTY_WEIGHT * z_acc_excess / 8.0

        grip = math.exp(-200.0 * max(0.0, fingertip_dist - 0.03))

        left_force, right_force = self.get_finger_net_contact_forces()
        avg_finger_force = (np.linalg.norm(left_force) + np.linalg.norm(right_force)) * 0.5
        currently_released = avg_finger_force < self.REGRASP_FORCE_THRESHOLD
        if self._in_release:
            self._release_step_count += 1
        exiting_release = self._in_release and not currently_released
        if exiting_release and self._regrasp_count < 2:
            regrasp_bonus = self._release_step_count * self.REGRASP_BONUS_PER_STEP * 2.0
        else:
            regrasp_bonus = 0.0
        if exiting_release:
            self._regrasp_count += 1
            self._release_step_count = 0
        self._in_release = currently_released

        ep = float(self.episode_step)
        if success:
            base_reward = 500.0 - ep * 0.1
        elif fail or timeout:
            base_reward = -250.0
        else:
            base_reward = -0.00075

        ee_z_penalty = -0.35 * max(ee_z - 0.86, 0.0)
        self.last_reward_terms = {
            "tracking": 3.0 * tracking,
            "jerk_penalty": jerk_penalty,
            "z_acc_penalty": z_acc_penalty,
            "grip": 0.5 * grip,
            "ee_z_penalty": ee_z_penalty,
            "regrasp_bonus": regrasp_bonus,
        }

        reward = (
            base_reward
            + 3.0 * tracking
            + jerk_penalty
            + z_acc_penalty
            + 0.5 * grip
            + ee_z_penalty
            + regrasp_bonus
        )
        return timeout or success or fail, reward

    def _store_initial_z_error(self):
        cuboid_pos = self._body_pos(self.cuboid_body_id)
        left_ft = self.get_fingertip_pos(self.left_finger_body_id)
        right_ft = self.get_fingertip_pos(self.right_finger_body_id)
        finger_mid = (left_ft + right_ft) / 2.0
        err = abs(cuboid_pos[2] - finger_mid[2] - self.desired_rel_z)
        self.initial_z_error = max(err, 1e-3)
        self.initial_tracking = 20.0 * math.exp(-30.0 * self.initial_z_error)

    def _reset_cuboid_home_pose(self):
        hand_pos = self._body_pos(self.hand_body_id)
        hand_quat = self._body_quat(self.hand_body_id)
        local_offset = np.array([0.0, 0.0, 0.1029])
        local_quat = np.array([0.00187891, -0.71790805, -0.00193768, -0.69613270])
        local_quat /= np.linalg.norm(local_quat)
        cuboid_home_pos = hand_pos + _rotate_by_quat(local_offset, hand_quat)
        cuboid_home_quat = _quat_mul(hand_quat, local_quat)

        self.data.qpos[self.cuboid_qposadr : self.cuboid_qposadr + 3] = cuboid_home_pos
        self.data.qpos[self.cuboid_qposadr + 3 : self.cuboid_qposadr + 7] = cuboid_home_quat
        self.data.qvel[self.cuboid_dofadr : self.cuboid_dofadr + 6] = 0.0
        mujoco.mj_forward(self.model, self.data)

    def _apply_arm_gravity_compensation(self):
        self.data.qfrc_applied[:] = 0.0
        if self.gravity_compensation:
            self.data.qfrc_applied[self.motors_dof] = self.data.qfrc_bias[self.motors_dof]

    def _body_pos(self, body_id):
        return self.data.xpos[body_id].copy()

    def _body_quat(self, body_id):
        return self.data.xquat[body_id].copy()

    def _body_linear_vel(self, body_id):
        jacp = np.zeros((3, self.model.nv))
        jacr = np.zeros((3, self.model.nv))
        mujoco.mj_jacBody(self.model, self.data, jacp, jacr, body_id)
        return jacp @ self.data.qvel

    def _sync_viewer(self, sim_step):
        if self.viewer is not None and sim_step % self.render_every == 0:
            self.viewer.sync()

    def _launch_viewer(self):
        try:
            import mujoco.viewer
        except ImportError as exc:
            raise RuntimeError("mujoco.viewer is not available in this environment") from exc
        return mujoco.viewer.launch_passive(self.model, self.data)


class MujocoFrankaVelocityController:
    def __init__(
        self,
        model,
        data,
        env,
        hand_body_id,
        motors_dof,
        arm_actuators,
        target_center,
        target_quat,
        pos_gain,
        rot_gain,
        jacobian_regularizer,
        dt,
        target_period,
        steps_per_target,
        render_every,
        finger_actuators=None,
        playback_speed=None,
    ):
        self.model = model
        self.data = data
        self.env = env
        self.hand_body_id = hand_body_id
        self.motors_dof = motors_dof
        self.arm_actuators = arm_actuators
        self.finger_actuators = finger_actuators
        self.arm_qpos_idx = np.asarray(motors_dof, dtype=int)
        self.target_center = target_center
        self.target_quat = target_quat
        self.pos_gain = pos_gain
        self.rot_gain = rot_gain
        self.jacobian_regularizer = jacobian_regularizer
        self.dt = dt
        self.target_period = target_period
        self.steps_per_target = steps_per_target
        self.render_every = render_every
        self.playback_speed = playback_speed

        self._last_high_level_sample = None
        self._segment_start_sample = None
        self._segment_end_sample = None
        self._segment_playback_start_t = 0.0
        self._playback_wall_start = time.perf_counter()

    def reset(self, initial_sample=None):
        self._last_high_level_sample = initial_sample
        self._segment_start_sample = None
        self._segment_end_sample = None
        self._segment_playback_start_t = 0.0

    def reset_playback_clock(self):
        self._playback_wall_start = time.perf_counter()

    def step(self, target_z, target_z_vel, target_z_acc, start_sim_step, gripper_pos=None):
        high_level_sample = (target_z, target_z_vel, target_z_acc)
        self._update_target_segment(high_level_sample, start_sim_step * self.dt)
        self.control_gripper(gripper_pos)

        records = []
        for local_step in range(self.steps_per_target):
            sim_step = start_sim_step + local_step
            t = sim_step * self.dt
            target_z, target_z_vel, target_z_acc = self._sample_target(t)
            target_pos, target_vel, qvel = self._control_once(target_z, target_z_vel)

            update_visualizer = sim_step % self.render_every == 0
            self._pace_visualizer(update_visualizer, sim_step)
            self.env._apply_arm_gravity_compensation()
            mujoco.mj_step(self.model, self.data)
            if update_visualizer:
                self.env._sync_viewer(sim_step)

            records.append(
                FrankaControlRecord(
                    sim_step=sim_step,
                    time=(sim_step + 1) * self.dt,
                    target_pos=target_pos,
                    target_vel=target_vel,
                    target_z_acc=target_z_acc,
                    qvel=qvel,
                    actual_pos=self.env._body_pos(self.hand_body_id),
                    actual_vel=self.env._body_linear_vel(self.hand_body_id),
                    tau=self.data.actuator_force[self.arm_actuators].copy(),
                )
            )

        return records

    def control_gripper(self, gripper_pos):
        if gripper_pos is None:
            return
        if self.finger_actuators is None:
            raise ValueError("finger_actuators must be provided to control the gripper")

        gripper_pos = np.asarray(gripper_pos, dtype=float).reshape(-1)
        if gripper_pos.shape != (2,):
            raise ValueError(f"gripper_pos must have shape (2,), got {gripper_pos.shape}")
        self.data.ctrl[self.finger_actuators] = gripper_pos

    def _update_target_segment(self, high_level_sample, playback_start_t):
        if self._last_high_level_sample is None:
            self._last_high_level_sample = high_level_sample
            return

        self._segment_start_sample = self._last_high_level_sample
        self._segment_end_sample = high_level_sample
        self._segment_playback_start_t = playback_start_t
        self._last_high_level_sample = high_level_sample

    def _sample_target(self, t):
        if self._segment_end_sample is None:
            return self._last_high_level_sample

        return cubic_hermite_z_reference(
            local_t=t - self._segment_playback_start_t,
            segment_dt=self.target_period,
            start_sample=self._segment_start_sample,
            end_sample=self._segment_end_sample,
        )

    def _control_once(self, target_z, target_z_vel):
        target_pos = self.target_center.copy()
        target_vel = np.zeros(3)
        target_pos[2] = target_z
        target_vel[2] = target_z_vel

        ee_velocity_cmd, jacobian = compute_ee_velocity_command_and_jacobian(
            model=self.model,
            data=self.data,
            hand_body_id=self.hand_body_id,
            motors_dof=self.motors_dof,
            target_pos=target_pos,
            target_vel=target_vel,
            target_quat=self.target_quat,
            pos_gain=self.pos_gain,
            rot_gain=self.rot_gain,
        )
        qvel = jacobian.T @ np.linalg.solve(
            jacobian @ jacobian.T + self.jacobian_regularizer,
            ee_velocity_cmd,
        )
        ctrl_min = self.model.actuator_ctrlrange[self.arm_actuators, 0]
        ctrl_max = self.model.actuator_ctrlrange[self.arm_actuators, 1]
        qpos = self.data.qpos[self.arm_qpos_idx]
        
        # In the XML, Kv/Kp = 0.1 for all arm actuators. 
        # To achieve force = Kv * (qvel_desired - qvel), we need:
        # Kp * (ctrl - qpos) - Kv * qvel = Kv * qvel_desired - Kv * qvel
        # Kp * (ctrl - qpos) = Kv * qvel_desired
        # ctrl = qpos + (Kv/Kp) * qvel_desired
        kv_over_kp = 0.1
        qpos_target = qpos + qvel * kv_over_kp
        self.data.ctrl[self.arm_actuators] = np.clip(qpos_target, ctrl_min, ctrl_max)

        return target_pos, target_vel, qvel

    def _pace_visualizer(self, update_visualizer, sim_step):
        if self.playback_speed is None or not update_visualizer:
            return

        t_playback = ((sim_step + 1) * self.dt) / self.playback_speed
        t_wall = time.perf_counter() - self._playback_wall_start
        if t_wall < t_playback:
            time.sleep(t_playback - t_wall)


def cubic_hermite_z_reference(local_t, segment_dt, start_sample, end_sample):
    z0, z_vel0, _ = start_sample
    z1, z_vel1, _ = end_sample
    s = np.clip(local_t / segment_dt, 0.0, 1.0)

    s2 = s * s
    s3 = s2 * s

    h00 = 2.0 * s3 - 3.0 * s2 + 1.0
    h10 = s3 - 2.0 * s2 + s
    h01 = -2.0 * s3 + 3.0 * s2
    h11 = s3 - s2
    z = h00 * z0 + h10 * segment_dt * z_vel0 + h01 * z1 + h11 * segment_dt * z_vel1

    dh00 = 6.0 * s2 - 6.0 * s
    dh10 = 3.0 * s2 - 4.0 * s + 1.0
    dh01 = -6.0 * s2 + 6.0 * s
    dh11 = 3.0 * s2 - 2.0 * s
    z_vel = (dh00 * z0 + dh10 * segment_dt * z_vel0 + dh01 * z1 + dh11 * segment_dt * z_vel1) / segment_dt

    ddh00 = 12.0 * s - 6.0
    ddh10 = 6.0 * s - 4.0
    ddh01 = -12.0 * s + 6.0
    ddh11 = 6.0 * s - 2.0
    z_acc = (ddh00 * z0 + ddh10 * segment_dt * z_vel0 + ddh01 * z1 + ddh11 * segment_dt * z_vel1) / (
        segment_dt**2
    )

    return z, z_vel, z_acc


def compute_ee_velocity_command_and_jacobian(
    model,
    data,
    hand_body_id,
    motors_dof,
    target_pos,
    target_vel,
    target_quat,
    pos_gain,
    rot_gain,
):
    error_pos = target_pos - data.xpos[hand_body_id]

    ee_quat = data.xquat[hand_body_id].copy()
    error_quat = _quat_mul(_inv_quat(ee_quat), target_quat)
    error_rotvec = _quat_to_rotvec(error_quat)

    ee_velocity_cmd = np.concatenate(
        [
            target_vel + pos_gain * error_pos,
            rot_gain * error_rotvec,
        ]
    )

    jacp = np.zeros((3, model.nv))
    jacr = np.zeros((3, model.nv))
    mujoco.mj_jacBody(model, data, jacp, jacr, hand_body_id)
    jacobian = np.vstack([jacp[:, motors_dof], jacr[:, motors_dof]])
    return ee_velocity_cmd, jacobian


def _build_franka_lift_model(dt):
    root = ET.parse(XML_DIR / "panda_no_tendon.xml").getroot()
    root.set("model", "panda_mujoco_lift")
    root.find("compiler").set("meshdir", str(XML_DIR / "assets"))
    root.find("option").set("timestep", f"{dt:.9g}")

    worldbody = root.find("worldbody")
    ET.SubElement(
        worldbody,
        "geom",
        name="floor",
        type="plane",
        size="0 0 0.05",
        rgba="0.45 0.48 0.50 1",
        friction="0.8 0.005 0.0001",
    )
    ET.SubElement(
        worldbody,
        "camera",
        name="overview",
        pos="3.5 0 2.5",
        xyaxes="0 1 0 -0.5735 0 0.8192",
        fovy="30",
    )

    box_body = ET.parse(XML_DIR / "box.xml").getroot().find("worldbody").find("body")
    box_body.find("freejoint").set("name", "cuboid_freejoint")
    box_geom = box_body.find("geom")
    box_geom.set("name", "cuboid_geom")
    box_geom.set("rgba", "0.18 0.42 0.82 1")
    box_geom.set("size", "0.0125 0.0125 0.075")
    worldbody.append(box_body)

    for actuator_name in ("actuator8", "actuator9"):
        actuator = root.find(f".//actuator/general[@name='{actuator_name}']")
        actuator.set("gainprm", "250")
        actuator.set("biasprm", "0 -250 -25")
        actuator.set("forcerange", "-250 250")

    xml = ET.tostring(root, encoding="unicode")
    return mujoco.MjModel.from_xml_string(xml)


def _body_id(model, name):
    body_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
    if body_id < 0:
        raise ValueError(f"body not found in MuJoCo model: {name}")
    return body_id


def _joint_id(model, name):
    joint_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
    if joint_id < 0:
        raise ValueError(f"joint not found in MuJoCo model: {name}")
    return joint_id


def _geoms_for_body(model, body_id):
    return {geom_id for geom_id in range(model.ngeom) if int(model.geom_bodyid[geom_id]) == body_id}


def _rotate_by_quat(vec, quat):
    out = np.zeros(3)
    mujoco.mju_rotVecQuat(out, np.asarray(vec, dtype=float), np.asarray(quat, dtype=float))
    return out


def _quat_mul(q1, q2):
    out = np.zeros(4)
    mujoco.mju_mulQuat(out, np.asarray(q1, dtype=float), np.asarray(q2, dtype=float))
    if out[0] < 0.0:
        out *= -1.0
    return out / max(np.linalg.norm(out), 1e-12)


def _inv_quat(quat):
    out = np.asarray(quat, dtype=float).copy()
    out[1:] *= -1.0
    return out


def _quat_to_rotvec(quat):
    quat = np.asarray(quat, dtype=float)
    quat = quat / max(np.linalg.norm(quat), 1e-12)
    q_w = quat[0]
    q_vec = quat[1:]
    s2 = np.linalg.norm(q_vec)
    angle = 2.0 * np.arctan2(s2, abs(q_w))
    inv_sinc = angle / max(s2, 1e-12)
    return (1.0 if q_w >= 0.0 else -1.0) * inv_sinc * q_vec


FrankaEnv = FrankaMuJoCoEnv


def main():
    parser = argparse.ArgumentParser(description="MuJoCo Franka lift environment smoke test")
    parser.add_argument("-v", "--vis", action="store_true", default=False)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--solid-up", action="store_true", default=False)
    parser.add_argument("--debug", action="store_true", default=False)
    args = parser.parse_args()

    env = FrankaMuJoCoEnv(vis=args.vis, solid_up=args.solid_up, debug=args.debug)
    try:
        print("initial obs:", env.get_obs_flat())
        for i in range(args.steps):
            action = np.array([0.0, -1.0, -1.0])
            _, reward, done = env.step(action)
            if (i + 1) % 10 == 0 or done:
                obs = env.get_observation()
                print(
                    f"step={i + 1:04d} reward={reward:+.3f} done={done} "
                    f"ee_z={obs['ee_pos']:.4f} cuboid_rel_z={obs['cuboid_rel_z']:+.4f} "
                    f"desired_rel_z={obs['desired_rel_z']:+.4f}"
                )
            if done:
                break
    finally:
        env.close()


if __name__ == "__main__":
    main()

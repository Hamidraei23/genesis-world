import argparse
import time
from pathlib import Path

import numpy as np

try:
    from .env_franka import FrankaEnv
    from .single_franka_vel_sep import cyclic_z_reference, save_motion_plot, save_torque_plot
except ImportError:
    from env_franka import FrankaEnv
    from single_franka_vel_sep import cyclic_z_reference, save_motion_plot, save_torque_plot

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLOT_FILE = REPO_ROOT / "franka_env_velocity_motion.png"
GRIPPER_OPEN_Z_THRESHOLD = 0.93
BRAKE_EE_Z_VEL_THRESHOLD = 0.05
BRAKE_STOPPED_EE_Z_VEL_THRESHOLD = 0.02


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=False)
    parser.add_argument("-c", "--cpu", action="store_true", default=False)
    parser.add_argument("--steps", type=int, default=10000)
    parser.add_argument("--plot-file", type=str, default=str(DEFAULT_PLOT_FILE))
    parser.add_argument("--training", action="store_true", default=False,
                        help="Training mode: skip plot generation")
    parser.add_argument("--cycle", type=int, default=0,
                        help="Number of completed gripper open-close cycles before braking; 0 disables")
    args = parser.parse_args()
    if args.cycle < 0:
        parser.error("--cycle must be >= 0")

    env = FrankaEnv(vis=args.vis, cpu=args.cpu)

    target_distance = 0.1
    upward_accel = 5.0
    braking_accel = 13.0

    def sample_target_z(t):
        return cyclic_z_reference(
            t=t,
            start_z=env.target_center[2],
            distance=target_distance,
            accel_up=upward_accel,
            decel_mag=braking_accel,
        )

    times = []
    target_z_history = []
    actual_z_history = []
    target_z_vel_history = []
    actual_z_vel_history = []
    target_z_acc_history = []
    actual_z_acc_history = []
    position_error_history = []
    qvel_norm_history = []
    torque_history = []

    prev_actual_z_vel = None
    prev_torque = None
    gripper_target_sample = None
    last_actual_z = None
    last_actual_z_vel = None
    last_gripper_is_open = None
    saw_gripper_open = False
    completed_gripper_cycles = 0
    cycle_limit_reached = False
    braking = False
    brake_gripper_pos = env.closed_gripper_pos.copy()
    brake_hold_z = None
    t_offset = 0.0          # keeps plot time monotonic across episode resets
    high_level_steps = args.steps // env.target_update_every

    env.print_finger_status("before motion start")
    print(
        "Realistic smooth target stream enabled: "
        f"target_dt={env.target_period:.3f}s, sim_dt={env.dt:.3f}s, "
        f"interpolation_delay={env.target_period:.3f}s"
    )
    if args.cycle > 0:
        print(
            f"Cycle brake enabled: braking after {args.cycle} completed gripper "
            f"open-close cycle(s), once |ee_z_vel| < {BRAKE_EE_Z_VEL_THRESHOLD:.3f} m/s."
        )

    t_real_start = time.perf_counter()

    def reset_cycle_state():
        nonlocal gripper_target_sample, last_actual_z, last_actual_z_vel
        nonlocal last_gripper_is_open, saw_gripper_open, completed_gripper_cycles
        nonlocal cycle_limit_reached, braking, brake_gripper_pos, brake_hold_z

        gripper_target_sample = None
        last_actual_z = None
        last_actual_z_vel = None
        last_gripper_is_open = None
        saw_gripper_open = False
        completed_gripper_cycles = 0
        cycle_limit_reached = False
        braking = False
        brake_gripper_pos = env.closed_gripper_pos.copy()
        brake_hold_z = None

    def update_gripper_cycle_counter(gripper_is_open, gripper_pos, step_idx):
        nonlocal last_gripper_is_open, saw_gripper_open, completed_gripper_cycles
        nonlocal cycle_limit_reached, brake_gripper_pos

        if last_gripper_is_open is None:
            last_gripper_is_open = gripper_is_open
            saw_gripper_open = gripper_is_open
            return

        if gripper_is_open == last_gripper_is_open:
            return

        if gripper_is_open:
            saw_gripper_open = True
            if not args.training:
                print(f"  [GRIPPER] step={step_idx} opened")
        elif saw_gripper_open:
            completed_gripper_cycles += 1
            saw_gripper_open = False
            if not args.training:
                print(f"  [GRIPPER] step={step_idx} closed  cycles={completed_gripper_cycles}")

            if args.cycle > 0 and completed_gripper_cycles >= args.cycle and not cycle_limit_reached:
                cycle_limit_reached = True
                brake_gripper_pos = gripper_pos.copy()
                print(
                    f"  [CYCLE] reached {completed_gripper_cycles} completed open-close cycle(s); "
                    f"waiting for |ee_z_vel| < {BRAKE_EE_Z_VEL_THRESHOLD:.3f} m/s before braking."
                )

        last_gripper_is_open = gripper_is_open

    def anchor_brake_target(z):
        env.target_center[2] = z
        env.controller.target_center[2] = z
        env.target_z = float(z)
        env.target_z_vel = 0.0
        env.target_z_acc = 0.0
        env.prev_target_z_vel = 0.0
        env.controller.reset((env.target_z, 0.0, 0.0))

    try:
        for i in range(high_level_steps):
            if (
                braking
                and last_actual_z_vel is not None
                and abs(last_actual_z_vel) < BRAKE_STOPPED_EE_Z_VEL_THRESHOLD
            ):
                print(
                    f"  [BRAKE] stopped with ee_z_vel={last_actual_z_vel:+.4f} m/s "
                    f"at target_z={brake_hold_z:.4f}"
                )
                break

            t = env.sim_step * env.dt
            high_level_sample = sample_target_z(t)

            if braking:
                high_level_sample = (brake_hold_z, 0.0, 0.0)
                gripper_pos = brake_gripper_pos
                v_des = 0.0
            else:
                if gripper_target_sample is None:
                    gripper_target_sample = high_level_sample
                gripper_is_open = gripper_target_sample[0] >= GRIPPER_OPEN_Z_THRESHOLD
                if gripper_is_open:
                    gripper_pos = env.open_gripper_pos
                else:
                    gripper_pos = env.closed_gripper_pos

                if not cycle_limit_reached:
                    update_gripper_cycle_counter(gripper_is_open, gripper_pos, i)
                else:
                    gripper_pos = brake_gripper_pos

                v_des = 1.2 * high_level_sample[1]

                if (
                    cycle_limit_reached
                    and last_actual_z is not None
                    and last_actual_z_vel is not None
                    and abs(last_actual_z_vel) < BRAKE_EE_Z_VEL_THRESHOLD
                ):
                    braking = True
                    brake_hold_z = last_actual_z
                    high_level_sample = (brake_hold_z, 0.0, 0.0)
                    v_des = 0.0
                    anchor_brake_target(brake_hold_z)
                    print(
                        f"  [BRAKE] step={i} anchoring target_z={brake_hold_z:.4f} "
                        f"with ee_z_vel={last_actual_z_vel:+.4f} m/s"
                    )

            # high_level_sample[1] = 1.2 * high_level_sample[1]
            action = np.concatenate(([v_des], gripper_pos))
            records, reward, done = env.step(action)
            if done:
                print(f"  [DONE] high-level step={i}  reward={reward:.1f}  episode reset")
                if braking:
                    print("  [BRAKE] episode ended after braking; stopping runner.")
                    break
                t_offset = times[-1] if times else 0.0
                prev_actual_z_vel = None
                prev_torque = None
                reset_cycle_state()
                continue   # skip appending crash-state records to the plot
            if not braking:
                gripper_target_sample = high_level_sample

            for record in records:
                last_actual_z = record.actual_pos[2]
                last_actual_z_vel = record.actual_vel[2]
                actual_z_acc = (
                    (record.actual_vel[2] - prev_actual_z_vel) / env.dt
                    if prev_actual_z_vel is not None
                    else 0.0
                )
                prev_actual_z_vel = record.actual_vel[2]

                times.append(t_offset + record.time)
                target_z_history.append(record.target_pos[2])
                actual_z_history.append(record.actual_pos[2])
                target_z_vel_history.append(record.target_vel[2])
                actual_z_vel_history.append(record.actual_vel[2])
                target_z_acc_history.append(record.target_z_acc)
                actual_z_acc_history.append(actual_z_acc)
                position_error_history.append(np.linalg.norm(record.target_pos - record.actual_pos))
                qvel_norm_history.append(np.linalg.norm(record.qvel))

                tau_dot = (record.tau - prev_torque) / env.dt if prev_torque is not None else np.zeros_like(record.tau)
                prev_torque = record.tau
                torque_history.append((record.tau, tau_dot))

                if (record.sim_step + 1) % 1000 == 0:
                    env.print_finger_status(f"step {record.sim_step + 1}")

            t_sim = records[-1].time
            t_wall = time.perf_counter() - t_real_start
            if t_wall < t_sim:
                time.sleep(t_sim - t_wall)

            if not args.training and (i + 1) % 10 == 0:
                obs = env.get_observation()
                dist = abs(obs["cuboid_rel_z"] - obs["desired_rel_z"])
                print(
                    f"  hl_step {i+1:5d}  "
                    f"cuboid_rel_z={obs['cuboid_rel_z']:+.4f}  "
                    f"desired_rel_z={obs['desired_rel_z']:+.4f}  "
                    f"dist={dist:.4f}"
                    f"ee_pos_z={obs['ee_pos']:.4f}  "
                    f"ee_vel_z={obs['ee_vel']:.4f}  "
                )

    except KeyboardInterrupt:
        print("Simulation stopped manually.")

    if not args.training and len(times) > 0:
        save_motion_plot(
            args.plot_file,
            np.array(times),
            np.array(target_z_history),
            np.array(actual_z_history),
            np.array(target_z_vel_history),
            np.array(actual_z_vel_history),
            np.array(target_z_acc_history),
            np.array(actual_z_acc_history),
            np.array(position_error_history),
            np.array(qvel_norm_history),
        )
        torque_plot_file = str(Path(args.plot_file).with_stem(Path(args.plot_file).stem + "_torques"))
        save_torque_plot(
            torque_plot_file,
            np.array(times),
            [t[0] for t in torque_history],
            [t[1] for t in torque_history],
        )


if __name__ == "__main__":
    main()

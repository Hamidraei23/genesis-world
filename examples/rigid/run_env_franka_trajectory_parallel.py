"""
Equivalent of run_env_franka_trajectory.py using FrankaEnvParallel with num_envs=1.

Run this to visually verify the parallel env produces the same trajectory as the
single-env version.  The motion plot is saved at target_dt resolution (every 0.02 s)
rather than sim_dt (0.001 s) because the parallel env's step() returns one obs per
high-level action, not per sim step.  The trajectory shape should be identical.

Usage:
    python3 examples/rigid/run_env_franka_trajectory_parallel.py -v
    python3 examples/rigid/run_env_franka_trajectory_parallel.py -v --steps 5000
    python3 examples/rigid/run_env_franka_trajectory_parallel.py --record  # headless MP4
"""

import argparse
import time
from pathlib import Path

import numpy as np
import torch

import genesis as gs

try:
    from .env_franka_parallel import FrankaEnvParallel
    from .single_franka_vel_sep import cyclic_z_reference, save_motion_plot
except ImportError:
    from env_franka_parallel import FrankaEnvParallel
    from single_franka_vel_sep import cyclic_z_reference, save_motion_plot

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLOT_FILE = REPO_ROOT / "franka_parallel_trajectory.png"
DEFAULT_REWARD_PLOT_FILE = REPO_ROOT / "franka_parallel_reward.png"
DEFAULT_REWARD_PARTS_PLOT_FILE = REPO_ROOT / "franka_parallel_reward_parts.png"
CLOSED_GRIPPER = -1.0
OPEN_GRIPPER = 1.0
GRIPPER_OPEN_Z_THRESHOLD = 0.93
BRAKE_EE_Z_VEL_THRESHOLD = 0.05
BRAKE_STOPPED_EE_Z_VEL_THRESHOLD = 0.02
REWARD_PARTS = (
    ("z_track", "z_track"),
    ("centering", "centering"),
    ("grip_force", "grip_force"),
    ("jerk_penalty", "jerk_penalty"),
    ("z_acc_penalty", "z_acc_penalty"),
    ("ee_z_penalty", "ee_z_penalty"),
    ("regrasp_bonus", "regrasp_bonus"),
)


def save_reward_plot(plot_file, steps, rewards, returns):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

    axes[0].plot(steps, rewards, label="step reward", linewidth=1.4)
    axes[0].set_ylabel("reward")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(steps, returns, label="episode return", color="tab:green", linewidth=1.4)
    axes[1].set_ylabel("return")
    axes[1].set_xlabel("high-level step")
    axes[1].legend(loc="best")
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("Franka Parallel Reward")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved reward plot to {plot_path.resolve()}")


def save_reward_parts_plot(plot_file, steps, reward_parts):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(len(REWARD_PARTS), 1, figsize=(10, 11), sharex=True)

    for ax, (key, label) in zip(axes, REWARD_PARTS):
        ax.plot(steps, reward_parts[key], label=label, linewidth=1.3)
        ax.set_ylabel(label)
        ax.legend(loc="best")
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("high-level step")
    fig.suptitle("Franka Parallel Reward Parts")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved reward parts plot to {plot_path.resolve()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=False,
                        help="Open interactive viewer")
    parser.add_argument("--record", action="store_true", default=False,
                        help="Record offscreen video to franka_parallel.mp4")
    parser.add_argument("--steps", type=int, default=10000,
                        help="Total sim steps to run")
    parser.add_argument("--plot-file", type=str, default=str(DEFAULT_PLOT_FILE))
    parser.add_argument("--reward-plot-file", type=str, default=str(DEFAULT_REWARD_PLOT_FILE))
    parser.add_argument("--reward-parts-plot-file", type=str, default=str(DEFAULT_REWARD_PARTS_PLOT_FILE))
    parser.add_argument("--training", action="store_true", default=False,
                        help="Training mode: skip plot generation")
    parser.add_argument("--cycle", type=int, default=0,
                        help="Number of completed gripper open-close cycles before braking; 0 disables")
    parser.add_argument("--limit-regrasp", action="store_true",
                        help="Terminate an episode as failure on the 4th regrasp")
    args = parser.parse_args()
    if args.cycle < 0:
        parser.error("--cycle must be >= 0")

    gs.init(backend=gs.gpu, precision="32", logging_level="warning")

    env = FrankaEnvParallel(
        num_envs=1,
        vis=args.vis or args.record,
        dt=0.001,
        target_dt=0.02,
        limit_regrasp=args.limit_regrasp,
    )

    # Optionally attach an offscreen camera to record env 0
    cam = None
    if args.record:
        cam = env.scene.add_camera(
            res=(1280, 960),
            pos=(3.5, 0.0, 2.5),
            lookat=(0.0, 0.0, 0.5),
            fov=30,
            GUI=False,
        )
        cam.start_recording()

    # start_z comes from the settled ee position after warmup
    start_z = env.target_center[0, 2].item()

    target_distance = 0.1
    upward_accel = 5.0
    braking_accel = 13.0

    def sample_target_z(t):
        return cyclic_z_reference(
            t=t,
            start_z=start_z,
            distance=target_distance,
            accel_up=upward_accel,
            decel_mag=braking_accel,
        )

    # ------------------------------------------------------------------ #
    # Tracking buffers  (one sample per high-level step = every target_dt)#
    # ------------------------------------------------------------------ #
    times = []
    target_z_history = []
    actual_z_history = []
    target_z_vel_history = []
    actual_z_vel_history = []
    target_z_acc_history = []
    actual_z_acc_history = []
    position_error_history = []
    reward_step_history = []
    reward_history = []
    episode_return_history = []
    reward_parts_history = {key: [] for key, _ in REWARD_PARTS}

    prev_actual_z_vel = None
    last_actual_z = None
    last_actual_z_vel = None
    gripper_z = None  # last high-level z used for gripper decision
    last_gripper_is_open = None
    saw_gripper_open = False
    completed_gripper_cycles = 0
    cycle_limit_reached = False
    braking = False
    brake_gripper_val = CLOSED_GRIPPER
    brake_hold_z = None
    episode_t0 = 0.0  # sim_step*dt at the start of the current episode
    t_offset = 0.0    # keeps plot time monotonic across episode resets
    episode_return = 0.0
    episode_reward_step = 0
    high_level_steps = args.steps // env.target_update_every

    print(
        f"target_dt={env.target_period:.3f}s  sim_dt={env.dt:.3f}s  "
        f"steps_per_target={env.target_update_every}"
    )
    print(f"Running {high_level_steps} high-level steps ({args.steps} sim steps total)...")
    if args.cycle > 0:
        print(
            f"Cycle brake enabled: braking after {args.cycle} completed gripper "
            f"open-close cycle(s), once |ee_z_vel| < {BRAKE_EE_Z_VEL_THRESHOLD:.3f} m/s."
        )

    t_real_start = time.perf_counter()

    def reset_cycle_state():
        nonlocal episode_return, episode_reward_step
        nonlocal last_actual_z, last_actual_z_vel, gripper_z, last_gripper_is_open
        nonlocal saw_gripper_open, completed_gripper_cycles, cycle_limit_reached
        nonlocal braking, brake_gripper_val, brake_hold_z

        episode_return = 0.0
        episode_reward_step = 0
        last_actual_z = None
        last_actual_z_vel = None
        gripper_z = None
        last_gripper_is_open = None
        saw_gripper_open = False
        completed_gripper_cycles = 0
        cycle_limit_reached = False
        braking = False
        brake_gripper_val = CLOSED_GRIPPER
        brake_hold_z = None

    def update_gripper_cycle_counter(gripper_is_open, gripper_val, step_idx):
        nonlocal last_gripper_is_open, saw_gripper_open, completed_gripper_cycles
        nonlocal cycle_limit_reached, brake_gripper_val

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
                brake_gripper_val = gripper_val
                print(
                    f"  [CYCLE] reached {completed_gripper_cycles} completed open-close cycle(s); "
                    f"waiting for |ee_z_vel| < {BRAKE_EE_Z_VEL_THRESHOLD:.3f} m/s before braking."
                )

        last_gripper_is_open = gripper_is_open

    def anchor_brake_target(z):
        env.target_center[0, 2] = z
        env.target_z[0] = z
        env.target_z_vel[0] = 0.0
        env.target_z_acc[0] = 0.0
        env.prev_target_z_vel[0] = 0.0
        env._seg_start = None
        env._seg_end = None
        env._seg_t0[0] = env.sim_step * env.dt

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

            t = env.sim_step * env.dt - episode_t0
            z_ref, z_vel_ref, z_acc_ref = sample_target_z(t)
            z_vel_ref = z_vel_ref * 1.25

            if braking:
                z_ref = brake_hold_z if brake_hold_z is not None else z_ref
                z_vel_ref = 0.0
                z_acc_ref = 0.0
                gripper_val = brake_gripper_val
            else:
                # Gripper: open above threshold, closed below. Preserve the existing
                # one-sample delay by deciding from the previous high-level z sample.
                if gripper_z is None:
                    gripper_z = z_ref
                gripper_is_open = gripper_z >= GRIPPER_OPEN_Z_THRESHOLD
                gripper_val = OPEN_GRIPPER if gripper_is_open else CLOSED_GRIPPER
                gripper_z = z_ref

                if not cycle_limit_reached:
                    update_gripper_cycle_counter(gripper_is_open, gripper_val, i)
                else:
                    gripper_val = brake_gripper_val

                if (
                    cycle_limit_reached
                    and last_actual_z is not None
                    and last_actual_z_vel is not None
                    and abs(last_actual_z_vel) < BRAKE_EE_Z_VEL_THRESHOLD
                ):
                    braking = True
                    brake_hold_z = last_actual_z
                    z_ref = brake_hold_z
                    z_vel_ref = 0.0
                    z_acc_ref = 0.0
                    anchor_brake_target(brake_hold_z)
                    print(
                        f"  [BRAKE] step={i} anchoring target_z={brake_hold_z:.4f} "
                        f"with ee_z_vel={last_actual_z_vel:+.4f} m/s"
                    )

            # Action tensor shape (1, 3): [target_z_vel, finger_l, finger_r]
            action = torch.tensor(
                [[z_vel_ref, gripper_val, gripper_val]],
                dtype=torch.float32,
                device=gs.device,
            )
            obs_td, reward, done, _ = env.step(action)
            obs = obs_td["policy"]
            step_reward = reward[0].item()
            episode_return += step_reward
            episode_reward_step += 1
            reward_step_history.append(episode_reward_step)
            reward_history.append(step_reward)
            episode_return_history.append(episode_return)
            for key, _ in REWARD_PARTS:
                reward_parts_history[key].append(env.last_reward_terms[key][0].item())

            n_done = done.sum().item()
            if n_done > 0:
                total_reward = reward[done].sum().item()
                print(f"  [DONE] step={i}  envs_reset={int(n_done)}  total_reward={total_reward:.1f}")
                if braking:
                    print("  [BRAKE] episode ended after braking; stopping runner.")
                    break
                episode_t0 = env.sim_step * env.dt  # restart reference phase
                t_offset = times[-1] if times else 0.0  # keep plot time monotonic
                prev_actual_z_vel = None
                reset_cycle_state()
                continue   # skip appending crash-state obs to the plot

            if cam is not None:
                cam.render()

            # --- read scalars from obs (env index 0) ---
            actual_z = obs[0, FrankaEnvParallel.OBS_EE_POS_Z].item()
            actual_z_vel = obs[0, FrankaEnvParallel.OBS_EE_VEL_Z].item()
            last_actual_z = actual_z
            last_actual_z_vel = actual_z_vel

            actual_z_acc = (
                (actual_z_vel - prev_actual_z_vel) / env.target_period
                if prev_actual_z_vel is not None
                else 0.0
            )
            prev_actual_z_vel = actual_z_vel

            t_after = env.sim_step * env.dt - episode_t0
            times.append(t_offset + t_after)
            target_z_history.append(z_ref)
            actual_z_history.append(actual_z)
            target_z_vel_history.append(z_vel_ref)
            actual_z_vel_history.append(actual_z_vel)
            target_z_acc_history.append(z_acc_ref)
            actual_z_acc_history.append(actual_z_acc)
            position_error_history.append(abs(z_ref - actual_z))

            if not args.training and (i + 1) % 100 == 0:
                ft_dist = obs[0, FrankaEnvParallel.OBS_FINGERTIP_DIST].item()
                lf = obs[0, FrankaEnvParallel.OBS_LEFT_FORCE].cpu().numpy()
                rf = obs[0, FrankaEnvParallel.OBS_RIGHT_FORCE].cpu().numpy()
                cuboid_rel_z = obs[0, FrankaEnvParallel.OBS_CUBOID_REL_Z].item()
                desired_rel_z = obs[0, FrankaEnvParallel.OBS_DESIRED_REL_Z].item()
                dist_to_desired = abs(cuboid_rel_z - desired_rel_z)
                print(
                    f"step {env.sim_step:6d}  z={actual_z:.4f}  "
                    f"err={position_error_history[-1]*1000:.2f}mm  "
                    f"ft_dist={ft_dist:.4f}  "
                    f"|lf|={np.linalg.norm(lf):.3f}  |rf|={np.linalg.norm(rf):.3f}  "
                    f"cuboid_rel_z={cuboid_rel_z:+.4f}  "
                    f"desired_rel_z={desired_rel_z:+.4f}  "
                    f"dist={dist_to_desired:.4f}"
                )

            # Optional: pace playback so viewer is not too fast
            if args.vis:
                t_sim = t_after
                t_wall = time.perf_counter() - t_real_start
                if t_wall < t_sim:
                    time.sleep(t_sim - t_wall)

    except KeyboardInterrupt:
        print("Stopped manually.")

    if cam is not None:
        cam.stop_recording(save_to_filename="franka_parallel.mp4", fps=60)
        print("Saved franka_parallel.mp4")

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
            np.zeros(len(times)),   # qvel_norm not available from obs dict
        )

    if not args.training and len(reward_step_history) > 0:
        save_reward_plot(
            args.reward_plot_file,
            np.array(reward_step_history),
            np.array(reward_history),
            np.array(episode_return_history),
        )
        save_reward_parts_plot(
            args.reward_parts_plot_file,
            np.array(reward_step_history),
            {key: np.array(values) for key, values in reward_parts_history.items()},
        )


if __name__ == "__main__":
    main()

1 file changed
+563
-0
examples/rigid/run_policy_franka_mujoco.py
"""
CPU single-env policy rollout for a trained Franka PPO policy in MuJoCo.

This is the MuJoCo equivalent of run_policy_franka_cpu.py. It uses the same
trained policy interface and observation/action layout, but rolls out with
FrankaMuJoCoEnv instead of the Genesis FrankaEnv.

Usage:
    python3 examples/rigid/run_policy_franka_mujoco.py -e franka-lift-v1 --vis --ckpt 360
    python3 examples/rigid/run_policy_franka_mujoco.py -e franka-lift-v1 --vis --ckpt 360 --negative

Checkpoints are loaded from:
    logs/<exp_name>/model_<iter>.pt
"""

import argparse
import os
import pickle
from importlib import metadata
from pathlib import Path

import numpy as np

try:
    from .env_franka_mujoco import FrankaMuJoCoEnv
except ImportError:
    from env_franka_mujoco import FrankaMuJoCoEnv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLOT_FILE = REPO_ROOT / "franka_policy_mujoco_motion.png"
DEFAULT_REWARD_PLOT_FILE = REPO_ROOT / "franka_policy_mujoco_reward.png"
DEFAULT_REWARD_PARTS_PLOT_FILE = REPO_ROOT / "franka_policy_mujoco_reward_parts.png"
DEFAULT_SAVE_FILE = REPO_ROOT / "franka_policy_mujoco_z_vel.npy"
REWARD_PARTS = (
    ("tracking", "3*tracking"),
    ("jerk_penalty", "jerk_penalty"),
    ("z_acc_penalty", "z_acc_penalty"),
    ("grip", "0.5*grip"),
    ("ee_z_penalty", "ee_z_penalty"),
)


def _load_policy_dependencies():
    try:
        import torch
        from tensordict import TensorDict

        if int(metadata.version("rsl-rl-lib").split(".")[0]) < 5:
            raise ImportError
        from rsl_rl.runners import OnPolicyRunner
    except (metadata.PackageNotFoundError, ImportError, ValueError) as e:
        raise ImportError("Please install torch, tensordict, and rsl-rl-lib>=5.0.0 to run a trained policy.") from e

    return torch, TensorDict, OnPolicyRunner


class _RunnerEnvProxy:
    """
    Minimal env shim used only to initialise OnPolicyRunner.

    The rollout uses FrankaMuJoCoEnv directly; this proxy only gives rsl-rl the
    observation/action dimensions needed to reconstruct the policy network.
    """

    num_envs = 1
    num_actions = 3
    extras = {}
    cfg = {}

    def __init__(self, torch_module, tensor_dict_cls):
        self.torch = torch_module
        self.TensorDict = tensor_dict_cls

    def _obs_td(self):
        obs = self.torch.zeros(1, FrankaMuJoCoEnv.OBS_DIM)
        return self.TensorDict({"policy": obs}, batch_size=[1])

    def get_observations(self):
        return self._obs_td()

    def reset(self):
        return self._obs_td()

    def step(self, _actions):
        return self._obs_td(), self.torch.zeros(1), self.torch.zeros(1, dtype=self.torch.bool), {}


def obs_to_tensordict(obs_flat: np.ndarray, device, torch_module, tensor_dict_cls):
    t = torch_module.tensor(obs_flat, dtype=torch_module.float32, device=device).unsqueeze(0)
    return tensor_dict_cls({"policy": t}, batch_size=[1])


def _shade_releases(ax, release_spans):
    for idx, (s0, s1) in enumerate(release_spans):
        ax.axvspan(s0, s1, color="orange", alpha=0.25, label="release window" if idx == 0 else "")


def save_policy_motion_plot(
    plot_file,
    times,
    target_z,
    actual_z,
    target_z_vel,
    actual_z_vel,
    target_z_acc,
    actual_z_acc,
    z_error,
    position_error,
    qvel_norm,
    release_spans=None,
    cuboid_z_error_times=None,
    cuboid_z_error=None,
):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    release_spans = release_spans or []
    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    n_panels = 7 if cuboid_z_error is not None and len(cuboid_z_error) > 0 else 6
    fig, axes = plt.subplots(n_panels, 1, figsize=(10, 16 + 2 * (n_panels - 6)), sharex=True)

    axes[0].plot(times, target_z, label="target z", linewidth=1.8)
    axes[0].plot(times, actual_z, label="actual z", linewidth=1.2)
    _shade_releases(axes[0], release_spans)
    axes[0].set_ylabel("z position (m)")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(times, target_z_vel, label="target vz", linewidth=1.8)
    axes[1].plot(times, actual_z_vel, label="actual vz", linewidth=1.2)
    _shade_releases(axes[1], release_spans)
    axes[1].set_ylabel("z velocity (m/s)")
    axes[1].legend(loc="best")
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(times, target_z_acc, label="target az", linewidth=1.8)
    axes[2].plot(times, actual_z_acc, label="actual az", linewidth=1.2)
    _shade_releases(axes[2], release_spans)
    axes[2].set_ylabel("z acceleration (m/s^2)")
    axes[2].legend(loc="best")
    axes[2].grid(True, alpha=0.3)

    axes[3].plot(times, z_error, color="tab:purple", linewidth=1.2)
    _shade_releases(axes[3], release_spans)
    axes[3].set_ylabel("EE z error (m)")
    axes[3].grid(True, alpha=0.3)

    axes[4].plot(times, position_error, color="tab:red", linewidth=1.2)
    _shade_releases(axes[4], release_spans)
    axes[4].set_ylabel("position error (m)")
    axes[4].grid(True, alpha=0.3)

    axes[5].plot(times, qvel_norm, color="tab:green", linewidth=1.2)
    _shade_releases(axes[5], release_spans)
    axes[5].set_ylabel("||qvel||")
    axes[5].grid(True, alpha=0.3)

    if n_panels == 7:
        axes[6].plot(
            cuboid_z_error_times,
            cuboid_z_error,
            color="tab:orange",
            linewidth=1.2,
            label="|cuboid_rel_z - desired_rel_z|",
        )
        _shade_releases(axes[6], release_spans)
        axes[6].set_ylabel("cuboid z_error (m)")
        axes[6].set_xlabel("time (s)")
        axes[6].legend(loc="best")
        axes[6].grid(True, alpha=0.3)
    else:
        axes[5].set_xlabel("time (s)")

    fig.suptitle("MuJoCo Franka PPO Policy Rollout Motion  (orange = gripper release)")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved motion plot to {plot_path.resolve()}")


def save_torque_plot(plot_file, times, torques, torques_dot):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    torques = np.array(torques)
    torques_dot = np.array(torques_dot)
    labels = [f"J{j + 1}" for j in range(torques.shape[1])]

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    for j, label in enumerate(labels):
        axes[0].plot(times, torques[:, j], label=label, linewidth=1.2)
    axes[0].set_ylabel("torque (N*m)")
    axes[0].legend(loc="best", ncol=4)
    axes[0].grid(True, alpha=0.3)

    for j, label in enumerate(labels):
        axes[1].plot(times, torques_dot[:, j], label=label, linewidth=1.2)
    axes[1].set_ylabel("torque rate (N*m/s)")
    axes[1].set_xlabel("time (s)")
    axes[1].legend(loc="best", ncol=4)
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("MuJoCo Franka Joint Torques")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved torque plot to {plot_path.resolve()}")


def save_reward_plot(plot_file, steps, rewards, returns, release_spans=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    release_spans = release_spans or []
    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

    axes[0].plot(steps, rewards, label="step reward", linewidth=1.4)
    _shade_releases(axes[0], release_spans)
    axes[0].set_ylabel("reward")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(steps, returns, label="episode return", color="tab:green", linewidth=1.4)
    _shade_releases(axes[1], release_spans)
    axes[1].set_ylabel("return")
    axes[1].set_xlabel("high-level step")
    axes[1].legend(loc="best")
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("MuJoCo Franka PPO Policy Reward  (orange = gripper release)")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved reward plot to {plot_path.resolve()}")


def save_reward_parts_plot(plot_file, steps, reward_parts, release_spans=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    release_spans = release_spans or []
    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(len(REWARD_PARTS), 1, figsize=(10, 11), sharex=True)

    for ax, (key, label) in zip(axes, REWARD_PARTS):
        ax.plot(steps, reward_parts[key], label=label, linewidth=1.3)
        _shade_releases(ax, release_spans)
        ax.set_ylabel(label)
        ax.legend(loc="best")
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("high-level step")
    fig.suptitle("MuJoCo Franka PPO Policy Reward Parts  (orange = gripper release)")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved reward parts plot to {plot_path.resolve()}")


def _resolve_checkpoint(log_dir, ckpt):
    if ckpt is not None:
        return os.path.join(log_dir, f"model_{ckpt}.pt")

    pts = [f for f in os.listdir(log_dir) if f.startswith("model_") and f.endswith(".pt")]
    if not pts:
        raise FileNotFoundError(f"No model_*.pt found in {log_dir}")
    pts.sort(key=lambda f: int(f.split("_")[1].split(".")[0]))
    return os.path.join(log_dir, pts[-1])


def main():
    parser = argparse.ArgumentParser(description="CPU single-env PPO policy rollout for MuJoCo Franka")
    parser.add_argument(
        "-e",
        "--exp_name",
        type=str,
        default="franka-lift-v1",
        help="Experiment name matching the training run",
    )
    parser.add_argument("--ckpt", type=int, default=None, help="Checkpoint iteration (e.g. 360). Defaults to latest.")
    parser.add_argument("--vis", action="store_true", default=False, help="Open MuJoCo interactive viewer")
    parser.add_argument("--record", action="store_true", default=False, help="Headless compatibility flag; no video writer yet.")
    parser.add_argument("--steps", type=int, default=10000, help="Max high-level steps for the single episode (0 = no cap)")
    parser.add_argument(
        "--plot-file",
        type=str,
        default=str(DEFAULT_PLOT_FILE),
        help="Path for the motion plot. Torque plot uses the same stem plus '_torques'.",
    )
    parser.add_argument("--reward-plot-file", type=str, default=str(DEFAULT_REWARD_PLOT_FILE))
    parser.add_argument("--reward-parts-plot-file", type=str, default=str(DEFAULT_REWARD_PARTS_PLOT_FILE))
    parser.add_argument("--dt", type=float, default=0.001)
    parser.add_argument("--target_dt", type=float, default=0.02)
    parser.add_argument(
        "--playback-speed",
        type=float,
        default=0.5,
        help="Viewer playback speed relative to real time. 0.5 = half speed.",
    )
    parser.add_argument(
        "--negative",
        action="store_true",
        default=False,
        help="Use negative desired_rel_z (solid-up mode). Pass when evaluating a --negative-trained checkpoint.",
    )
    parser.add_argument("--save", action="store_true", default=False, help="Save policy z_vel outputs to a .npy file.")
    parser.add_argument("--save-file", type=str, default=str(DEFAULT_SAVE_FILE))
    args = parser.parse_args()

    if args.playback_speed <= 0.0:
        raise ValueError("--playback-speed must be greater than 0")

    if args.record:
        args.vis = False

    torch, tensor_dict_cls, runner_cls = _load_policy_dependencies()
    device = torch.device("cpu")
    log_dir = f"logs/{args.exp_name}"
    ckpt_path = _resolve_checkpoint(log_dir, args.ckpt)
    print(f"Loading checkpoint: {ckpt_path}")

    with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
        train_cfg = pickle.load(f)

    env = FrankaMuJoCoEnv(
        vis=args.vis,
        dt=args.dt,
        target_dt=args.target_dt,
        playback_speed=args.playback_speed,
        solid_up=args.negative,
    )

    runner = runner_cls(_RunnerEnvProxy(torch, tensor_dict_cls), train_cfg, log_dir, device=device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=device)

    obs_flat = env.get_obs_flat()
    obs_td = obs_to_tensordict(obs_flat, device, torch, tensor_dict_cls)
    high_level_steps = args.steps if args.steps > 0 else float("inf")

    ep_reward = 0.0
    ep_len = 0
    ep_count = 0
    prev_actual_z_vel = None
    prev_record_actual_z_vel = None
    prev_torque = None

    times = []
    target_z_history = []
    actual_z_history = []
    target_z_vel_history = []
    actual_z_vel_history = []
    target_z_acc_history = []
    actual_z_acc_history = []
    z_error_history = []
    position_error_history = []
    qvel_norm_history = []
    torque_history = []
    reward_step_history = []
    reward_history = []
    episode_return_history = []
    reward_parts_history = {key: [] for key, _ in REWARD_PARTS}
    cuboid_z_error_step_times = []
    cuboid_z_error_vals = []
    z_vel_output = []

    release_spans_time = []
    release_spans_step = []
    in_release = False
    release_start_time = 0.0
    release_start_step = 0
    force_thresh = env.REGRASP_FORCE_THRESHOLD

    print(
        f"target_dt={env.target_period:.3f}s  sim_dt={env.dt:.3f}s  "
        f"steps_per_target={env.target_update_every}  playback_speed={args.playback_speed:.2f}x"
    )
    print(
        f"Running one MuJoCo episode "
        f"({'no step cap' if args.steps == 0 else f'max {int(high_level_steps)} high-level steps'}) "
        f"(checkpoint: {os.path.basename(ckpt_path)})"
    )

    i = 0
    try:
        with torch.no_grad():
            while i < high_level_steps:
                lf = obs_flat[5:8]
                rf = obs_flat[8:11]
                avg_force = (np.linalg.norm(lf) + np.linalg.norm(rf)) * 0.5
                currently_released = avg_force < force_thresh
                cur_time = times[-1] if times else 0.0
                if currently_released and not in_release:
                    in_release = True
                    release_start_time = cur_time
                    release_start_step = ep_len
                elif not currently_released and in_release:
                    release_spans_time.append((release_start_time, cur_time))
                    release_spans_step.append((release_start_step, ep_len))
                    in_release = False

                action = policy(obs_td).cpu().numpy().flatten()
                records, reward, done = env.step(action)

                if args.save:
                    z_vel_output.append(float(action[0]))

                for record in records:
                    actual_z_acc = (
                        (record.actual_vel[2] - prev_record_actual_z_vel) / env.dt
                        if prev_record_actual_z_vel is not None
                        else 0.0
                    )
                    prev_record_actual_z_vel = record.actual_vel[2]

                    times.append(record.time)
                    target_z_history.append(record.target_pos[2])
                    actual_z_history.append(record.actual_pos[2])
                    target_z_vel_history.append(record.target_vel[2])
                    actual_z_vel_history.append(record.actual_vel[2])
                    target_z_acc_history.append(record.target_z_acc)
                    actual_z_acc_history.append(actual_z_acc)
                    z_error_history.append(record.target_pos[2] - record.actual_pos[2])
                    position_error_history.append(np.linalg.norm(record.target_pos - record.actual_pos))
                    qvel_norm_history.append(np.linalg.norm(record.qvel))

                    tau_dot = (
                        (record.tau - prev_torque) / env.dt if prev_torque is not None else np.zeros_like(record.tau)
                    )
                    prev_torque = record.tau
                    torque_history.append((record.tau, tau_dot))

                ep_reward += reward
                ep_len += 1
                reward_step_history.append(ep_len)
                reward_history.append(reward)
                episode_return_history.append(ep_reward)
                for key, _ in REWARD_PARTS:
                    reward_parts_history[key].append(env.last_reward_terms[key])

                if done:
                    if in_release and times:
                        release_spans_time.append((release_start_time, times[-1]))
                        release_spans_step.append((release_start_step, ep_len))
                        in_release = False
                    success = env.last_done_reason == "success"
                    ep_count += 1
                    result = "SUCCESS" if success else f"fail reason={env.last_done_reason or 'unknown'}"
                    print(f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  {result}")
                    break

                obs_flat = env.get_obs_flat()
                obs_td = obs_to_tensordict(obs_flat, device, torch, tensor_dict_cls)

                cuboid_z_error_step_times.append(times[-1] if times else 0.0)
                cuboid_z_error_vals.append(abs(float(obs_flat[11]) - float(obs_flat[14])))
                if i % 100 == 0:
                    actual_z = float(obs_flat[0])
                    actual_z_vel = float(obs_flat[1])
                    ft_dist = float(obs_flat[2])
                    lf = obs_flat[5:8]
                    rf = obs_flat[8:11]
                    cuboid_rel_z = float(obs_flat[11])
                    desired_rel_z = float(obs_flat[14])
                    z_acc = (
                        (actual_z_vel - prev_actual_z_vel) / env.target_period
                        if prev_actual_z_vel is not None
                        else 0.0
                    )
                    prev_actual_z_vel = actual_z_vel
                    print(
                        f"step {env.sim_step:6d}  z={actual_z:.4f}  z_vel={actual_z_vel:+.3f}  "
                        f"z_acc={z_acc:+.2f}  ft_dist={ft_dist:.4f}  "
                        f"|lf|={np.linalg.norm(lf):.3f}  |rf|={np.linalg.norm(rf):.3f}  "
                        f"cuboid_rel_z={cuboid_rel_z:+.4f}  desired={desired_rel_z:+.4f}  "
                        f"err={abs(cuboid_rel_z - desired_rel_z) * 1000:.2f}mm  "
                        f"ep_rew={ep_reward:.1f}"
                    )

                i += 1

    except KeyboardInterrupt:
        if in_release and times:
            release_spans_time.append((release_start_time, times[-1]))
            release_spans_step.append((release_start_step, ep_len))
            in_release = False
        print("\nStopped by user.")
    finally:
        env.close()

    if i >= high_level_steps and ep_count == 0:
        print(f"Stopped after step cap before episode ended: {int(high_level_steps)} high-level steps.")

    if len(times) > 0:
        save_policy_motion_plot(
            args.plot_file,
            np.array(times),
            np.array(target_z_history),
            np.array(actual_z_history),
            np.array(target_z_vel_history),
            np.array(actual_z_vel_history),
            np.array(target_z_acc_history),
            np.array(actual_z_acc_history),
            np.array(z_error_history),
            np.array(position_error_history),
            np.array(qvel_norm_history),
            release_spans=release_spans_time,
            cuboid_z_error_times=np.array(cuboid_z_error_step_times),
            cuboid_z_error=np.array(cuboid_z_error_vals),
        )
        torque_plot_file = str(Path(args.plot_file).with_stem(Path(args.plot_file).stem + "_torques"))
        save_torque_plot(
            torque_plot_file,
            np.array(times),
            [t[0] for t in torque_history],
            [t[1] for t in torque_history],
        )

    if len(reward_step_history) > 0:
        save_reward_plot(
            args.reward_plot_file,
            np.array(reward_step_history),
            np.array(reward_history),
            np.array(episode_return_history),
            release_spans=release_spans_step,
        )
        save_reward_parts_plot(
            args.reward_parts_plot_file,
            np.array(reward_step_history),
            {key: np.array(values) for key, values in reward_parts_history.items()},
            release_spans=release_spans_step,
        )

    print(f"Episode length: {ep_len} high-level steps ({ep_len * env.target_update_every} sim steps)")

    if args.save and z_vel_output:
        save_path = Path(args.save_file).expanduser()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(save_path, np.array(z_vel_output, dtype=np.float32))
        print(f"Saved z_vel output ({len(z_vel_output)} steps) to {save_path.resolve()}")


if __name__ == "__main__":
    main()

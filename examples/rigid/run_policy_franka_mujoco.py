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
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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
    ("base_reward", "base_reward"),
    ("regrasp_bonus", "regrasp_bonus"),
    ("jerk_penalty", "jerk_penalty"),
    ("z_improvement", "z_improvement"),
    ("avg_force", "avg_force"),
    ("regrasp_event", "regrasp_event"),
    ("success_candidate", "success_candidate"),
    ("success_steps", "success_steps"),
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

def _plot_episode(steps_buf, release_spans, ep_idx, save_dir):
    steps = np.asarray(steps_buf["steps"])
    ee_z = np.asarray(steps_buf["ee_z"])
    cub_rel_z = np.asarray(steps_buf["cuboid_rel_z"])
    des_rel_z = np.asarray(steps_buf["desired_rel_z"])
    ft_dist = np.asarray(steps_buf["ft_dist"])
    lf_mag = np.asarray(steps_buf["lf_mag"])
    rf_mag = np.asarray(steps_buf["rf_mag"])
    reward = np.asarray(steps_buf["reward"])
    cum_reward = np.cumsum(reward)

    fig, axes = plt.subplots(5, 1, figsize=(12, 14), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Overview  (orange = gripper release)", fontsize=12)

    panels = [
        (axes[0], [(cub_rel_z, "cuboid_rel_z", "C0"), (des_rel_z, "desired_rel_z", "C3--")], "Z relative [m]", "Cuboid in-hand Z"),
        (axes[1], [(ee_z, "ee_pos_z", "C1")], "EE height [m]", "End-effector Z"),
        (axes[2], [(ft_dist, "fingertip_dist", "C2")], "Distance [m]", "Fingertip distance"),
        (axes[3], [(lf_mag, "|left_force|", "C4"), (rf_mag, "|right_force|", "C5")], "Force [N]", "Finger contact forces"),
        (axes[4], [(reward, "reward/step", "C6"), (cum_reward, "cumulative", "C7")], "Reward", "Reward"),
    ]
    for ax, series, ylabel, title in panels:
        for y, label, fmt in series:
            ax.plot(steps, y, fmt, label=label, linewidth=1.2)
        _shade_releases(ax, release_spans)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("High-level step")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_overview.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] overview  → {path}")

def _plot_motion(times, target_z_history, actual_z_history, target_z_vel_history, actual_z_vel_history, target_z_acc_history, actual_z_acc_history, position_error_history, release_spans_time, ep_idx, save_dir):
    times = np.asarray(times)
    ee_z = np.asarray(actual_z_history)
    target_z = np.asarray(target_z_history)
    actual_z_vel = np.asarray(actual_z_vel_history)
    target_z_vel = np.asarray(target_z_vel_history)
    actual_z_acc = np.asarray(actual_z_acc_history)
    target_z_acc = np.asarray(target_z_acc_history)
    pos_err = np.asarray(position_error_history)

    fig, axes = plt.subplots(4, 1, figsize=(12, 11), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Motion detail  (orange = gripper release)", fontsize=12)

    panels = [
        (axes[0], [(ee_z, "actual ee_z", "C0"), (target_z, "target z", "C3--")], "Z position [m]", "EE Z position (actual vs target)"),
        (axes[1], [(actual_z_vel, "actual z_vel", "C0"), (target_z_vel, "target z_vel", "C3--")], "Z velocity [m/s]", "Z velocity (actual vs target)"),
        (axes[2], [(actual_z_acc, "actual z_acc", "C0"), (target_z_acc, "target z_acc", "C3--")], "Z accel [m/s²]", "Z acceleration (actual vs commanded)"),
        (axes[3], [(pos_err, "position_error", "C5")], "Pos Error [m]", "EE Position Error"),
    ]
    for ax, series, ylabel, title in panels:
        for y, label, fmt in series:
            ax.plot(times, y, fmt, label=label, linewidth=1.2)
        _shade_releases(ax, release_spans_time)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("Time (s)")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_motion.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] motion    → {path}")

def _plot_torques(times, torque_history, release_spans_time, ep_idx, save_dir):
    times = np.asarray(times)
    torques = np.asarray([t[0] for t in torque_history])
    torques_dot = np.asarray([t[1] for t in torque_history])

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Torques  (orange = gripper release)", fontsize=12)

    labels = [f"J{j + 1}" for j in range(torques.shape[1])]
    for j, label in enumerate(labels):
        axes[0].plot(times, torques[:, j], label=label, linewidth=1.2)
    _shade_releases(axes[0], release_spans_time)
    axes[0].set_ylabel("torque (N*m)")
    axes[0].legend(loc="upper left", ncol=4, fontsize=7)
    axes[0].grid(True, alpha=0.3)

    for j, label in enumerate(labels):
        axes[1].plot(times, torques_dot[:, j], label=label, linewidth=1.2)
    _shade_releases(axes[1], release_spans_time)
    axes[1].set_ylabel("torque rate (N*m/s)")
    axes[1].set_xlabel("Time (s)")
    axes[1].legend(loc="upper left", ncol=4, fontsize=7)
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_torques.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] torques   → {path}")

def _plot_reward_parts(steps_buf, release_spans, ep_idx, save_dir):
    steps = np.asarray(steps_buf["steps"])
    keys = [k for k in steps_buf.keys() if k not in ["steps", "ee_z", "cuboid_rel_z", "desired_rel_z", "ft_dist", "lf_mag", "rf_mag", "reward"]]
    fig, axes = plt.subplots(len(keys), 1, figsize=(12, 2.5 * len(keys)), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Reward parts  (orange = gripper release)", fontsize=12)

    colors = [f"C{i}" for i in range(len(keys))]
    for ax, key, color in zip(axes, keys, colors):
        vals = np.asarray(steps_buf[key])
        ax.plot(steps, vals, color=color, linewidth=1.2, label=key)
        ax.axhline(0, color="k", linewidth=0.5, linestyle="--")
        _shade_releases(ax, release_spans)
        ax.set_ylabel(key, fontsize=8)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("High-level step")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_reward_parts.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] rew parts → {path}")

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
    parser.add_argument("--plot", action="store_true", default=False, help="Generate and save plots after evaluation")
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
    parser.add_argument(
        "--normalization",
        action="store_true",
        default=False,
        help="Enable fixed observation normalization (must match training setting)",
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
        normalize=args.normalization,
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
    position_error_history = []
    torque_history = []

    def _fresh_buffers():
        d = dict(steps=[], ee_z=[], cuboid_rel_z=[], desired_rel_z=[], ft_dist=[], lf_mag=[], rf_mag=[], reward=[])
        for k, _ in REWARD_PARTS:
            d[k] = []
        return d
    steps_buf = _fresh_buffers()
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
    post_pulse_hold_steps = max(1, int(round(0.5 / env.target_period)))
    post_pulse_hold_remaining = 0
    wait_for_post_pulse_direction_change = False
    prev_policy_z_vel_sign = 0

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
                unnorm_obs = env.get_observation()
                avg_force = (unnorm_obs["left_force_mag"] + unnorm_obs["right_force_mag"]) * 0.5
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
                policy_z_vel = action[0] * env.z_vel_max
                if policy_z_vel > 1e-4:
                    policy_z_vel_sign = 1
                elif policy_z_vel < -1e-4:
                    policy_z_vel_sign = -1
                else:
                    policy_z_vel_sign = 0

                if (
                    wait_for_post_pulse_direction_change
                    and post_pulse_hold_remaining == 0
                    and prev_policy_z_vel_sign != 0
                    and policy_z_vel_sign != 0
                    and policy_z_vel_sign != prev_policy_z_vel_sign
                ):
                    post_pulse_hold_remaining = post_pulse_hold_steps
                    wait_for_post_pulse_direction_change = False

                if policy_z_vel_sign != 0:
                    prev_policy_z_vel_sign = policy_z_vel_sign

                if post_pulse_hold_remaining > 0:
                    action = action.copy()
                    action[0] = 0.0
                    action[1:] = -1.0
                    post_pulse_hold_remaining -= 1

                pulse_steps_before = env._gripper_pulse_steps
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
                steps_buf["steps"].append(ep_len)
                steps_buf["ee_z"].append(unnorm_obs["ee_pos"])
                steps_buf["cuboid_rel_z"].append(unnorm_obs["cuboid_rel_z"])
                steps_buf["desired_rel_z"].append(unnorm_obs["desired_rel_z"])
                steps_buf["ft_dist"].append(env.get_fingertip_distance())
                steps_buf["lf_mag"].append(unnorm_obs["left_force_mag"])
                steps_buf["rf_mag"].append(unnorm_obs["right_force_mag"])
                steps_buf["reward"].append(reward)
                for key, _ in REWARD_PARTS:
                    steps_buf[key].append(float(env.last_reward_terms.get(key, 0.0)))

                if done:
                    if in_release and times:
                        release_spans_time.append((release_start_time, times[-1]))
                        release_spans_step.append((release_start_step, ep_len))
                        in_release = False
                    success = env.last_done_reason == "success"
                    ep_count += 1
                    result = "SUCCESS" if success else f"fail reason={env.last_done_reason or 'unknown'}"
                    print(f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  {result}")
                    
                    if args.plot and len(times) > 0:
                        save_dir = os.path.join(log_dir, "plots")
                        _plot_episode(steps_buf, release_spans_step, ep_count, save_dir)
                        _plot_motion(times, target_z_history, actual_z_history, target_z_vel_history, actual_z_vel_history, target_z_acc_history, actual_z_acc_history, position_error_history, release_spans_time, ep_count, save_dir)
                        _plot_torques(times, torque_history, release_spans_time, ep_count, save_dir)
                        _plot_reward_parts(steps_buf, release_spans_step, ep_count, save_dir)

                    steps_buf = _fresh_buffers()
                    times = []
                    target_z_history = []
                    actual_z_history = []
                    target_z_vel_history = []
                    actual_z_vel_history = []
                    target_z_acc_history = []
                    actual_z_acc_history = []
                    position_error_history = []
                    torque_history = []
                    release_spans_time = []
                    release_spans_step = []
                    in_release = False
                    ep_reward = 0.0
                    ep_len = 0
                    post_pulse_hold_remaining = 0
                    wait_for_post_pulse_direction_change = False
                    prev_policy_z_vel_sign = 0

                else:
                    if pulse_steps_before == 1:
                        wait_for_post_pulse_direction_change = True

                obs_flat = env.get_obs_flat()
                obs_td = obs_to_tensordict(obs_flat, device, torch, tensor_dict_cls)

                parts_str = "  ".join(
                    f"{k}={float(v):.3f}" for k, v in env.last_reward_terms.items()
                )
                print(f"current reward is {reward:.3f}  [{parts_str}]")
                
                # ---- per-step observation printout -----------------------
                unnorm_obs = env.get_observation()
                _o = unnorm_obs  # dictionary format, let's format it directly
                print(
                    f"  obs | "
                    f"ee_pos_z={_o['ee_pos']:+.4f}  "
                    f"ee_vel_z={_o['ee_vel']:+.4f}  "
                    f"target_z_vel={_o['target_z_vel']:+.4f}  "
                    f"target_z_acc={_o['target_z_acc']:+.4f}  "
                    f"left_force_mag={_o['left_force_mag']:+.3f}  "
                    f"right_force_mag={_o['right_force_mag']:+.3f}  "
                    f"cuboid_rel_z={_o['cuboid_rel_z']:+.4f}  "
                    f"cuboid_rel_x={_o['cuboid_rel_x']:+.4f}  "
                    f"cuboid_rel_y={_o['cuboid_rel_y']:+.4f}  "
                    f"desired_rel_z={_o['desired_rel_z']:+.4f}"
                )

                cuboid_z_error_step_times.append(times[-1] if times else 0.0)
                cuboid_z_error_vals.append(abs(unnorm_obs["cuboid_rel_z"] - unnorm_obs["desired_rel_z"]))
                
                if i % 100 == 0:
                    actual_z = unnorm_obs["ee_pos"]
                    actual_z_vel = unnorm_obs["ee_vel"]
                    lf_mag = unnorm_obs["left_force_mag"]
                    rf_mag = unnorm_obs["right_force_mag"]
                    cuboid_rel_z = unnorm_obs["cuboid_rel_z"]
                    desired_rel_z = unnorm_obs["desired_rel_z"]
                    ft_dist = env.get_fingertip_distance()
                    
                    z_acc = (
                        (actual_z_vel - prev_actual_z_vel) / env.target_period
                        if prev_actual_z_vel is not None
                        else 0.0
                    )
                    prev_actual_z_vel = actual_z_vel
                    print(
                        f"step {env.sim_step:6d}  z={actual_z:.4f}  z_vel={actual_z_vel:+.3f}  "
                        f"z_acc={z_acc:+.2f}  ft_dist={ft_dist:.4f}  "
                        f"|lf|={lf_mag:.3f}  |rf|={rf_mag:.3f}  "
                        f"cuboid_rel_z={cuboid_rel_z:+.4f}  desired={desired_rel_z:+.4f}  "
                        f"err={abs(cuboid_rel_z - desired_rel_z)*1000:.2f}mm  "
                        f"ep_rew={ep_reward:.1f}  z_vel={actual_z_vel:+.3f}"
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



    print(f"Episode length: {ep_len} high-level steps ({ep_len * env.target_update_every} sim steps)")

    if args.save and z_vel_output:
        save_path = Path(args.save_file).expanduser()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(save_path, np.array(z_vel_output, dtype=np.float32))
        print(f"Saved z_vel output ({len(z_vel_output)} steps) to {save_path.resolve()}")


if __name__ == "__main__":
    main()

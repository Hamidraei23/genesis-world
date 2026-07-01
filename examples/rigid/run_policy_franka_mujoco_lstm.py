"""
MuJoCo rollout for a trained Franka PPO+LSTM policy.

This is the MuJoCo equivalent of run_policy_franka_parallel_lstm.py. It reuses
ObservationHistoryWrapper semantics by building the sequence window in this
script and can enable env-level --zero behavior.

Usage:
    python3 examples/rigid/run_policy_franka_mujoco_lstm.py -e franka-lift-v1-lstm --ckpt 660 --vis
    python3 examples/rigid/run_policy_franka_mujoco_lstm.py -e franka-lift-v1-lstm --ckpt 660 --plot --normalization --zero
"""

import argparse
import os
import pickle
from importlib import metadata

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    if int(metadata.version("rsl-rl-lib").split(".")[0]) < 5:
        raise ImportError
except (metadata.PackageNotFoundError, ImportError, ValueError) as e:
    raise ImportError("Please install 'rsl-rl-lib>=5.0.0'.") from e

import torch
from tensordict import TensorDict
from rsl_rl.runners import OnPolicyRunner

try:
    from .env_franka_mujoco import FrankaMuJoCoEnv
except ImportError:
    from env_franka_mujoco import FrankaMuJoCoEnv

# Import model symbols so rsl_rl can deserialize checkpoints that reference
# this callable path.
try:
    from .train_franka_ppo_lstm import SequenceBidirectionalLSTMModel, SEQUENCE_LENGTH  # noqa: F401
except ImportError:
    from train_franka_ppo_lstm import SequenceBidirectionalLSTMModel, SEQUENCE_LENGTH  # noqa: F401


class _RunnerLSTMEnvProxy:
    """Minimal env shim for OnPolicyRunner model reconstruction."""

    num_envs = 1
    num_actions = 3
    extras = {}
    cfg = {}

    def _obs_td(self):
        obs = torch.zeros(1, SEQUENCE_LENGTH, FrankaMuJoCoEnv.OBS_DIM)
        return TensorDict({"policy": obs}, batch_size=[1])

    def get_observations(self):
        return self._obs_td()

    def reset(self):
        return self._obs_td()

    def step(self, _actions):
        return self._obs_td(), torch.zeros(1), torch.zeros(1, dtype=torch.bool), {}


def _resolve_checkpoint(log_dir: str, ckpt: int | None) -> str:
    if ckpt is not None:
        return os.path.join(log_dir, f"model_{ckpt}.pt")

    pts = [f for f in os.listdir(log_dir) if f.startswith("model_") and f.endswith(".pt")]
    if not pts:
        raise FileNotFoundError(f"No model_*.pt found in {log_dir}")
    pts.sort(key=lambda f: int(f.split("_")[1].split(".")[0]))
    return os.path.join(log_dir, pts[-1])


def _history_to_tensordict(history: np.ndarray, device: torch.device) -> TensorDict:
    obs = torch.tensor(history, dtype=torch.float32, device=device).unsqueeze(0)
    return TensorDict({"policy": obs}, batch_size=[1])


REWARD_PART_KEYS = (
    "base_reward",
    "regrasp_bonus",
    "jerk_penalty",
    "success_candidate",
    "success_steps",
)
REGRASP_PLOT_KEYS = ("z_improvement", "regrasp_bonus")
ROLLOUT_TERM_KEYS = tuple(dict.fromkeys((*REWARD_PART_KEYS, *REGRASP_PLOT_KEYS)))


def _reward_term_scalar(value):
    if value is None:
        return None
    if torch.is_tensor(value):
        t = value.detach()
        if t.numel() == 0:
            return None
        return float(t.reshape(-1)[0].item())
    arr = np.asarray(value)
    if arr.size == 0:
        return None
    return float(arr.reshape(-1)[0])


def _reward_term_float(value, default: float = 0.0) -> float:
    scalar = _reward_term_scalar(value)
    return default if scalar is None else float(scalar)


def _format_reward_term(key: str, value) -> str:
    scalar = _reward_term_scalar(value)
    if scalar is not None:
        return f"{key}={scalar:.3f}"
    return f"{key}=None"


def _fresh_buffers():
    d = dict(
        steps=[], ee_z=[], cuboid_rel_z=[], desired_rel_z=[],
        ft_dist=[], lf_mag=[], rf_mag=[], reward=[],
        target_z=[], actual_z_vel=[], target_z_vel=[],
        target_z_acc=[], actual_z_acc=[], z_error=[],
    )
    for k in ROLLOUT_TERM_KEYS:
        d[k] = []
    return d


def _shade_releases(ax, release_spans):
    for idx, (s0, s1) in enumerate(release_spans):
        ax.axvspan(s0, s1, color="orange", alpha=0.25, label="release window" if idx == 0 else "")


def _plot_episode(bufs, release_spans, ep_idx, save_dir):
    steps = np.asarray(bufs["steps"])
    ee_z = np.asarray(bufs["ee_z"])
    cub_rel_z = np.asarray(bufs["cuboid_rel_z"])
    des_rel_z = np.asarray(bufs["desired_rel_z"])
    ft_dist = np.asarray(bufs["ft_dist"])
    lf_mag = np.asarray(bufs["lf_mag"])
    rf_mag = np.asarray(bufs["rf_mag"])
    reward = np.asarray(bufs["reward"])
    cum_reward = np.cumsum(reward)

    fig, axes = plt.subplots(5, 1, figsize=(12, 14), sharex=True)
    fig.suptitle(f"Episode {ep_idx} -- Overview  (orange = gripper release)", fontsize=12)

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
    print(f"  [PLOT] overview  -> {path}")


def _plot_motion(bufs, release_spans, ep_idx, save_dir):
    steps = np.asarray(bufs["steps"])
    ee_z = np.asarray(bufs["ee_z"])
    target_z = np.asarray(bufs["target_z"])
    actual_z_vel = np.asarray(bufs["actual_z_vel"])
    target_z_vel = np.asarray(bufs["target_z_vel"])
    actual_z_acc = np.asarray(bufs["actual_z_acc"])
    target_z_acc = np.asarray(bufs["target_z_acc"])
    z_error = np.asarray(bufs["z_error"])
    cub_rel_z = np.asarray(bufs["cuboid_rel_z"])
    des_rel_z = np.asarray(bufs["desired_rel_z"])
    ft_dist = np.asarray(bufs["ft_dist"])

    fig, axes = plt.subplots(6, 1, figsize=(12, 16), sharex=True)
    fig.suptitle(f"Episode {ep_idx} -- Motion detail  (orange = gripper release)", fontsize=12)

    panels = [
        (axes[0], [(ee_z, "actual ee_z", "C0"), (target_z, "target z", "C3--")], "Z position [m]", "EE Z position (actual vs target)"),
        (axes[1], [(actual_z_vel, "actual z_vel", "C0"), (target_z_vel, "target z_vel", "C3--")], "Z velocity [m/s]", "Z velocity (actual vs target)"),
        (axes[2], [(actual_z_acc, "actual z_acc", "C0"), (target_z_acc, "target z_acc", "C3--")], "Z accel [m/s^2]", "Z acceleration (actual vs commanded)"),
        (axes[3], [(z_error, "z_error", "C5")], "|cuboid_rel_z - desired| [m]", "Object tracking error"),
        (axes[4], [(cub_rel_z, "cuboid_rel_z", "C0"), (des_rel_z, "desired_rel_z", "C3--")], "Z relative [m]", "Cuboid in-hand Z vs target"),
        (axes[5], [(ft_dist, "fingertip_dist", "C2")], "Distance [m]", "Fingertip distance"),
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
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_motion.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] motion    -> {path}")


def _plot_reward_parts(bufs, release_spans, ep_idx, save_dir):
    steps = np.asarray(bufs["steps"])
    fig, axes = plt.subplots(len(REWARD_PART_KEYS), 1, figsize=(12, 2.5 * len(REWARD_PART_KEYS)), sharex=True)
    fig.suptitle(f"Episode {ep_idx} -- Reward parts  (orange = gripper release)", fontsize=12)

    colors = [f"C{i}" for i in range(len(REWARD_PART_KEYS))]
    for ax, key, color in zip(axes, REWARD_PART_KEYS, colors):
        vals = np.asarray(bufs[key])
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
    print(f"  [PLOT] rew parts -> {path}")


def _plot_regrasp(bufs, release_spans, ep_idx, save_dir):
    steps = np.asarray(bufs["steps"])
    z_improvement = np.asarray(bufs["z_improvement"])
    regrasp_bonus = np.asarray(bufs["regrasp_bonus"])

    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    fig.suptitle(f"Episode {ep_idx} -- Regrasp detail  (orange = gripper release)", fontsize=12)

    panels = [
        (axes[0], z_improvement, "z_improvement", "Z improvement [m]", "C0"),
        (axes[1], regrasp_bonus, "regrasp_bonus", "Reward", "C1"),
    ]
    for ax, vals, label, ylabel, color in panels:
        ax.plot(steps, vals, color=color, linewidth=1.2, label=label)
        ax.axhline(0, color="k", linewidth=0.5, linestyle="--")
        _shade_releases(ax, release_spans)
        ax.set_ylabel(ylabel)
        ax.set_title(label, fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("High-level step")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_regrasp.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] regrasp  -> {path}")


def _save_all_plots(bufs, release_spans, ep_idx, save_dir):
    _plot_episode(bufs, release_spans, ep_idx, save_dir)
    _plot_motion(bufs, release_spans, ep_idx, save_dir)
    _plot_reward_parts(bufs, release_spans, ep_idx, save_dir)
    _plot_regrasp(bufs, release_spans, ep_idx, save_dir)


def _append_rollout_sample(bufs, obs, env, step_idx, reward_value, reward_terms, target_z, actual_z_acc):
    lf_mag = float(obs["left_force_mag"])
    rf_mag = float(obs["right_force_mag"])
    actual_z_vel = float(obs["ee_vel"])
    cub_rel_z = float(obs["cuboid_rel_z"])
    des_rel_z = float(obs["desired_rel_z"])
    ft_dist = float(env.get_fingertip_distance())

    bufs["steps"].append(step_idx)
    bufs["ee_z"].append(float(obs["ee_pos"]))
    bufs["cuboid_rel_z"].append(cub_rel_z)
    bufs["desired_rel_z"].append(des_rel_z)
    bufs["ft_dist"].append(ft_dist)
    bufs["lf_mag"].append(lf_mag)
    bufs["rf_mag"].append(rf_mag)
    bufs["reward"].append(float(reward_value))
    bufs["target_z"].append(float(target_z))
    bufs["actual_z_vel"].append(actual_z_vel)
    bufs["target_z_vel"].append(float(obs["target_z_vel"]))
    bufs["target_z_acc"].append(float(obs["target_z_acc"]))
    bufs["actual_z_acc"].append(float(actual_z_acc))
    bufs["z_error"].append(abs(cub_rel_z - des_rel_z))
    for rk in ROLLOUT_TERM_KEYS:
        bufs[rk].append(_reward_term_float(reward_terms.get(rk)))

    return {
        "actual_z": bufs["ee_z"][-1],
        "actual_z_vel": actual_z_vel,
        "avg_force": (lf_mag + rf_mag) * 0.5,
        "cuboid_rel_z": cub_rel_z,
        "desired_rel_z": des_rel_z,
        "ft_dist": ft_dist,
        "lf_mag": lf_mag,
        "rf_mag": rf_mag,
    }


def main():
    parser = argparse.ArgumentParser(description="MuJoCo rollout for Franka PPO+LSTM policy")
    parser.add_argument("-e", "--exp_name", type=str, default="franka-lift-v1-lstm")
    parser.add_argument("--ckpt", type=int, default=None)
    parser.add_argument("--vis", action="store_true", default=False)
    parser.add_argument("--plot", action="store_true", default=False)
    parser.add_argument("--steps", type=int, default=10000)
    parser.add_argument("--dt", type=float, default=0.001)
    parser.add_argument("--target_dt", type=float, default=0.02)
    parser.add_argument("--playback-speed", type=float, default=0.5)
    parser.add_argument("--negative", action="store_true", default=False)
    parser.add_argument("--normalization", action="store_true", default=False)
    parser.add_argument("--randomize", action="store_true", default=False)
    parser.add_argument("--limit-regrasp", action="store_true", default=False)
    parser.add_argument("--zero", action="store_true", default=False,
                        help="Enable post-pulse zero-z-velocity + close-gripper hold in env")
    args = parser.parse_args()

    if args.playback_speed <= 0.0:
        raise ValueError("--playback-speed must be greater than 0")

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
        randomize=args.randomize,
        limit_regrasp=args.limit_regrasp,
        zero=args.zero,
    )

    runner = OnPolicyRunner(_RunnerLSTMEnvProxy(), train_cfg, log_dir, device=device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=device)

    obs_flat = env.get_obs_flat()
    obs_history = np.repeat(obs_flat[None, :], SEQUENCE_LENGTH, axis=0)
    obs_td = _history_to_tensordict(obs_history, device)

    high_level_steps = args.steps if args.steps > 0 else float("inf")
    ep_reward = 0.0
    ep_len = 0
    ep_count = 0
    prev_actual_z_vel = None

    bufs = _fresh_buffers()
    release_spans: list[tuple[int, int]] = []
    in_release = False
    release_start = 0
    force_thresh = env.REGRASP_FORCE_THRESHOLD

    print(
        f"target_dt={env.target_period:.3f}s  sim_dt={env.dt:.3f}s  "
        f"steps_per_target={env.target_update_every}  playback_speed={args.playback_speed:.2f}x  "
        f"sequence_length={SEQUENCE_LENGTH}"
    )

    i = 0
    try:
        with torch.no_grad():
            while i < high_level_steps:
                unnorm_obs = env.get_observation()
                target_z = float(env.target_z)
                actual_z_vel_cur = float(unnorm_obs["ee_vel"])
                actual_z_acc_cur = (
                    (actual_z_vel_cur - prev_actual_z_vel) / env.target_period
                    if prev_actual_z_vel is not None else 0.0
                )
                prev_actual_z_vel = actual_z_vel_cur

                action = policy(obs_td).cpu().numpy().reshape(-1)
                _, reward, done = env.step(action)

                ep_reward += reward
                ep_len += 1

                obs_flat = env.get_obs_flat()
                obs_history = np.roll(obs_history, shift=-1, axis=0)
                obs_history[-1, :] = obs_flat
                if done:
                    obs_history[:] = obs_flat

                obs_td = _history_to_tensordict(obs_history, device)

                parts_str = "  ".join(_format_reward_term(k, v) for k, v in env.last_reward_terms.items())
                print(f"current reward is {reward:.3f}  [{parts_str}]")

                sample = _append_rollout_sample(
                    bufs,
                    unnorm_obs,
                    env,
                    ep_len,
                    reward,
                    env.last_reward_terms,
                    target_z,
                    actual_z_acc_cur,
                )

                currently_released = sample["avg_force"] < force_thresh
                if currently_released and not in_release:
                    in_release = True
                    release_start = ep_len
                elif not currently_released and in_release:
                    release_spans.append((release_start, ep_len))
                    in_release = False

                if done:
                    if in_release:
                        release_spans.append((release_start, ep_len))
                        in_release = False

                    terminal_status = "SUCCESS" if env.last_done_reason == "success" else f"FAIL ({env.last_done_reason})"
                    ep_count += 1
                    print(f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  {terminal_status}")

                    if args.plot:
                        _save_all_plots(bufs, release_spans, ep_count, save_dir=os.path.join(log_dir, "plots"))

                    bufs = _fresh_buffers()
                    release_spans = []
                    in_release = False
                    ep_reward = 0.0
                    ep_len = 0
                    prev_actual_z_vel = None

                i += 1

    except KeyboardInterrupt:
        print("\nStopped by user.")
        if args.plot and bufs["steps"]:
            if in_release:
                release_spans.append((release_start, ep_len))
            ep_count += 1
            _save_all_plots(bufs, release_spans, ep_count, save_dir=os.path.join(log_dir, "plots"))
    finally:
        env.close()


if __name__ == "__main__":
    main()

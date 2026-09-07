"""
Visualised policy rollout for a trained FrankaEnvParallel PPO policy.

Runs a single env with the interactive viewer (or records an MP4), printing
per-step obs diagnostics every 100 high-level steps — same style as
run_env_franka_trajectory_parallel.py, but driven by the trained policy
instead of a hand-crafted trajectory.

Usage:
    # Interactive viewer, latest checkpoint
    python3 examples/rigid/run_policy_franka_parallel.py -e franka-lift-v1

    # Specific checkpoint iteration
    python3 examples/rigid/run_policy_franka_parallel.py -e franka-lift-v1 --ckpt 500

    # Headless (no viewer)
    python3 examples/rigid/run_policy_franka_parallel.py -e franka-lift-v1 --no-vis

    # Record an MP4
    python3 examples/rigid/run_policy_franka_parallel.py -e franka-lift-v1 --record

    # Use the 2H observation layout and 2H-control checkpoints
    python3 examples/rigid/run_policy_franka_parallel.py -e franka-lift-v1 --2H

Checkpoints are loaded from:
    logs/<exp_name>/model_<iter>.pt
"""

import argparse
import os
import pickle
import time
from importlib import metadata

import matplotlib
matplotlib.use("Agg")  # headless – required inside Docker (no $DISPLAY)
import matplotlib.pyplot as plt
import numpy as np
import torch

try:
    if int(metadata.version("rsl-rl-lib").split(".")[0]) < 5:
        raise ImportError
except (metadata.PackageNotFoundError, ImportError, ValueError) as e:
    raise ImportError("Please install 'rsl-rl-lib>=5.0.0'.") from e
from rsl_rl.runners import OnPolicyRunner

import genesis as gs

try:
    from .env_franka_parallel import FrankaEnvParallel
except ImportError:
    from env_franka_parallel import FrankaEnvParallel

try:
    from .env_franka_parallel_backup_2h import FrankaEnvParallel as FrankaEnvParallel2H
except ImportError:
    from env_franka_parallel_backup_2h import FrankaEnvParallel as FrankaEnvParallel2H


def main():
    parser = argparse.ArgumentParser(description="Visualised PPO policy rollout for Franka")
    parser.add_argument("-e", "--exp_name", type=str, default="franka-lift",
                        help="Experiment name matching the training run")
    parser.add_argument("--ckpt", type=int, default=None,
                        help="Checkpoint iteration (e.g. 500). Defaults to latest.")
    parser.add_argument("--no-vis", dest="vis", action="store_false", default=True,
                        help="Disable interactive viewer (headless)")
    parser.add_argument("--record", action="store_true", default=False,
                        help="Record offscreen video to franka_policy.mp4. Forces --no-vis.")
    parser.add_argument("--steps", type=int, default=10000,
                        help="Total high-level steps to run (0 = run until Ctrl-C)")
    parser.add_argument("--dt", type=float, default=0.001)
    parser.add_argument("--target_dt", type=float, default=0.02)
    parser.add_argument("--replay-speed", type=float, default=4.0,
                        help="Viewer playback speed multiplier. Use 1.0 for real time.")
    parser.add_argument("--render-every", type=int, default=1,
                        help="Refresh the interactive viewer every N high-level policy steps.")
    parser.add_argument("--plot", action="store_true", default=False,
                        help="Generate and save a diagnostic plot after each episode")
    parser.add_argument("--limit-regrasp", "--limit-grasp", dest="limit_regrasp", action="store_true",
                        help="Terminate an episode as failure on the 4th regrasp")
    parser.add_argument("--negative", action="store_true", default=False,
                        help="Use negative desired_rel_z (solid-up mode). Pass when evaluating a --negative-trained checkpoint.")
    parser.add_argument("--normalization", action="store_true", default=False,
                        help="Enable fixed observation normalization (must match training setting)")
    parser.add_argument("--randomize", action="store_true", default=False,
                        help="Enable domain randomization (must match training setting)")
    parser.add_argument("--zero", action="store_true", default=False,
                        help="After a gripper pulse, temporarily zero z velocity and close the gripper")
    parser.add_argument("--control-error", action="store_true", default=False,
                        help="Add per-episode constant z-velocity command bias in [-0.05, -0.03] U [0.03, 0.05]")
    parser.add_argument("--2H", dest="use_2h", action="store_true", default=False,
                        help="Use env_franka_parallel_backup_2h and logs/<exp_name>-2H-control")
    args = parser.parse_args()

    if args.record:
        args.vis = False
    if args.replay_speed <= 0.0:
        raise ValueError("--replay-speed must be greater than 0")
    if args.render_every <= 0:
        raise ValueError("--render-every must be greater than 0")

    log_dir = f"logs/{args.exp_name}"
    env_cls = FrankaEnvParallel
    if args.use_2h:
        env_cls = FrankaEnvParallel2H
        log_dir = f"{log_dir}-2H-control"

    # ---- resolve checkpoint -----------------------------------------------
    if args.ckpt is not None:
        ckpt_path = os.path.join(log_dir, f"model_{args.ckpt}.pt")
    else:
        pts = [f for f in os.listdir(log_dir) if f.startswith("model_") and f.endswith(".pt")]
        if not pts:
            raise FileNotFoundError(f"No model_*.pt found in {log_dir}")
        pts.sort(key=lambda f: int(f.split("_")[1].split(".")[0]))
        ckpt_path = os.path.join(log_dir, pts[-1])
    print(f"Loading checkpoint: {ckpt_path}")

    # ---- load training config ---------------------------------------------
    with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
        train_cfg = pickle.load(f)

    # ---- Genesis + env ----------------------------------------------------
    gs.init(backend=gs.gpu, precision="32", logging_level="warning")

    # record=True tells the env to add the camera BEFORE scene.build()
    obs_cls = env_cls

    env = env_cls(
        num_envs=1,
        vis=args.vis,
        record=args.record,
        dt=args.dt,
        target_dt=args.target_dt,
        limit_regrasp=args.limit_regrasp,
        solid_up=args.negative,
        normalize=args.normalization,
        randomize=args.randomize,
        control_error=args.control_error,
    )

    # ---- load policy ------------------------------------------------------
    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=gs.device)

    if args.record:
        env.cam.start_recording()

    # reward-part keys logged by the env each step
    REWARD_PART_KEYS = (
        "z_track", "centering", "grip_force",
        "jerk_penalty", "z_acc_penalty", "ee_z_penalty",
        "regrasp_bonus", "no_regrasp_penalty",
    )
    REGRASP_PLOT_KEYS = ("z_improvement", "regrasp_bonus")
    ROLLOUT_TERM_KEYS = tuple(dict.fromkeys((*REWARD_PART_KEYS, *REGRASP_PLOT_KEYS)))

    def _obs_scale_for(obs: torch.Tensor) -> torch.Tensor:
        """Return fixed normalization scales aligned to the active observation width."""
        scale = torch.tensor(FrankaEnvParallel.OBS_SCALE, device=obs.device, dtype=obs.dtype)
        obs_dim = obs.shape[-1]
        if scale.numel() == obs_dim:
            return scale
        if scale.numel() > obs_dim:
            return scale[:obs_dim]
        pad = torch.ones(obs_dim - scale.numel(), device=obs.device, dtype=obs.dtype)
        return torch.cat([scale, pad], dim=0)

    def _reward_term_scalar(value, env_index: int = 0):
        """Extract one scalar for this env; return None for vector/matrix debug tensors."""
        if value is None:
            return None
        if torch.is_tensor(value):
            t = value.detach()
            if t.numel() == 0:
                return None
            if t.ndim > 0:
                t = t[env_index] if t.shape[0] > env_index else t.reshape(-1)[0]
            return t.reshape(()).item() if t.numel() == 1 else None

        arr = np.asarray(value)
        if arr.size == 0:
            return None
        if arr.ndim > 0:
            arr = arr[env_index] if arr.shape[0] > env_index else arr.reshape(-1)[0]
        return float(arr.reshape(())) if arr.size == 1 else None

    def _reward_term_float(value, default: float = 0.0) -> float:
        scalar = _reward_term_scalar(value)
        return default if scalar is None else float(scalar)

    def _format_reward_term(key: str, value) -> str:
        scalar = _reward_term_scalar(value)
        if scalar is not None:
            return f"{key}={float(scalar):.3f}"
        if torch.is_tensor(value):
            return f"{key}=tensor{tuple(value.shape)}"
        return f"{key}=array{np.asarray(value).shape}"

    # ---- per-episode data buffers ----------------------------------------
    def _fresh_buffers():
        d = dict(
            steps=[], ee_z=[], cuboid_rel_z=[], desired_rel_z=[],
            ft_dist=[], avg_force=[], reward=[],
            # motion detail
            target_z=[], actual_z_vel=[], target_z_vel=[],
            target_z_acc=[], actual_z_acc=[], z_error=[],
        )
        for k in ROLLOUT_TERM_KEYS:
            d[k] = []
        return d

    def _shade_releases(ax, release_spans):
        """Add orange axvspan for every release window on an axes."""
        for idx, (s0, s1) in enumerate(release_spans):
            ax.axvspan(s0, s1, color="orange", alpha=0.25,
                       label="release window" if idx == 0 else "")

    def _plot_episode(bufs, release_spans, ep_idx, save_dir):
        """Plot 1 – overview: cuboid Z, EE Z, fingertip dist, average force, reward."""
        steps      = np.asarray(bufs["steps"])
        ee_z       = np.asarray(bufs["ee_z"])
        cub_rel_z  = np.asarray(bufs["cuboid_rel_z"])
        des_rel_z  = np.asarray(bufs["desired_rel_z"])
        ft_dist    = np.asarray(bufs["ft_dist"])
        avg_force  = np.asarray(bufs["avg_force"])
        reward     = np.asarray(bufs["reward"])
        cum_reward = np.cumsum(reward)

        fig, axes = plt.subplots(5, 1, figsize=(12, 14), sharex=True)
        fig.suptitle(f"Episode {ep_idx} — Overview  (orange = gripper release)", fontsize=12)

        panels = [
            (axes[0], [(cub_rel_z, "cuboid_rel_z", "C0"), (des_rel_z, "desired_rel_z", "C3--")],
             "Z relative [m]", "Cuboid in-hand Z"),
            (axes[1], [(ee_z, "ee_pos_z", "C1")],
             "EE height [m]", "End-effector Z"),
            (axes[2], [(ft_dist, "fingertip_dist", "C2")],
             "Distance [m]", "Fingertip distance"),
            (axes[3], [(avg_force, "avg_force", "C4")],
             "Force [N]", "Average finger contact force"),
            (axes[4], [(reward, "reward/step", "C6"), (cum_reward, "cumulative", "C7")],
             "Reward", "Reward"),
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

    def _plot_motion(bufs, release_spans, ep_idx, save_dir):
        """Plot 2 – motion detail: Z pos/vel/acc, z_error, cuboid tracking, fingertip."""
        steps        = np.asarray(bufs["steps"])
        ee_z         = np.asarray(bufs["ee_z"])
        target_z     = np.asarray(bufs["target_z"])
        actual_z_vel = np.asarray(bufs["actual_z_vel"])
        target_z_vel = np.asarray(bufs["target_z_vel"])
        actual_z_acc = np.asarray(bufs["actual_z_acc"])
        target_z_acc = np.asarray(bufs["target_z_acc"])
        z_error      = np.asarray(bufs["z_error"])
        cub_rel_z    = np.asarray(bufs["cuboid_rel_z"])
        des_rel_z    = np.asarray(bufs["desired_rel_z"])
        ft_dist      = np.asarray(bufs["ft_dist"])

        fig, axes = plt.subplots(6, 1, figsize=(12, 16), sharex=True)
        fig.suptitle(f"Episode {ep_idx} — Motion detail  (orange = gripper release)", fontsize=12)

        panels = [
            (axes[0], [(ee_z,         "actual ee_z",    "C0"),
                       (target_z,     "target z",       "C3--")],
             "Z position [m]",      "EE Z position (actual vs target)"),
            (axes[1], [(actual_z_vel, "actual z_vel",   "C0"),
                       (target_z_vel, "target z_vel",   "C3--")],
             "Z velocity [m/s]",    "Z velocity (actual vs target)"),
            (axes[2], [(actual_z_acc, "actual z_acc",   "C0"),
                       (target_z_acc, "target z_acc",   "C3--")],
             "Z accel [m/s²]",      "Z acceleration (actual vs commanded)"),
            (axes[3], [(z_error,      "z_error",         "C5")],
             "|cuboid_rel_z − desired| [m]", "Object tracking error"),
            (axes[4], [(cub_rel_z,   "cuboid_rel_z",    "C0"),
                       (des_rel_z,   "desired_rel_z",   "C3--")],
             "Z relative [m]",      "Cuboid in-hand Z vs target"),
            (axes[5], [(ft_dist,     "fingertip_dist",  "C2")],
             "Distance [m]",        "Fingertip distance"),
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
        print(f"  [PLOT] motion    → {path}")

    def _plot_reward_parts(bufs, release_spans, ep_idx, save_dir):
        """Plot 3 – one panel per reward term, with shaded release windows."""
        steps = np.asarray(bufs["steps"])
        fig, axes = plt.subplots(len(REWARD_PART_KEYS), 1,
                                 figsize=(12, 2.5 * len(REWARD_PART_KEYS)), sharex=True)
        fig.suptitle(f"Episode {ep_idx} — Reward parts  (orange = gripper release)", fontsize=12)

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
        print(f"  [PLOT] rew parts → {path}")

    def _plot_regrasp(bufs, release_spans, ep_idx, save_dir):
        """Plot 4 – regrasp-specific metrics."""
        steps = np.asarray(bufs["steps"])
        z_improvement = np.asarray(bufs["z_improvement"])
        regrasp_bonus = np.asarray(bufs["regrasp_bonus"])

        fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        fig.suptitle(f"Episode {ep_idx} — Regrasp detail  (orange = gripper release)", fontsize=12)

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
        print(f"  [PLOT] regrasp  → {path}")

    def _save_all_plots(bufs, release_spans, ep_idx, save_dir):
        _plot_episode(bufs, release_spans, ep_idx, save_dir)
        _plot_motion(bufs, release_spans, ep_idx, save_dir)
        _plot_reward_parts(bufs, release_spans, ep_idx, save_dir)
        _plot_regrasp(bufs, release_spans, ep_idx, save_dir)

    def _append_rollout_sample(bufs, obs, env, step_idx, reward_value, reward_terms, target_z, actual_z_acc):
        """
        Append one sample from the current reset-to-reset segment.

        The env resets internally before returning observations on a terminal step, so callers pass the
        pre-step observation here and attach the reward produced by that step.
        """
        avg_force = _reward_term_float(reward_terms.get("avg_force"))

        actual_z_vel = obs[0, obs_cls.OBS_EE_VEL_Z].item()
        cub_rel_z    = obs[0, obs_cls.OBS_CUBOID_REL_Z].item()
        des_rel_z    = obs[0, obs_cls.OBS_DESIRED_REL_Z].item()

        # Fingertip distance is not in the obs anymore; compute directly from env
        ft_dist = float(env.get_fingertip_distance())

        bufs["steps"].append(step_idx)
        bufs["ee_z"].append(obs[0, FrankaEnvParallel.OBS_EE_POS_Z].item())
        bufs["cuboid_rel_z"].append(cub_rel_z)
        bufs["desired_rel_z"].append(des_rel_z)
        bufs["ft_dist"].append(ft_dist)
        bufs["avg_force"].append(avg_force)
        bufs["reward"].append(reward_value)
        # motion detail
        bufs["target_z"].append(target_z)
        bufs["actual_z_vel"].append(actual_z_vel)
        bufs["target_z_vel"].append(obs[0, obs_cls.OBS_TARGET_Z_VEL].item())
        bufs["target_z_acc"].append(obs[0, obs_cls.OBS_TARGET_Z_ACC].item())
        bufs["actual_z_acc"].append(actual_z_acc)
        bufs["z_error"].append(abs(cub_rel_z - des_rel_z))
        # reward parts
        for rk in ROLLOUT_TERM_KEYS:
            v = reward_terms.get(rk)
            bufs[rk].append(_reward_term_float(v))

        return {
            "actual_z": bufs["ee_z"][-1],
            "actual_z_vel": actual_z_vel,
            "avg_force": avg_force,
            "cuboid_rel_z": cub_rel_z,
            "desired_rel_z": des_rel_z,
            "ft_dist": ft_dist,
        }

    # ---- run loop ---------------------------------------------------------
    obs_td = env.reset()

    high_level_steps = args.steps if args.steps > 0 else float("inf")

    ep_reward = 0.0
    ep_len = 0
    ep_count = 0
    prev_actual_z_vel = None   # for every-step z_acc calculation
    prev_print_z_vel  = None   # for the 100-step print block only
    t_real_start = time.perf_counter()

    bufs = _fresh_buffers()
    # release-window tracking for current episode
    release_spans: list[tuple[int, int]] = []
    _in_release = False
    _release_start = 0
    _force_thresh = env.REGRASP_FORCE_THRESHOLD
    post_pulse_hold_steps = max(1, int(round(0.5 / env.target_period)))
    post_pulse_hold_remaining = 0
    wait_for_post_pulse_direction_change = False
    prev_policy_z_vel_sign = 0

    print(
        f"target_dt={env.target_period:.3f}s  sim_dt={env.dt:.3f}s  "
        f"steps_per_target={env.target_update_every}  replay_speed={args.replay_speed:.2f}x  "
        f"render_every={args.render_every}"
    )
    print(f"Running {'forever' if args.steps == 0 else high_level_steps} high-level steps...")

    i = 0
    try:
        with torch.no_grad():
            while i < high_level_steps:
                # Capture the observation before env.step(), because step() may reset and return the
                # next episode's first observation when reset_buf is true.
                obs = obs_td["policy"].clone()  # (1, OBS_DIM)
                if env.normalize:
                    unnorm_obs = obs * _obs_scale_for(obs)
                else:
                    unnorm_obs = obs

                target_z = env.target_z[0].item()
                actual_z_vel_cur = unnorm_obs[0, obs_cls.OBS_EE_VEL_Z].item()
                actual_z_acc_cur = (
                    (actual_z_vel_cur - prev_actual_z_vel) / env.target_period
                    if prev_actual_z_vel is not None else 0.0
                )
                prev_actual_z_vel = actual_z_vel_cur

                actions = policy(obs_td)
                pulse_steps_before = None
                if args.zero:
                    policy_z_vel = actions[0, 0].item() * env.Z_VEL_MAX
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
                        actions = actions.clone()
                        actions[:, 0] = 0.0
                        actions[:, 1:] = -1.0
                        post_pulse_hold_remaining -= 1

                    pulse_steps_before = env._gripper_pulse_steps[0].item()
                obs_td, rew_buf, reset_buf, _ = env.step(actions, update_visualizer=not args.vis)

                reward_cur = rew_buf[0].item()
                parts_str = "  ".join(
                    _format_reward_term(k, v) for k, v in env.last_reward_terms.items()
                )
                print(f"current reward is {reward_cur:.3f}  [{parts_str}]")
                # ---- per-step observation printout -----------------------
                _o = unnorm_obs[0]  # (OBS_DIM,) pre-step unnormalized observation
                print(
                    f"  obs | "
                    f"ee_pos_z={_o[0]:+.4f}  "
                    f"ee_vel_z={_o[1]:+.4f}  "
                    f"target_z_vel={_o[2]:+.4f}  "
                    f"target_z_acc={_o[3]:+.4f}  "
                    f"cuboid_rel_z={_o[obs_cls.OBS_CUBOID_REL_Z]:+.4f}  "
                    f"cuboid_rel_x={_o[obs_cls.OBS_CUBOID_REL_X]:+.4f}  "
                    f"cuboid_rel_y={_o[obs_cls.OBS_CUBOID_REL_Y]:+.4f}  "
                    f"desired_rel_z={_o[obs_cls.OBS_DESIRED_REL_Z]:+.4f}"
                )
                ep_reward += reward_cur
                ep_len += 1

                if args.record:
                    env.cam.render()

                # ---- collect per-step data --------------------------------
                sample = _append_rollout_sample(
                    bufs, unnorm_obs, env, ep_len, reward_cur, env.last_reward_terms, target_z, actual_z_acc_cur
                )

                # ---- detect release windows (open → closed) ---------------
                currently_released = sample["avg_force"] < _force_thresh
                if currently_released and not _in_release:
                    _in_release = True
                    _release_start = ep_len
                elif not currently_released and _in_release:
                    release_spans.append((_release_start, ep_len))
                    _in_release = False

                # ---- episode end ------------------------------------------
                if reset_buf[0].item():
                    # close any open release window
                    if _in_release:
                        release_spans.append((_release_start, ep_len))
                        _in_release = False

                    success = _reward_term_float(env.last_reward_terms.get("success"), 0.0) > 0.5
                    fail = _reward_term_float(env.last_reward_terms.get("fail"), 0.0) > 0.5
                    timeout = _reward_term_float(env.last_reward_terms.get("timeout"), 0.0) > 0.5
                    if success:
                        outcome = "SUCCESS"
                    elif fail:
                        outcome = "fail"
                    elif timeout:
                        outcome = "timeout"
                    else:
                        outcome = "done"
                    ep_count += 1
                    print(
                        f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  "
                        f"regrasps={len(release_spans)}  "
                        f"{outcome}"
                    )

                    if args.plot and bufs["steps"]:
                        _save_all_plots(bufs, release_spans, ep_count,
                                        save_dir=os.path.join(log_dir, "plots"))

                    # reset buffers
                    bufs = _fresh_buffers()
                    release_spans = []
                    _in_release = False
                    ep_reward = 0.0
                    ep_len = 0
                    prev_actual_z_vel = None
                    prev_print_z_vel  = None
                    post_pulse_hold_remaining = 0
                    wait_for_post_pulse_direction_change = False
                    prev_policy_z_vel_sign = 0

                elif args.zero and pulse_steps_before == 1:
                    wait_for_post_pulse_direction_change = True

                if (not reset_buf[0].item()) and i % 100 == 0:
                    actual_z      = sample["actual_z"]
                    ft_dist       = sample["ft_dist"]
                    cuboid_rel_z  = sample["cuboid_rel_z"]
                    desired_rel_z = sample["desired_rel_z"]
                    z_acc = (
                        (actual_z_vel_cur - prev_print_z_vel) / env.target_period
                        if prev_print_z_vel is not None else 0.0
                    )
                    prev_print_z_vel = actual_z_vel_cur
                    print(
                        f"step {env.sim_step:6d}  z={actual_z:.4f}  z_vel={actual_z_vel_cur:+.3f}  "
                        f"z_acc={z_acc:+.2f}  ft_dist={ft_dist:.4f}  "
                        f"avg_force={sample['avg_force']:.3f}  "
                        f"cuboid_rel_z={cuboid_rel_z:+.4f}  desired={desired_rel_z:+.4f}  "
                        f"err={abs(cuboid_rel_z - desired_rel_z)*1000:.2f}mm  "
                        f"ep_rew={ep_reward:.1f}  z_vel={actual_z_vel_cur:+.3f}"
                    )

                # Refresh the viewer at the replay cadence, not on every 1 ms physics step.
                if args.vis and (i + 1) % args.render_every == 0:
                    t_sim = env.sim_step * env.dt / args.replay_speed
                    t_wall = time.perf_counter() - t_real_start
                    if t_wall < t_sim:
                        time.sleep(t_sim - t_wall)
                    env.scene.visualizer.update(force=False, auto=True)

                i += 1

    except KeyboardInterrupt:
        print("\nStopped by user.")
        # plot whatever episode data was collected so far
        if args.plot and bufs["steps"]:
            if _in_release:
                release_spans.append((_release_start, ep_len))
            ep_count += 1
            _save_all_plots(bufs, release_spans, ep_count,
                            save_dir=os.path.join(log_dir, "plots"))

    if args.record and env.cam is not None:
        env.cam.stop_recording(save_to_filename="franka_policy.mp4", fps=60)
        print("Saved franka_policy.mp4")


if __name__ == "__main__":
    main()

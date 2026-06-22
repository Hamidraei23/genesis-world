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
    args = parser.parse_args()

    if args.record:
        args.vis = False

    log_dir = f"logs/{args.exp_name}"

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
    env = FrankaEnvParallel(
        num_envs=1,
        vis=args.vis,
        record=args.record,
        dt=args.dt,
        target_dt=args.target_dt,
        limit_regrasp=args.limit_regrasp,
        solid_up=args.negative,
        normalize=args.normalization,
        randomize=args.randomize,
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

    # ---- per-episode data buffers ----------------------------------------
    def _fresh_buffers():
        d = dict(
            steps=[], ee_z=[], cuboid_rel_z=[], desired_rel_z=[],
            ft_dist=[], lf_mag=[], rf_mag=[], reward=[],
            # motion detail
            target_z=[], actual_z_vel=[], target_z_vel=[],
            target_z_acc=[], actual_z_acc=[], z_error=[],
        )
        for k in REWARD_PART_KEYS:
            d[k] = []
        return d

    def _shade_releases(ax, release_spans):
        """Add orange axvspan for every release window on an axes."""
        for idx, (s0, s1) in enumerate(release_spans):
            ax.axvspan(s0, s1, color="orange", alpha=0.25,
                       label="release window" if idx == 0 else "")

    def _plot_episode(bufs, release_spans, ep_idx, save_dir):
        """Plot 1 – overview: cuboid Z, EE Z, fingertip dist, forces, reward."""
        steps      = np.asarray(bufs["steps"])
        ee_z       = np.asarray(bufs["ee_z"])
        cub_rel_z  = np.asarray(bufs["cuboid_rel_z"])
        des_rel_z  = np.asarray(bufs["desired_rel_z"])
        ft_dist    = np.asarray(bufs["ft_dist"])
        lf_mag     = np.asarray(bufs["lf_mag"])
        rf_mag     = np.asarray(bufs["rf_mag"])
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
            (axes[3], [(lf_mag, "|left_force|", "C4"), (rf_mag, "|right_force|", "C5")],
             "Force [N]", "Finger contact forces"),
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

    def _save_all_plots(bufs, release_spans, ep_idx, save_dir):
        _plot_episode(bufs, release_spans, ep_idx, save_dir)
        _plot_motion(bufs, release_spans, ep_idx, save_dir)
        _plot_reward_parts(bufs, release_spans, ep_idx, save_dir)

    def _append_rollout_sample(bufs, obs, env, step_idx, reward_value, reward_terms, target_z, actual_z_acc):
        """
        Append one sample from the current reset-to-reset segment.

        The env resets internally before returning observations on a terminal step, so callers pass the
        pre-step observation here and attach the reward produced by that step.
        """
        lf_mag  = obs[0, FrankaEnvParallel.OBS_LEFT_FORCE_MAG].item()
        rf_mag  = obs[0, FrankaEnvParallel.OBS_RIGHT_FORCE_MAG].item()

        actual_z_vel = obs[0, FrankaEnvParallel.OBS_EE_VEL_Z].item()
        cub_rel_z    = obs[0, FrankaEnvParallel.OBS_CUBOID_REL_Z].item()
        des_rel_z    = obs[0, FrankaEnvParallel.OBS_DESIRED_REL_Z].item()

        # Fingertip distance is not in the obs anymore; compute directly from env
        ft_dist = float(env.get_fingertip_distance())

        bufs["steps"].append(step_idx)
        bufs["ee_z"].append(obs[0, FrankaEnvParallel.OBS_EE_POS_Z].item())
        bufs["cuboid_rel_z"].append(cub_rel_z)
        bufs["desired_rel_z"].append(des_rel_z)
        bufs["ft_dist"].append(ft_dist)
        bufs["lf_mag"].append(lf_mag)
        bufs["rf_mag"].append(rf_mag)
        bufs["reward"].append(reward_value)
        # motion detail
        bufs["target_z"].append(target_z)
        bufs["actual_z_vel"].append(actual_z_vel)
        bufs["target_z_vel"].append(obs[0, FrankaEnvParallel.OBS_TARGET_Z_VEL].item())
        bufs["target_z_acc"].append(obs[0, FrankaEnvParallel.OBS_TARGET_Z_ACC].item())
        bufs["actual_z_acc"].append(actual_z_acc)
        bufs["z_error"].append(abs(cub_rel_z - des_rel_z))
        # reward parts
        for rk in REWARD_PART_KEYS:
            v = reward_terms.get(rk)
            bufs[rk].append(float(v[0]) if v is not None else 0.0)

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

    print(
        f"target_dt={env.target_period:.3f}s  sim_dt={env.dt:.3f}s  "
        f"steps_per_target={env.target_update_every}"
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
                    obs_scale = torch.tensor(FrankaEnvParallel.OBS_SCALE, device=gs.device)
                    unnorm_obs = obs * obs_scale
                else:
                    unnorm_obs = obs
                    
                target_z = env.target_z[0].item()
                actual_z_vel_cur = unnorm_obs[0, FrankaEnvParallel.OBS_EE_VEL_Z].item()
                actual_z_acc_cur = (
                    (actual_z_vel_cur - prev_actual_z_vel) / env.target_period
                    if prev_actual_z_vel is not None else 0.0
                )
                prev_actual_z_vel = actual_z_vel_cur

                actions = policy(obs_td)
                obs_td, rew_buf, reset_buf, _ = env.step(actions)

                reward_cur = rew_buf[0].item()
                parts_str = "  ".join(
                    f"{k}={float(v):.3f}" for k, v in env.last_reward_terms.items()
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
                    f"left_force_mag={_o[4]:+.3f}  "
                    f"right_force_mag={_o[5]:+.3f}  "
                    f"cuboid_rel_z={_o[6]:+.4f}  "
                    f"cuboid_rel_x={_o[7]:+.4f}  "
                    f"cuboid_rel_y={_o[8]:+.4f}  "
                    f"desired_rel_z={_o[9]:+.4f}"
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

                    success = ep_reward > 0
                    ep_count += 1
                    print(
                        f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  "
                        f"regrasps={len(release_spans)}  "
                        f"{'SUCCESS' if success else 'fail'}"
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

                elif i % 100 == 0:
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
                        f"|lf|={sample['lf_mag']:.3f}  |rf|={sample['rf_mag']:.3f}  "
                        f"cuboid_rel_z={cuboid_rel_z:+.4f}  desired={desired_rel_z:+.4f}  "
                        f"err={abs(cuboid_rel_z - desired_rel_z)*1000:.2f}mm  "
                        f"ep_rew={ep_reward:.1f}  z_vel={actual_z_vel_cur:+.3f}"
                    )

                # Real-time pacing when viewer is open
                if args.vis:
                    t_sim = env.sim_step * env.dt
                    t_wall = time.perf_counter() - t_real_start
                    if t_wall < t_sim:
                        time.sleep(t_sim - t_wall)

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

"""
SAC training for FrankaMuJoCoEnv with stable-baselines3.

Usage:
    python examples/rigid/train_franka_sac_mujoco.py
    python examples/rigid/train_franka_sac_mujoco.py -e franka-mujoco-sac-v1 --total_timesteps 1000000
    python examples/rigid/train_franka_sac_mujoco.py -B 4 --vec-env subproc --mix --normalization --randomize
    python examples/rigid/train_franka_sac_mujoco.py --resume logs/franka-mujoco-sac/model_final.zip
"""

from __future__ import annotations

import argparse
import os
import pickle
import random
import re
import shutil
from importlib import metadata

import numpy as np
import torch

try:
    from stable_baselines3 import SAC
    from stable_baselines3.common.callbacks import CheckpointCallback
    from stable_baselines3.common.monitor import Monitor
    from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecMonitor
except (ImportError, metadata.PackageNotFoundError) as e:
    raise ImportError("Please install stable-baselines3>=2.0 and gymnasium to train SAC.") from e

try:
    from .env_franka_mujoco_sac import FrankaMuJoCoSACEnv
except ImportError:
    from env_franka_mujoco_sac import FrankaMuJoCoSACEnv


def get_train_cfg(args: argparse.Namespace) -> dict:
    return {
        "algorithm": "SAC",
        "policy": "MlpPolicy",
        "total_timesteps": args.total_timesteps,
        "num_envs": args.num_envs,
        "vec_env": args.vec_env,
        "learning_rate": args.learning_rate,
        "buffer_size": args.buffer_size,
        "learning_starts": args.learning_starts,
        "batch_size": args.batch_size,
        "tau": args.tau,
        "gamma": args.gamma,
        "train_freq": args.train_freq,
        "gradient_steps": args.gradient_steps,
        "ent_coef": args.ent_coef,
        "net_arch": [256, 256],
        "dt": args.dt,
        "target_dt": args.target_dt,
        "limit_regrasp": args.limit_regrasp,
        "negative": args.negative,
        "mix": args.mix,
        "randomize": args.randomize,
        "normalization": args.normalization,
        "seed": args.seed,
    }


def make_env(args: argparse.Namespace, rank: int):
    def _init():
        seed = args.seed + rank
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        env = FrankaMuJoCoSACEnv(
            dt=args.dt,
            target_dt=args.target_dt,
            limit_regrasp=args.limit_regrasp,
            solid_up=args.negative,
            mix=args.mix,
            randomize=args.randomize,
            normalize=args.normalization,
            seed=seed,
        )
        return Monitor(env)

    return _init


def build_vec_env(args: argparse.Namespace):
    env_fns = [make_env(args, rank) for rank in range(args.num_envs)]
    if args.vec_env == "subproc" and args.num_envs > 1:
        env = SubprocVecEnv(env_fns, start_method="spawn")
    else:
        env = DummyVecEnv(env_fns)
    return VecMonitor(env)


def resolve_replay_buffer_path(args: argparse.Namespace, log_dir: str) -> str | None:
    if args.resume_replay_buffer:
        return args.resume_replay_buffer
    if args.resume is None:
        return None

    resume_dir = os.path.dirname(args.resume) or "."
    resume_name = os.path.basename(args.resume)
    candidates = []
    match = re.fullmatch(r"(.*)_(\d+)_steps\.zip", resume_name)
    if match:
        prefix, step = match.groups()
        candidates.append(os.path.join(resume_dir, f"{prefix}_replay_buffer_{step}_steps.pkl"))
    if resume_name == "model_final.zip":
        candidates.append(os.path.join(resume_dir, "replay_buffer_final.pkl"))
    candidates.append(os.path.join(log_dir, "replay_buffer_final.pkl"))

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def main():
    parser = argparse.ArgumentParser(description="SAC training for MuJoCo Franka")
    parser.add_argument("-e", "--exp_name", type=str, default="franka-mujoco-sac",
                        help="Experiment name; also used as log subdirectory")
    parser.add_argument("-B", "--num_envs", type=int, default=1,
                        help="Number of MuJoCo environments. Use --vec-env subproc for true parallelism.")
    parser.add_argument("--vec-env", choices=("dummy", "subproc"), default="dummy",
                        help="Vector env backend. dummy is simpler; subproc can help with multiple CPU envs.")
    parser.add_argument("--total_timesteps", type=int, default=1_000_000,
                        help="Total high-level SAC environment steps")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--resume", type=str, default=None,
                        help="Path to a stable-baselines3 SAC checkpoint .zip")
    parser.add_argument("--resume-replay-buffer", type=str, default=None,
                        help="Optional path to a SAC replay buffer .pkl. If omitted, common paths are auto-detected.")
    parser.add_argument("--dt", type=float, default=0.001,
                        help="Physics sim timestep (seconds)")
    parser.add_argument("--target_dt", type=float, default=0.02,
                        help="High-level action period (seconds); should be a multiple of dt")
    parser.add_argument("--limit-regrasp", action="store_true",
                        help="Terminate an episode as failure on the 4th regrasp")
    parser.add_argument("--negative", action="store_true",
                        help="Use negative desired_rel_z (solid-up training mode)")
    parser.add_argument("--mix", action="store_true",
                        help="Mix normal and solid-up modes per episode")
    parser.add_argument("--randomize", action="store_true",
                        help="Enable MuJoCo domain randomization and observation noise")
    parser.add_argument("--normalization", action="store_true",
                        help="Enable fixed observation normalization")
    parser.add_argument("--device", type=str, default="auto")
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--buffer-size", type=int, default=1_000_000)
    parser.add_argument("--learning-starts", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--tau", type=float, default=0.005)
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--train-freq", type=int, default=1)
    parser.add_argument("--gradient-steps", type=int, default=1)
    parser.add_argument("--ent-coef", type=str, default="auto")
    parser.add_argument("--save-interval", type=int, default=50_000,
                        help="Checkpoint interval in high-level env steps")
    parser.add_argument("--log-interval", type=int, default=10)
    parser.add_argument("--progress-bar", action="store_true",
                        help="Show stable-baselines3 progress bar if tqdm/rich are installed")
    args = parser.parse_args()

    if args.num_envs <= 0:
        raise ValueError("--num_envs must be positive")

    log_dir = os.path.join("logs", args.exp_name)
    if args.resume is None:
        if os.path.exists(log_dir):
            shutil.rmtree(log_dir)
    os.makedirs(log_dir, exist_ok=True)

    cfg = get_train_cfg(args)
    with open(os.path.join(log_dir, "sac_train_cfg.pkl"), "wb") as f:
        pickle.dump(cfg, f)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    env = build_vec_env(args)
    policy_kwargs = dict(net_arch=[256, 256], activation_fn=torch.nn.ReLU)

    if args.resume is not None:
        model = SAC.load(args.resume, env=env, device=args.device)
        replay_buffer_path = resolve_replay_buffer_path(args, log_dir)
        if replay_buffer_path is not None:
            model.load_replay_buffer(replay_buffer_path)
            print(f"Loaded SAC replay buffer: {replay_buffer_path}")
        reset_num_timesteps = False
        print(f"Resumed SAC checkpoint: {args.resume}")
    else:
        model = SAC(
            "MlpPolicy",
            env,
            learning_rate=args.learning_rate,
            buffer_size=args.buffer_size,
            learning_starts=args.learning_starts,
            batch_size=args.batch_size,
            tau=args.tau,
            gamma=args.gamma,
            train_freq=args.train_freq,
            gradient_steps=args.gradient_steps,
            ent_coef=args.ent_coef,
            policy_kwargs=policy_kwargs,
            tensorboard_log=log_dir,
            seed=args.seed,
            device=args.device,
            verbose=1,
        )
        reset_num_timesteps = True

    save_freq = max(1, args.save_interval // args.num_envs)
    checkpoint_cb = CheckpointCallback(
        save_freq=save_freq,
        save_path=log_dir,
        name_prefix="model",
        save_replay_buffer=True,
        save_vecnormalize=False,
    )

    try:
        model.learn(
            total_timesteps=args.total_timesteps,
            callback=checkpoint_cb,
            log_interval=args.log_interval,
            tb_log_name=args.exp_name,
            reset_num_timesteps=reset_num_timesteps,
            progress_bar=args.progress_bar,
        )
        final_path = os.path.join(log_dir, "model_final")
        model.save(final_path)
        model.save_replay_buffer(os.path.join(log_dir, "replay_buffer_final"))
        print(f"Saved final SAC model to {final_path}.zip")
    finally:
        env.close()


if __name__ == "__main__":
    main()

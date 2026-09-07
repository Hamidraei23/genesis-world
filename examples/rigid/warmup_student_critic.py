"""
Critic warm-up for the distilled Franka student before PPO fine-tuning.

Problem: the distilled critic in model_0.pt predicts values on the TEACHER's
return scale, but the stochastic student earns different returns. Advantages
computed from that critic are systematically wrong, and the very first PPO
update can destroy the distilled actor.

This script freezes every actor parameter (mean MLP + exploration std), runs
N ordinary PPO iterations so the critic regresses onto the frozen student's
real returns, then exports a checkpoint with the untouched actor, the warmed
critic, and a fresh optimizer.

Usage (inside the genesis docker container, from /workspace):

    # 1) Warm the critic (actor frozen the whole time):
    python3 examples/rigid/warmup_student_critic.py -e franka-lift-v1 --iters 50 \
        --mix --zero --randomize --normalization --control-error

    # 2) Fine-tune from the warmed checkpoint with the unmodified trainer:
    python3 examples/rigid/train_franka_ppo.py -e franka-lift-v1-student-ft3 \
        --resume logs/franka-lift-v1-2H-control/student/model_warmed.pt \
        --mix --zero --randomize --normalization --control-error -B 1024

Watch "Mean value loss" during warm-up: resume fine-tuning once it has dropped
and plateaued (typically well under 100k). "Mean reward" should stay flat at
the frozen student's level the entire time — if it moves, something is wrong.
"""

import argparse
import os
from itertools import chain
from importlib import metadata

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
    from .train_franka_ppo import get_train_cfg
except ImportError:
    from train_franka_ppo import get_train_cfg


def main():
    parser = argparse.ArgumentParser(description="Critic-only PPO warm-up for the distilled student")
    parser.add_argument("-e", "--exp_name", type=str, default="franka-lift-v1",
                        help="Experiment name; student folder at logs/<exp_name>-2H-control/student")
    parser.add_argument("--resume", type=str, default=None,
                        help="Checkpoint to warm up (default: <student_dir>/model_0.pt)")
    parser.add_argument("--out", type=str, default=None,
                        help="Output checkpoint path (default: <student_dir>/model_warmed.pt)")
    parser.add_argument("--iters", type=int, default=50,
                        help="PPO iterations with the actor frozen")
    parser.add_argument("-B", "--num_envs", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--dt", type=float, default=0.001)
    parser.add_argument("--target_dt", type=float, default=0.02)
    parser.add_argument("--mix", action="store_true", default=False)
    parser.add_argument("--zero", action="store_true", default=False)
    parser.add_argument("--randomize", action="store_true", default=False)
    parser.add_argument("--normalization", action="store_true", default=False)
    parser.add_argument("--control-error", action="store_true", default=False)
    parser.add_argument("--limit-regrasp", action="store_true", default=False)
    parser.add_argument("--negative", action="store_true", default=False)
    args = parser.parse_args()

    student_dir = f"logs/{args.exp_name}-2H-control/student"
    resume_path = args.resume or os.path.join(student_dir, "model_0.pt")
    out_path = args.out or os.path.join(student_dir, "model_warmed.pt")
    if not os.path.exists(resume_path):
        raise FileNotFoundError(f"{resume_path} not found")
    log_dir = os.path.join(student_dir, "warmup_logs")
    os.makedirs(log_dir, exist_ok=True)

    train_cfg = get_train_cfg(f"{args.exp_name}-critic-warmup")

    gs.init(
        backend=gs.gpu,
        precision="32",
        logging_level="warning",
        seed=args.seed,
        performance_mode=True,
    )

    env = FrankaEnvParallel(
        num_envs=args.num_envs,
        vis=False,
        dt=args.dt,
        target_dt=args.target_dt,
        limit_regrasp=args.limit_regrasp,
        solid_up=args.negative,
        mix=args.mix,
        randomize=args.randomize,
        normalize=args.normalization,
        zero=args.zero,
        control_error=args.control_error,
    )

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(resume_path)
    print(f"Loaded checkpoint: {resume_path}")
    print(f"Actor std: {runner.alg.actor.distribution.std_param.detach().cpu().tolist()}")

    # Freeze the entire actor (mean MLP + std). Rollouts still use it for
    # sampling; only the critic receives gradient updates.
    n_frozen = 0
    for p in runner.alg.actor.parameters():
        p.requires_grad_(False)
        n_frozen += p.numel()
    print(f"Froze {n_frozen} actor parameters; training critic only for {args.iters} iterations")

    # With a frozen actor the measured KL is ~0, and the adaptive schedule
    # would ratchet the learning rate up every update. Pin it for the warm-up.
    runner.alg.schedule = "fixed"

    runner.learn(num_learning_iterations=args.iters, init_at_random_ep_len=True)

    # ---- export: original actor, warmed critic, fresh optimizer ----
    for p in runner.alg.actor.parameters():
        p.requires_grad_(True)
    saved = runner.alg.save()
    fresh_optimizer = torch.optim.Adam(
        chain(runner.alg.actor.parameters(), runner.alg.critic.parameters()),
        lr=train_cfg["algorithm"]["learning_rate"],
    )
    ckpt = {
        "actor_state_dict": saved["actor_state_dict"],
        "critic_state_dict": saved["critic_state_dict"],
        "optimizer_state_dict": fresh_optimizer.state_dict(),
        "iter": 0,
        "infos": None,
    }
    torch.save(ckpt, out_path)
    print(f"\nSaved warmed checkpoint -> {out_path}")
    print(
        "Fine-tune with:\n"
        f"  python3 examples/rigid/train_franka_ppo.py -e {args.exp_name}-student-ft3 "
        f"--resume {out_path} --mix --zero --randomize --normalization --control-error -B 1024"
    )


if __name__ == "__main__":
    main()

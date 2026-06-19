import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import torch

import genesis as gs

try:
    from .env_franka_parallel import FrankaEnvParallel
except ImportError:
    from env_franka_parallel import FrankaEnvParallel


def query_gpu_memory():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    first_gpu = result.stdout.strip().splitlines()[0]
    used_mb, total_mb = [int(part.strip()) for part in first_gpu.split(",")]
    return {"used_mb": used_mb, "total_mb": total_mb}


def run_trial(args):
    gs.init(backend=gs.gpu, precision="32", logging_level="warning")

    env = FrankaEnvParallel(
        num_envs=args.num_envs,
        vis=False,
        dt=args.dt,
        target_dt=args.target_dt,
    )

    actions = torch.zeros((args.num_envs, env.action_dim), device=gs.device)
    actions[:, 1:] = 0.000251

    for i in range(args.warmup):
        actions[:, 0] = 0.2 * torch.sin(torch.tensor(i * 0.1, device=gs.device))
        env.step(actions)

    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()

    mem_before = query_gpu_memory()
    t0 = time.perf_counter()

    for i in range(args.steps):
        actions[:, 0] = 0.2 * torch.sin(torch.tensor(i * 0.1, device=gs.device))
        env.step(actions)

    if torch.cuda.is_available():
        torch.cuda.synchronize()

    elapsed_s = time.perf_counter() - t0
    mem_after = query_gpu_memory()
    sim_steps = args.steps * env.target_update_every
    env_sim_steps = args.num_envs * sim_steps

    result = {
        "num_envs": args.num_envs,
        "elapsed_s": elapsed_s,
        "control_steps_per_s": args.num_envs * args.steps / elapsed_s,
        "sim_steps_per_s": sim_steps / elapsed_s,
        "env_sim_steps_per_s": env_sim_steps / elapsed_s,
        "target_update_every": env.target_update_every,
        "gpu_mem_before": mem_before,
        "gpu_mem_after": mem_after,
    }
    if torch.cuda.is_available():
        result["torch_peak_reserved_mb"] = torch.cuda.max_memory_reserved() / 1024**2
        result["torch_peak_allocated_mb"] = torch.cuda.max_memory_allocated() / 1024**2

    print(json.dumps(result, sort_keys=True))


def run_sweep(args):
    script = Path(__file__).resolve()
    for num_envs in args.sweep:
        cmd = [
            sys.executable,
            str(script),
            "--num-envs",
            str(num_envs),
            "--steps",
            str(args.steps),
            "--warmup",
            str(args.warmup),
            "--dt",
            str(args.dt),
            "--target-dt",
            str(args.target_dt),
        ]
        print(f"\n=== num_envs={num_envs} ===", flush=True)
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout, end="")
        if result.returncode != 0:
            print(result.stderr, end="", file=sys.stderr)
            print(f"FAILED at num_envs={num_envs}", file=sys.stderr)
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--num-envs", type=int, default=1)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--dt", type=float, default=0.001)
    parser.add_argument("--target-dt", type=float, default=0.02)
    parser.add_argument("--sweep", type=int, nargs="*")
    args = parser.parse_args()

    if args.sweep:
        run_sweep(args)
    else:
        run_trial(args)


if __name__ == "__main__":
    main()

"""
Per-(pulse_delay, pulse_length) target-trajectory capture at a fixed lift height.

For every combination in the grid --delays x --lengths this script pins the
gripper-pulse timing (exactly the way eval_test_franka_delays.py pins it:
PULSE_*_RANDOM_MIN == PULSE_*_RANDOM_MAX, re-read on every env reset), pins
desired_rel_z to a single value (default +0.02 m -- 2 cm, normal/up mode, NOT
solid-up), rolls the policy out until an episode terminates in SUCCESS, and
writes that episode's commanded trajectory to a CSV.

Success in this env already implies the 2 cm was actually achieved:
    |cuboid_rel_z - desired_rel_z| <= 0.005  AND  |ee_vel_z| < 0.02
    AND firm grasp for >= 3 steps  AND  0.7 <= ee_z <= 0.86
so a written CSV is a trajectory that reached 2 cm and held it.

One row per high-level step (target_dt, 20 ms by default). Between two
consecutive rows the low-level controller interpolates target_z with a cubic
Hermite segment through (z, z_vel) at both ends -- see
FrankaEnvParallel._sample_target_z -- so the 1 kHz reference is reconstructible
offline from these rows alone.

Columns:
    target_z/_vel/_acc   the commanded z reference for that step (the trajectory
                         to replay; target_z_vel already includes the
                         control-error bias and the acceleration clamp)
    ee_z, ee_vel_z       measured end-effector z and z velocity
    cuboid_z             measured cube z; cuboid_rel_z = cube z - fingertip mid z
    grip_cmd_l/r         finger position actually commanded (metres), i.e. after
                         the pulse override and the zero-hold -- authoritative
                         for which phase the row is in
    finger_l/r           measured finger joint positions (pinned by the cube
                         while it is grasped, so nearly constant)
    pulse_steps          pulse countdown *after* that step's decrement, so it
                         reads one lower than the value that chose the phase;
                         use grip_cmd_l/r to read the phase off directly
    zero_hold            zero-hold countdown remaining
    act_z_vel            policy z-velocity command, clamped, in m/s, BEFORE the
                         control-error bias
    act_grip_l/r         policy gripper action, clamped to [-1, 1]

Everything runs in one process (Genesis allows a single gs.init per process):
the scene is built once and the pulse pin is swapped between grid points,
because the samplers re-read PULSE_* on every reset.

Usage (inside the genesis container, from /workspace):
    python3 examples/rigid/eval_pulse_grid_traj_franka.py \
        -e franka-lift-v1-student-ft3 --ckpt 1059

    # exactly the 3x3 grid, 2 successful episodes per combination
    python3 examples/rigid/eval_pulse_grid_traj_franka.py \
        -e franka-lift-v1-student-ft3 --ckpt 1059 \
        --delays 1 2 3 --lengths 3 4 5 --per-combo 2

    # watch one env in the viewer (and/or write an MP4 per combination)
    python3 examples/rigid/eval_pulse_grid_traj_franka.py \
        -e franka-lift-v1-student-ft3 --ckpt 1059 \
        -B 1 --vis --record --max-steps 10000

Outputs (default --out-dir logs/<exp_name>/traj):
    traj_d<delay>_l<length>[_<k>].csv   one successful episode each
    summary.json                        what was found / missed per combination
"""

import argparse
import json
import os
import pickle
import sys
from pathlib import Path

import numpy as np
import torch

from rsl_rl.runners import OnPolicyRunner

import genesis as gs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_tests_franka import (  # noqa: E402  (path must be set first)
    load_env_class,
    resolve_env_spec,
    select_checkpoint,
)
from eval_test_franka_delays import (  # noqa: E402
    capture_ranges,
    observed_pulse,
    reseed,
    set_pulse,
)


# Columns read off the env state each step (order matters: written as-is)
STATE_COLS = (
    "target_z", "target_z_vel", "target_z_acc",
    "ee_z", "ee_vel_z",
    "cuboid_z", "cuboid_rel_z", "desired_rel_z",
    "grip_cmd_l", "grip_cmd_r",
    "finger_l", "finger_r", "fingertip_dist",
    "pulse_steps", "zero_hold",
)
# Columns derived from the action the policy just issued
ACTION_COLS = ("act_z_vel", "act_grip_l", "act_grip_r", "reward")

CSV_COLS = ("step", "t_s") + STATE_COLS + ACTION_COLS + ("pulse_delay", "pulse_length")


class GripperCommandCapture:
    """Record the finger positions the env actually commands each step.

    The env applies the pulse (forced open / forced close) and the zero-hold to
    the gripper *after* the policy action, and never stores the result. The arm
    is driven with control_dofs_velocity, so the only (num_envs, 2) position
    command is the gripper one -- everything else is filtered out here.
    """

    def __init__(self, env):
        self.env = env
        self._orig = env.franka.control_dofs_position
        self.last = torch.full((env.num_envs, 2), float("nan"), device=env.device)
        env.franka.control_dofs_position = self.__call__
        env._grip_cmd_capture = self

    def __call__(self, position, *args, **kwargs):
        if (torch.is_tensor(position) and position.ndim == 2
                and position.shape == (self.env.num_envs, 2)):
            self.last = position.detach().clone()
        return self._orig(position, *args, **kwargs)


def state_row(env) -> np.ndarray:
    """(num_envs, len(STATE_COLS)) snapshot of the env's commanded/measured state."""
    ee_pos = env.ee_link.get_pos()
    ee_vel = env.ee_link.get_vel()
    cuboid = env.cuboid.get_pos()
    left_ft = env._fingertip_pos(env.left_finger)
    right_ft = env._fingertip_pos(env.right_finger)
    finger_mid_z = (left_ft[:, 2] + right_ft[:, 2]) / 2.0
    fingers = env.franka.get_dofs_position(dofs_idx_local=env.fingers_dof)
    grip_cmd = env._grip_cmd_capture.last

    cols = (
        env.target_z, env.target_z_vel, env.target_z_acc,
        ee_pos[:, 2], ee_vel[:, 2],
        cuboid[:, 2], cuboid[:, 2] - finger_mid_z, env.desired_rel_z,
        grip_cmd[:, 0], grip_cmd[:, 1],
        fingers[:, 0], fingers[:, 1], (left_ft - right_ft).norm(dim=-1),
        env._gripper_pulse_steps, env._zero_hold_countdown,
    )
    return torch.stack([c.detach().float() for c in cols], dim=-1).cpu().numpy()


class EnvPins:
    """Pins desired_rel_z on every reset and snapshots terminal state pre-reset.

    The env resets a done env *inside* step(), so anything read after step()
    returns already reflects the home pose. Wrapping _reset_idx is the only
    place where the true terminal state is still visible.
    """

    def __init__(self, env, desired_rel_z: float):
        self.env = env
        self.desired = float(desired_rel_z)
        self.terminal: dict[int, np.ndarray] = {}
        self._orig_reset_idx = env._reset_idx
        self._orig_reset = env.reset
        env._reset_idx = self._reset_idx
        env.reset = self.reset

    def _reset_idx(self, envs_idx):
        if len(envs_idx) > 0:
            snap = state_row(self.env)
            for i in envs_idx.tolist():
                self.terminal[i] = snap[i]
        self._orig_reset_idx(envs_idx)
        self.env.desired_rel_z[envs_idx] = self.desired

    def reset(self, *args, **kwargs):
        self._orig_reset(*args, **kwargs)
        self.env.desired_rel_z[:] = self.desired
        self.terminal.clear()
        # desired_rel_z is obs channel 7; recompute so step 0 sees the pinned value
        self.env._update_obs_buf()
        return self.env.get_observations()


def write_csv(path: str, traj: np.ndarray, target_period: float,
              delay: int, length: int) -> None:
    """traj: (T, len(STATE_COLS) + len(ACTION_COLS)) for one episode."""
    n = traj.shape[0]
    step = np.arange(n, dtype=float)
    table = np.column_stack([
        step,
        step * target_period,
        traj,
        np.full(n, float(delay)),
        np.full(n, float(length)),
    ])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savetxt(path, table, delimiter=",", header=",".join(CSV_COLS),
               comments="", fmt="%.6f")


def run_combo(env, policy, pins: EnvPins, delay: int, length: int,
              args, ranges: dict, out_dir: str) -> dict:
    """Roll out with the pulse pinned; save the first --per-combo successes."""
    reseed(args.seed)
    set_pulse(env, ranges, delay=delay, length=length)

    if args.record:
        env.cam.start_recording()

    obs = env.reset()
    used = observed_pulse(env)
    if used["delays"] != [delay] or used["lengths"] != [length]:
        raise RuntimeError(
            f"pulse pin failed: asked delay={delay} length={length}, env used {used}"
        )

    n_envs = env.num_envs
    n_state = len(STATE_COLS)
    rows: list[np.ndarray] = []
    ep_start = np.zeros(n_envs, dtype=int)
    saved: list[dict] = []
    outcomes = {"success": 0, "fail": 0, "timeout": 0}

    for _ in range(args.max_steps):
        with torch.no_grad():
            actions = policy(obs)

        act = actions.detach().float()
        obs, rew, done, _extras = env.step(actions)
        if args.record:
            env.cam.render()

        row = np.concatenate([
            state_row(env),
            torch.stack([
                act[:, 0].clamp(-1.0, 1.0) * env.Z_VEL_MAX,
                act[:, 1].clamp(-1.0, 1.0), act[:, 2].clamp(-1.0, 1.0),
                rew.detach().float(),
            ], dim=-1).cpu().numpy(),
        ], axis=1)
        # done envs were already reset inside step(): restore their terminal state
        for i, snap in pins.terminal.items():
            row[i, :n_state] = snap
        pins.terminal.clear()
        rows.append(row)

        done_np = done.detach().cpu().numpy().astype(bool)
        if not done_np.any():
            continue

        terms = env.last_reward_terms
        succ_np = (terms["success"] > 0).detach().cpu().numpy().astype(bool)
        time_np = (terms["timeout"] > 0).detach().cpu().numpy().astype(bool)

        for i in np.nonzero(done_np)[0]:
            if succ_np[i]:
                outcomes["success"] += 1
            elif time_np[i]:
                outcomes["timeout"] += 1
            else:
                outcomes["fail"] += 1

            if succ_np[i] and len(saved) < args.per_combo:
                traj = np.stack([r[i] for r in rows[ep_start[i]:]], axis=0)
                suffix = "" if args.per_combo == 1 else f"_{len(saved)}"
                path = os.path.join(out_dir, f"traj_d{delay}_l{length}{suffix}.csv")
                write_csv(path, traj, env.target_period, delay, length)
                lift = float(traj[-1, STATE_COLS.index("cuboid_rel_z")])
                saved.append({"csv": path, "steps": int(traj.shape[0]),
                              "final_cuboid_rel_z": lift})
                print(f"    saved {path}  ({traj.shape[0]} steps, "
                      f"final cuboid_rel_z={lift:+.4f} m)")
            ep_start[i] = len(rows)

        if len(saved) >= args.per_combo:
            break

    if not saved:
        print(f"    [WARNING] no successful episode for delay={delay} length={length} "
              f"within {args.max_steps} steps ({outcomes})")

    if args.record:
        video = os.path.join(out_dir, f"traj_d{delay}_l{length}.mp4")
        os.makedirs(out_dir, exist_ok=True)
        env.cam.stop_recording(save_to_filename=video, fps=args.fps)
        print(f"    saved {video}")

    return {"pulse_delay": delay, "pulse_length": length,
            "saved": saved, "outcomes": outcomes,
            "policy_steps": len(rows)}


def main():
    p = argparse.ArgumentParser(
        description="Save one successful-episode target trajectory per pulse "
                    "(delay, length) combination at a fixed lift target")
    p.add_argument("-e", "--exp_name", type=str, required=True,
                   help="Run directory under logs/")
    p.add_argument("--env", type=str, default=None,
                   help="Override the env module (default: read logs/<run>/env_cfg.pkl)")
    p.add_argument("--ckpt", type=int, default=None,
                   help="Checkpoint iteration; default picks the best by training reward")
    p.add_argument("--after", type=int, default=450,
                   help="Auto-selection: only consider checkpoints at iter >= this")
    p.add_argument("--window", type=int, default=25,
                   help="Auto-selection: trailing-mean window in iterations")
    p.add_argument("--tag", type=str, default="Train/mean_reward",
                   help="Auto-selection: TensorBoard scalar to score by")
    p.add_argument("--delays", nargs="+", type=int, default=[1, 2, 3],
                   help="PULSE_DELAY_STEPS values to pin (steps of target_dt)")
    p.add_argument("--lengths", nargs="+", type=int, default=[3, 4, 5, 6],
                   help="PULSE_LENGTH values to pin (steps of target_dt)")
    p.add_argument("--desired-rel-z", type=float, default=0.02,
                   help="Lift target in metres, pinned for every episode. "
                        "Positive = normal/up mode (not solid-up)")
    p.add_argument("--per-combo", type=int, default=1,
                   help="Successful episodes to save per combination")
    p.add_argument("-B", "--num-envs", type=int, default=64)
    p.add_argument("--max-steps", type=int, default=3000,
                   help="Policy-step budget per combination")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--dt", type=float, default=0.001)
    p.add_argument("--target_dt", type=float, default=0.02)
    p.add_argument("--out-dir", type=str, default=None,
                   help="Default: logs/<exp_name>/traj")
    p.add_argument("--vis", action="store_true", default=False,
                   help="Open the interactive viewer (needs DISPLAY; the genesis "
                        "container already has X11 forwarded). Use with -B 1.")
    p.add_argument("--record", action="store_true", default=False,
                   help="Also write an offscreen MP4 per combination next to the CSVs")
    p.add_argument("--fps", type=int, default=50,
                   help="MP4 frame rate; 50 = real time at target_dt=0.02")
    p.add_argument("--no-normalization", dest="normalization", action="store_false",
                   default=True, help="Disable obs normalization (training used it)")
    p.add_argument("--no-randomize", dest="randomize", action="store_false",
                   default=True, help="Disable domain randomization (training used it)")
    p.add_argument("--no-zero", dest="zero", action="store_false", default=True,
                   help="Disable the post-pulse zero-hold (training used it)")
    p.add_argument("--no-control-error", dest="control_error", action="store_false",
                   default=True, help="Disable the command bias (training used it)")
    args = p.parse_args()

    if args.desired_rel_z <= 0:
        p.error("--desired-rel-z must be positive; negative is solid-up mode")
    if args.num_envs <= 4 and args.max_steps < 6000:
        print(f"[note] -B {args.num_envs}: only {args.num_envs} episode(s) run at a time, "
              f"so a success can take many episodes; consider --max-steps 10000")

    log_dir = f"logs/{args.exp_name}"
    if not os.path.isdir(log_dir):
        raise FileNotFoundError(f"No such run directory: {log_dir}")
    out_dir = args.out_dir or os.path.join(log_dir, "traj")

    env_spec = args.env or resolve_env_spec(log_dir, args.exp_name)
    env_cls = load_env_class(env_spec)
    print(f"### {args.exp_name}: env={env_spec} -> {env_cls.__name__}")

    if args.ckpt is not None:
        ckpt_iter = args.ckpt
        ckpt_path = os.path.join(log_dir, f"model_{args.ckpt}.pt")
        if not os.path.exists(ckpt_path):
            raise FileNotFoundError(ckpt_path)
    else:
        ckpt_iter, ckpt_path, _score, _ = select_checkpoint(
            log_dir, args.after, args.window, args.tag)
    print(f"Checkpoint: {ckpt_path}")

    with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
        train_cfg = pickle.load(f)

    gs.init(backend=gs.gpu, precision="32", logging_level="warning",
            seed=args.seed, performance_mode=True)

    ranges = capture_ranges(env_cls)
    print(f"Native pulse ranges: delay {ranges['delay']}  length {ranges['length']}")

    env = env_cls(
        num_envs=args.num_envs,
        vis=args.vis,
        record=args.record,   # adds the camera before scene.build()
        dt=args.dt,
        target_dt=args.target_dt,
        mix=False,           # fixed sign; the pin below fixes the magnitude
        solid_up=False,      # normal/up mode
        normalize=args.normalization,
        randomize=args.randomize,
        zero=args.zero,
        control_error=args.control_error,
    )
    pins = EnvPins(env, args.desired_rel_z)
    GripperCommandCapture(env)

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=gs.device)

    grid = [(d, l) for d in args.delays for l in args.lengths]
    print(f"Grid: {len(grid)} combinations, desired_rel_z pinned to "
          f"{args.desired_rel_z:+.3f} m, {args.per_combo} trajectory/ies each")

    results = []
    for delay, length in grid:
        print(f"\n--- pulse delay={delay} ({delay * env.target_period * 1e3:.0f} ms)  "
              f"length={length} ({length * env.target_period * 1e3:.0f} ms) ---")
        results.append(run_combo(env, policy, pins, delay, length,
                                 args, ranges, out_dir))

    os.makedirs(out_dir, exist_ok=True)
    summary = {
        "exp_name": args.exp_name,
        "env": env_spec,
        "ckpt_iter": ckpt_iter,
        "ckpt_path": ckpt_path,
        "desired_rel_z": args.desired_rel_z,
        "solid_up": False,
        "num_envs": args.num_envs,
        "seed": args.seed,
        "target_dt": args.target_dt,
        "dt": args.dt,
        "flags": {"normalization": args.normalization, "randomize": args.randomize,
                  "zero": args.zero, "control_error": args.control_error, "mix": False},
        "columns": list(CSV_COLS),
        "combinations": results,
    }
    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    n_saved = sum(len(r["saved"]) for r in results)
    missing = [(r["pulse_delay"], r["pulse_length"]) for r in results if not r["saved"]]
    print(f"\nWrote {n_saved} CSV(s) to {out_dir}")
    print(f"Wrote {summary_path}")
    if missing:
        print(f"No success for: {missing}")


if __name__ == "__main__":
    main()

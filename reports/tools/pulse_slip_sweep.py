"""Scripted pulse experiments in the tilted env: measure in-hand slip per pulse.

Runs the same scalar profile as franka_controllers/scripts/linear_vel_test.py
(ramp up at 4 m/s^2 to v_peak, reverse at 15 m/s^2 to -v_peak, cruise, stop)
with the gripper pulse fired at the start of the reversal, and records the
cuboid's grasp-axis position relative to the fingertips.

Each environment gets its own (v_peak, pulse_length, trigger offset) so a whole
sweep runs in one batched scene per tilt angle.

    python3 reports/tools/pulse_slip_sweep.py --tilts 0 15 30 45 --out reports/data/pulse_slip.npz
"""

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "rigid"))

import genesis as gs
from env_franka_parallel_tilted import FrankaEnvParallelTilted as Env


def build_env(num_envs, tilt, finger_kp, friction, zvel_max=None):
    if zvel_max is not None:
        Env.Z_VEL_MAX = zvel_max  # lift the policy's cap so faster profiles can be tested
    # Pin everything the env samples per episode so each cell is one clean measurement.
    Env.FINGER_GAIN_RANDOM_MIN = Env.FINGER_GAIN_RANDOM_MAX = finger_kp
    Env.FRICTION_MIN = Env.FRICTION_MAX = friction
    Env.PULSE_DELAY_STEPS = 0
    Env.PULSE_START_MIN_STEPS = 0
    return Env(num_envs=num_envs, tilt_deg=tilt, randomize=False, normalize=False)


def run(env, v_peak, lengths, offsets, pre_steps, accel_up=4.0, accel_down=15.0):
    """Drive every env through its own profile; return per-step logs."""
    N = env.num_envs
    dev = env.device
    T = env.target_period
    env.reset()
    env._gripper_pulse_delays[:] = 0
    env._gripper_pulse_lengths[:] = torch.as_tensor(lengths, device=dev)
    env.desired_rel_z[:] = 0.2  # unreachable: success never ends the episode early

    v_peak = torch.as_tensor(v_peak, device=dev, dtype=torch.float32)
    offsets = torch.as_tensor(offsets, device=dev)
    ramp_steps = torch.ceil(v_peak / (accel_up * T)).long()
    rev_start = pre_steps + ramp_steps  # first step whose command decelerates
    trigger = rev_start + offsets

    logs = {k: [] for k in ("rel_z", "ee_z", "ee_vel", "cmd_v", "force", "grip", "tip_dist")}
    v_cmd = torch.zeros(N, device=dev)
    n_steps = int(pre_steps + ramp_steps.max().item() + 45)
    for k in range(n_steps):
        k_t = torch.full((N,), k, device=dev)
        up = (k_t >= pre_steps) & (k_t < rev_start)
        rev = k_t >= rev_start
        # ramp up, then reverse at accel_down to -v_peak and hold there
        v_up = torch.minimum(v_peak, (k_t - pre_steps + 1).float() * accel_up * T)
        v_rev = torch.maximum(-v_peak, v_peak - (k_t - rev_start + 1).float() * accel_down * T)
        v_cmd = torch.where(up, v_up, torch.where(rev, v_rev, torch.zeros_like(v_cmd)))
        # stop 8 steps into the cruise so the arm stays inside the fail band
        stop = k_t >= rev_start + 3 + 8
        v_cmd = torch.where(stop, torch.clamp(-v_peak + (k_t - rev_start - 10).float() * 8.0 * T, max=0.0), v_cmd)
        grip = torch.where(k_t == trigger, 1.0, -1.0)
        act = torch.stack([v_cmd / env.Z_VEL_MAX, grip, grip], dim=-1)
        env.step(act)
        terms = env.last_reward_terms
        obs = env.obs_buf
        logs["rel_z"].append(obs[:, env.OBS_CUBOID_REL_Z].cpu().numpy())
        logs["ee_z"].append(obs[:, env.OBS_EE_POS_Z].cpu().numpy())
        logs["ee_vel"].append(obs[:, env.OBS_EE_VEL_Z].cpu().numpy())
        logs["cmd_v"].append(env.target_z_vel.cpu().numpy())
        logs["force"].append(terms["avg_force"].cpu().numpy())
        logs["grip"].append((env._gripper_pulse_steps > 0).float().cpu().numpy())
        logs["tip_dist"].append(env.get_fingertip_distance().cpu().numpy())
    out = {k: np.stack(v, axis=1) for k, v in logs.items()}
    out["trigger"] = trigger.cpu().numpy()
    out["rev_start"] = rev_start.cpu().numpy()
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tilts", type=float, nargs="+", default=[0.0, 15.0, 30.0, 45.0])
    p.add_argument("--vpeaks", type=float, nargs="+", default=[0.15, 0.25, 0.35, 0.45])
    p.add_argument("--lengths", type=int, nargs="+", default=[3, 4, 5, 6, 7, 8, 9])
    p.add_argument("--offsets", type=int, nargs="+", default=[-2, -1, 0, 1, 2])
    p.add_argument("--finger-kp", type=float, default=300.0)
    p.add_argument("--friction", type=float, default=0.75)
    p.add_argument("--pre-steps", type=int, default=15)
    p.add_argument("--zvel-max", type=float, default=None, help="override Env.Z_VEL_MAX [m/s]")
    p.add_argument("--out", type=str, default="reports/data/pulse_slip.npz")
    args = p.parse_args()

    grid = list(itertools.product(args.vpeaks, args.lengths, args.offsets))
    v = [g[0] for g in grid]
    L = [g[1] for g in grid]
    o = [g[2] for g in grid]

    gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
    results = {}
    env = None
    for tilt in args.tilts:
        if env is not None:
            gs.destroy()
            gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
        env = build_env(len(grid), tilt, args.finger_kp, args.friction, args.zvel_max)
        res = run(env, v, L, o, args.pre_steps)
        for key, val in res.items():
            results[f"t{int(tilt)}_{key}"] = val
        print(f"tilt {tilt}: done ({len(grid)} envs)", flush=True)
    np.savez(args.out, vpeak=np.array(v), length=np.array(L), offset=np.array(o),
             tilts=np.array(args.tilts), dt=0.02, **results)
    print("saved", args.out)


if __name__ == "__main__":
    main()

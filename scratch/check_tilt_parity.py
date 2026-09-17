"""Sim-level parity check: tilted env vs original env, across tilt angles."""
import math, sys, os
sys.path.insert(0, "/workspace/examples/rigid")
import numpy as np
import torch
import genesis as gs

gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)

from env_franka_parallel import FrankaEnvParallel as BaseEnv
from env_franka_parallel_tilted import FrankaEnvParallelTilted as TiltEnv

N = 8
STEPS = 120

def rollout(env, steps, seed=0):
    torch.manual_seed(seed)
    obs_td = env.reset()
    rows = []
    for i in range(steps):
        a = torch.zeros(env.num_envs, 3, device=gs.device)
        obs_td, rew, reset, extras = env.step(a)
        rows.append((obs_td["policy"].detach().cpu().numpy().copy(),
                     rew.detach().cpu().numpy().copy(),
                     reset.detach().cpu().numpy().copy()))
    return rows

def frame_report(env, tag):
    axis = env.task_axes[:, :, 2]
    world_z = torch.tensor([0.0, 0.0, 1.0], device=axis.device).expand_as(axis)
    ang = torch.rad2deg(torch.acos((axis * world_z).sum(-1).clamp(-1, 1)))
    R = env.task_axes
    orth = (torch.einsum("nji,njk->nik", R, R) - torch.eye(3, device=R.device)).abs().max()
    det = torch.linalg.det(R)
    print(f"[{tag}] task_origin z          = {env.task_origin[:,2].mean().item():.9f} "
          f"(spread {env.task_origin[:,2].std().item():.2e})")
    print(f"[{tag}] HOME_TASK_Z            = {env.HOME_TASK_Z:.9f}")
    print(f"[{tag}] origin_z - HOME_TASK_Z = {(env.task_origin[:,2].mean().item() - env.HOME_TASK_Z)*1000:.4f} mm")
    print(f"[{tag}] axis tilt vs world z   = {ang.mean().item():.6f} deg (max dev {(ang-ang.mean()).abs().max().item():.2e})")
    print(f"[{tag}] axis dir               = {axis[0].cpu().numpy()}")
    print(f"[{tag}] R orthonormality err   = {orth.item():.2e}, det = {det.mean().item():.9f}")
    print(f"[{tag}] q_home                 = {[round(v,6) for v in env.q_home[:7].tolist()]}")

OBS_NAMES = ["ee_z","ee_vz","tgt_vz","tgt_az","Lf","Rf","rel_z","rel_x","rel_y","des_z"]

results = {}

# ---- original env reference ----
base = BaseEnv(num_envs=N, vis=False)
base_rows = rollout(base, STEPS)
base_obs0 = base_rows[0][0]
print("\n=== ORIGINAL env_franka_parallel ===")
ee = base.ee_link.get_pos()
print("post-reset ee_pos mean:", ee.mean(0).cpu().numpy())
print("obs[0] after 1 step:", dict(zip(OBS_NAMES, np.round(base_obs0.mean(0), 6))))
results["base"] = np.stack([r[0] for r in base_rows])
base_rew = np.stack([r[1] for r in base_rows])
del base
gs.destroy()

for tilt, gains in [(0.0, dict(rot_gain=4.0, transverse_pos_gain=8.0)),
                    (0.0, {}), (15.0, {}), (30.0, {}), (45.0, {})]:
    gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
    from env_franka_parallel_tilted import FrankaEnvParallelTilted as TiltEnv
    env = TiltEnv(num_envs=N, vis=False, tilt_deg=tilt, **gains)
    tag = f"tilt={tilt:g}" + (" basegains" if gains else "")
    print(f"\n=== TILTED {tag} ===")
    frame_report(env, tag)
    rows = rollout(env, STEPS)
    O = np.stack([r[0] for r in rows])
    R = np.stack([r[1] for r in rows])
    print("obs[0] after 1 step:", dict(zip(OBS_NAMES, np.round(O[0].mean(0), 6))))
    print("obs[-1] final     :", dict(zip(OBS_NAMES, np.round(O[-1].mean(0), 6))))
    # transverse drift in task frame over the rollout
    ee = env.ee_link.get_pos()
    tr = env._world_vector_to_task(ee - env.task_origin)
    print(f"final transverse drift (task x,y) mm = {(tr[:,:2]*1000).abs().max().item():.3f}")
    print(f"final task z = {env._task_position_z(ee).mean().item():.6f}")
    print(f"reward sum mean = {R.sum(0).mean():.2f}   any reset = {np.stack([r[2] for r in rows]).any()}")
    d = np.abs(O - results["base"]).max(axis=(0,1))
    print("max |obs - base_obs| per channel:", dict(zip(OBS_NAMES, np.round(d, 6))))
    print(f"max |rew - base_rew| = {np.abs(R - base_rew).max():.6f}")
    del env
    gs.destroy()

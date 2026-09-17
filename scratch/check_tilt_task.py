"""Scripted-pulse episodes at several tilts: is the task still the same task?"""
import sys
sys.path.insert(0, "/workspace/examples/rigid")
import numpy as np, torch, genesis as gs

N, STEPS = 16, 450
OBS = ["ee_z","ee_vz","tgt_vz","tgt_az","Lf","Rf","rel_z","rel_x","rel_y","des_z"]
TERMS = ["ep_fail_rel_x","ep_fail_rel_y","ep_fail_fingertip","ep_fail_rel_z",
         "ep_fail_ee_low","ep_fail_ee_high","ep_fail_regrasp_limit","ep_success","ep_timeout"]

def run(cls, kw, tag):
    gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
    env = cls(num_envs=N, vis=False, **kw)
    torch.manual_seed(0)
    obs = env.reset()["policy"]
    counts = {k: 0 for k in TERMS}
    n_regrasp = 0
    slips, drift, eez, rel = [], [], [], []
    pulse_pre = None
    for i in range(STEPS):
        a = torch.zeros(N, 3, device=gs.device)
        # trigger a pulse every 80 high-level steps (lockout is 60)
        if i % 80 == 79:
            a[:, 1:] = 1.0
            pulse_pre = obs[:, 6].clone()
        obs_td, rew, reset, _ = env.step(a)
        obs = obs_td["policy"]
        t = env.last_reward_terms
        ev = t["regrasp_event"].bool()
        if ev.any():
            n_regrasp += int(ev.sum())
            if pulse_pre is not None:
                slips.append((obs[:, 6] - pulse_pre)[ev].cpu().numpy())
        if reset.any():
            for k in TERMS:
                counts[k] += int((t[k].bool() & reset).sum())
        ee = env.ee_link.get_pos()
        tr = env._world_vector_to_task(ee - env.task_origin) if hasattr(env, "task_axes") else (ee - ee)
        drift.append(tr[:, :2].abs().max().item())
        eez.append(obs[:, 0].mean().item()); rel.append(obs[:, 6].mean().item())
    print(f"\n=== {tag} ===")
    print(" regrasp events:", n_regrasp)
    if slips:
        s = np.concatenate(slips)
        print(f" per-pulse rel_z change: mean {s.mean()*1000:.2f} mm  min {s.min()*1000:.2f}  max {s.max()*1000:.2f}")
    print(" max transverse drift (mm):", round(max(drift)*1000, 3))
    print(" ee_z range over episode:", round(min(eez), 4), "-", round(max(eez), 4))
    print(" rel_z range over episode:", round(min(rel), 4), "-", round(max(rel), 4))
    print(" terminations:", {k: v for k, v in counts.items() if v})
    del env; gs.destroy()

from env_franka_parallel import FrankaEnvParallel as BaseEnv
from env_franka_parallel_tilted import FrankaEnvParallelTilted as TiltEnv
run(BaseEnv, {}, "ORIGINAL env_franka_parallel")
for a in (0.0, 45.0):
    run(TiltEnv, dict(tilt_deg=a), f"TILTED {a:g} deg")

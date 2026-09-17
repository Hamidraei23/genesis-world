"""Strict tilt=0 parity: tilted env with base gains must reproduce env_franka_parallel."""
import sys
sys.path.insert(0, "/workspace/examples/rigid")
import numpy as np, torch, genesis as gs

N, STEPS = 8, 150
OBS = ["ee_z","ee_vz","tgt_vz","tgt_az","Lf","Rf","rel_z","rel_x","rel_y","des_z"]

def rollout(cls, kw):
    gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
    env = cls(num_envs=N, vis=False, **kw)
    torch.manual_seed(0); np.random.seed(0)
    env.reset()
    O, R, D, T = [], [], [], []
    for i in range(STEPS):
        a = torch.zeros(N, 3, device=gs.device)
        obs, rew, reset, _ = env.step(a)
        O.append(obs["policy"].cpu().numpy().copy()); R.append(rew.cpu().numpy().copy())
        D.append(reset.cpu().numpy().copy())
        T.append(env.last_reward_terms["ep_fail"].cpu().numpy().copy())
    info = {}
    if hasattr(env, "task_origin"):
        info["origin_z"] = env.task_origin[:,2].mean().item()
        info["axis"] = env.task_axes[0,:,2].cpu().numpy()
    info["q_home"] = [round(v,6) for v in env.q_home[:7].tolist()]
    info["target_center"] = env.target_center[0].cpu().numpy()
    info["target_z"] = env.target_z[0].item()
    del env; gs.destroy()
    return np.stack(O), np.stack(R), np.stack(D), info

from env_franka_parallel import FrankaEnvParallel as BaseEnv
from env_franka_parallel_tilted import FrankaEnvParallelTilted as TiltEnv

bO, bR, bD, bI = rollout(BaseEnv, {})
tO, tR, tD, tI = rollout(TiltEnv, dict(tilt_deg=0.0, rot_gain=4.0, transverse_pos_gain=8.0))
dO, dR, dD, dI = rollout(TiltEnv, dict(tilt_deg=0.0))

print("\n##### BASE info:", bI)
print("##### TILT0-basegains info:", tI)
print("##### TILT0-defaultgains info:", dI)

def cmp(name, O, R, D):
    print(f"\n--- {name} vs base ---")
    d = np.abs(O - bO)
    print("max|dobs| per channel:", dict(zip(OBS, np.round(d.max(axis=(0,1)), 6))))
    print("max|dobs| at step 1  :", dict(zip(OBS, np.round(np.abs(O[0]-bO[0]).max(0), 8))))
    print("max|drew| =", round(float(np.abs(R-bR).max()), 6), " reset mismatch =", int((D!=bD).sum()))
    # first step where ee_z diverges > 1mm
    k = np.where(d[:,:,0].max(1) > 1e-3)[0]
    print("first step |dee_z|>1mm:", int(k[0]) if len(k) else "never")

cmp("TILT0 basegains", tO, tR, tD)
cmp("TILT0 defaultgains", dO, dR, dD)
print("\nbase ee_z traj  :", np.round(bO[::25,:,0].mean(1), 5))
print("tilt0 bg ee_z   :", np.round(tO[::25,:,0].mean(1), 5))
print("tilt0 def ee_z  :", np.round(dO[::25,:,0].mean(1), 5))
print("base rel_z traj :", np.round(bO[::25,:,6].mean(1), 5))
print("tilt0 bg rel_z  :", np.round(tO[::25,:,6].mean(1), 5))
print("tilt0 def rel_z :", np.round(dO[::25,:,6].mean(1), 5))

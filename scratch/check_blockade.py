"""Drive max acceleration from a real reset and read z_acc_penalty across the boundary."""
import sys
sys.path.insert(0, "/workspace/examples/rigid")
import torch, genesis as gs
gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
from env_franka_parallel_tilted import FrankaEnvParallelTilted as Env

env = Env(num_envs=4, vis=False, tilt_deg=45.0)
env.reset()
limit = Env.PULSE_START_MIN_STEPS
print(f"PULSE_START_MIN_STEPS={limit}  BLOCKADE_ACC_PENALTY_SCALE={Env.BLOCKADE_ACC_PENALTY_SCALE}")
rows = []
for i in range(1, 60):
    # alternate full-scale velocity command -> saturates the acceleration limiter
    a = torch.zeros(4, 3, device=gs.device)
    a[:, 0] = 0.12 if i % 2 else -0.12
    env.step(a)
    rows.append((int(env.episode_length_buf[0]),
                 float(env.target_z_acc[0]),
                 float(env.last_reward_terms["z_acc_penalty"][0])))
for buf, acc, pen in rows:
    if buf in (1, 2, 3, limit - 1, limit, limit + 1, limit + 2, limit + 3, 55):
        tag = "BLOCKADE" if buf <= limit else "normal  "
        print(f"  buf={buf:3d}  {tag}  target_z_acc={acc:8.3f}  z_acc_penalty={pen:9.4f}")
inside = [p for b, _, p in rows if b <= limit]
outside = [p for b, _, p in rows if b > limit]
print(f"\nmean inside blockade : {sum(inside)/len(inside):.4f}  (n={len(inside)})")
print(f"mean outside         : {sum(outside)/len(outside):.4f}  (n={len(outside)})")
print(f"ratio                : {(sum(inside)/len(inside))/(sum(outside)/len(outside)):.2f}x")

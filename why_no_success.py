"""Tally which success sub-condition blocks the policy, over many parallel episodes."""
import pickle, sys, torch, genesis as gs
sys.path.insert(0, "examples/rigid")
from rsl_rl.runners import OnPolicyRunner
from env_franka_regrasp import FrankaEnvParallel

LOG, CKPT, ENVS, STEPS = "logs/franka-regrasp-v34", 260, 256, 900
gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=1, performance_mode=True)
env = FrankaEnvParallel(num_envs=ENVS, vis=False, dt=0.001, target_dt=0.02,
                        mix=False, solid_up=False, normalize=True)
runner = OnPolicyRunner(env, pickle.load(open(f"{LOG}/train_cfg.pkl", "rb")), LOG, device=gs.device)
runner.load(f"{LOG}/model_{CKPT}.pt")
policy = runner.get_inference_policy(device=gs.device)

obs = env.reset()
names = ("z error <= 5mm", "lateral <= 15mm", "ee speed < 0.02", "slip speed < 0.01",
         "a regrasp happened", "acc rms <= 4")
blocked = torch.zeros(len(names), device=gs.device)
steps_seen = 0
errors, best_err = [], torch.full((ENVS,), 9.0, device=gs.device)
with torch.no_grad():
    for _ in range(STEPS):
        obs, _, done, _ = env.step(policy(obs))
        rel = env._relative_position()
        eev = env.ee_link.get_vel()
        err = (rel[:, 2] - env.desired_rel_z).abs()
        slip = ((rel[:, 2] - env._prev_cuboid_rel_z) / env.target_period).abs()
        rms = env.last_reward_terms["acceleration_rms"]
        checks = torch.stack([
            err <= 0.005, rel[:, :2].norm(dim=-1) <= 0.015, eev.norm(dim=-1) < 0.02,
            slip < 0.01, env._regrasp_count > 0, rms <= env.SUCCESS_ACC_RMS_MAX,
        ])
        blocked += (~checks).float().sum(dim=1)
        steps_seen += ENVS
        best_err = torch.minimum(best_err, err)
        if done.any():
            errors.append(err[done].clone()); best_err[done] = 9.0

print(f"\nsteps observed: {steps_seen:,} across {ENVS} environments\n")
print(f"{'success condition':<22}{'fails on % of steps':>21}")
for name, count in zip(names, blocked.tolist()):
    print(f"{name:<22}{100*count/steps_seen:>20.1f}%")
if errors:
    final = torch.cat(errors)
    q = torch.tensor([0.05, 0.25, 0.5, 0.75, 0.95], device=gs.device)
    print(f"\nfinal |z error| over {final.numel()} finished episodes, mm")
    for p, v in zip(q.tolist(), torch.quantile(final, q).tolist()):
        print(f"  p{int(p*100):<3}{v*1000:>8.1f}")
    print(f"\n  tolerance for success is 5.0 mm")

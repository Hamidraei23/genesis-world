"""Which condition is the marginal blocker, and how do the EE velocity axes split?"""
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
names = ("z error <= 5mm", "lateral <= 15mm", "ee speed3d < 0.02", "slip speed < 0.01",
         "a regrasp happened", "acc rms <= 4")
sole_blocker = torch.zeros(len(names), device=gs.device)
all_pass = torch.zeros((), device=gs.device)
vz_would_pass = torch.zeros((), device=gs.device)
speed_samples, vz_samples, lateral_speed = [], [], []
best_err = torch.full((ENVS,), 9.0, device=gs.device)
best_seen = []
with torch.no_grad():
    for _ in range(STEPS):
        obs, _, done, _ = env.step(policy(obs))
        rel, eev = env._relative_position(), env.ee_link.get_vel()
        err = (rel[:, 2] - env.desired_rel_z).abs()
        slip = ((rel[:, 2] - env._prev_cuboid_rel_z) / env.target_period).abs()
        checks = torch.stack([
            err <= 0.005, rel[:, :2].norm(dim=-1) <= 0.015, eev.norm(dim=-1) < 0.02,
            slip < 0.01, env._regrasp_count > 0,
            env.last_reward_terms["acceleration_rms"] <= env.SUCCESS_ACC_RMS_MAX,
        ])
        n_fail = (~checks).sum(dim=0)
        for i in range(len(names)):                      # the only condition failing
            sole_blocker[i] += ((n_fail == 1) & ~checks[i]).sum()
        all_pass += (n_fail == 0).sum()
        others = checks.clone(); others[2] = eev[:, 2].abs() < 0.02   # z-only variant
        vz_would_pass += (others.all(dim=0)).sum()
        speed_samples.append(eev.norm(dim=-1)); vz_samples.append(eev[:, 2].abs())
        lateral_speed.append(eev[:, :2].norm(dim=-1))
        best_err = torch.minimum(best_err, err)
        if done.any():
            best_seen.append(best_err[done].clone()); best_err[done] = 9.0

total = STEPS * ENVS
print(f"\nsteps observed {total:,}\n")
print(f"{'the ONLY condition failing on that step':<40}{'% of steps':>12}")
for name, c in zip(names, sole_blocker.tolist()):
    print(f"{name:<40}{100*c/total:>11.2f}%")
print(f"\nall six hold (success_candidate)        {100*all_pass.item()/total:>11.3f}%")
print(f"same but z-axis speed only              {100*vz_would_pass.item()/total:>11.3f}%")
sp, vz, lat = torch.cat(speed_samples), torch.cat(vz_samples), torch.cat(lateral_speed)
print(f"\nEE speed, m/s          median      p90")
print(f"  full 3-D norm      {sp.median():>8.4f} {torch.quantile(sp, 0.9):>8.4f}")
print(f"  z axis only        {vz.median():>8.4f} {torch.quantile(vz, 0.9):>8.4f}")
print(f"  lateral xy         {lat.median():>8.4f} {torch.quantile(lat, 0.9):>8.4f}")
if best_seen:
    b = torch.cat(best_seen)
    q = torch.tensor([0.05, 0.25, 0.5], device=gs.device)
    print(f"\nbest |z error| reached per episode, mm, over {b.numel()} episodes")
    for p, v in zip(q.tolist(), torch.quantile(b, q).tolist()):
        print(f"  p{int(p*100):<3}{v*1000:>8.1f}")

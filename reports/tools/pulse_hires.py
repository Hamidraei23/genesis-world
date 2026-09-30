"""One pulse at 1 kHz resolution, per contact-friction value, in the tilted env.

Wraps scene.step so every physics substep is logged: hand and cuboid velocity
along the fixed grasp axis, the cuboid's in-hand position and the finger
contact forces. Also reports where gravity's transverse component points
relative to the finger-closing axis, which decides whether the released bar
rests on a finger (and so slides against friction) during the pulse.

    python3 reports/tools/pulse_hires.py --tilts 0 40 45 --out reports/data/pulse_hires.npz
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "rigid"))

import genesis as gs
from env_franka_parallel_tilted import FrankaEnvParallelTilted as Env


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tilts", type=float, nargs="+", default=[0.0, 40.0, 45.0])
    p.add_argument("--frictions", type=float, nargs="+", default=[0.05, 0.25, 0.5, 0.75, 1.0])
    p.add_argument("--vpeak", type=float, default=0.45)
    p.add_argument("--length", type=int, default=7)
    p.add_argument("--finger-kp", type=float, default=300.0)
    p.add_argument("--out", type=str, default="reports/data/pulse_hires.npz")
    args = p.parse_args()

    Env.FINGER_GAIN_RANDOM_MIN = Env.FINGER_GAIN_RANDOM_MAX = args.finger_kp
    Env.FRICTION_MIN = Env.FRICTION_MAX = Env.FRICTION_BASE
    Env.PULSE_DELAY_STEPS = 0
    Env.PULSE_START_MIN_STEPS = 0

    results = {}
    first = True
    for tilt in args.tilts:
        if not first:
            gs.destroy()
        gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=0)
        first = False
        N = len(args.frictions)
        env = Env(num_envs=N, tilt_deg=tilt, randomize=False, normalize=False)
        dev = env.device
        env.reset()
        ratio = torch.tensor(args.frictions, device=dev) / Env.FRICTION_BASE
        n_geoms = len(env._contact_geoms_idx)
        env.scene.sim.rigid_solver.set_geoms_friction_ratio(
            ratio.unsqueeze(1).expand(N, n_geoms), geoms_idx=env._contact_geoms_idx, envs_idx=torch.arange(N, device=dev)
        )
        env._gripper_pulse_delays[:] = 0
        env._gripper_pulse_lengths[:] = args.length
        env.desired_rel_z[:] = 0.2

        # Geometry: finger-closing axis and gravity split in the task frame.
        axes = env.task_axes[0]  # columns = task x, y, z in world
        tip_l = env._fingertip_pos(env.left_finger)[0]
        tip_r = env._fingertip_pos(env.right_finger)[0]
        close_dir = (tip_l - tip_r) / (tip_l - tip_r).norm()
        g = torch.tensor([0.0, 0.0, -9.81], device=dev)
        g_task = axes.T @ g
        g_trans = g - axes[:, 2] * (g @ axes[:, 2])
        cos_close = float((g_trans / g_trans.norm().clamp(min=1e-9)) @ close_dir) if g_trans.norm() > 1e-6 else 0.0
        print(f"tilt {tilt}: gravity in task frame {g_task.tolist()}, "
              f"|g_transverse|={float(g_trans.norm()):.3f}, cos(g_trans, finger axis)={cos_close:+.3f}", flush=True)

        log = {k: [] for k in ("hand_v", "obj_v", "rel_z", "f_l", "f_r", "open")}
        orig_step = env.scene.step

        def logged_step(*a, **kw):
            orig_step(*a, **kw)
            hv = env._world_vector_to_task(env.ee_link.get_vel())[:, 2]
            ov = env._world_vector_to_task(env.cuboid.get_vel())[:, 2]
            mid = (env._fingertip_pos(env.left_finger) + env._fingertip_pos(env.right_finger)) / 2
            rz = env._world_vector_to_task(env.cuboid.get_pos() - mid)[:, 2]
            forces = env.franka.get_links_net_contact_force()
            log["hand_v"].append(hv.cpu().numpy())
            log["obj_v"].append(ov.cpu().numpy())
            log["rel_z"].append(rz.cpu().numpy())
            log["f_l"].append(forces[:, env.left_finger.idx_local].norm(dim=-1).cpu().numpy())
            log["f_r"].append(forces[:, env.right_finger.idx_local].norm(dim=-1).cpu().numpy())
            log["open"].append((env._gripper_pulse_steps > 0).float().cpu().numpy())

        env.scene.step = logged_step
        T = env.target_period
        pre, v = 15, args.vpeak
        ramp = int(np.ceil(v / (4.0 * T)))
        rev = pre + ramp
        cmd_log = []
        for k in range(rev + 25):
            if pre <= k < rev:
                vc = min(v, (k - pre + 1) * 4.0 * T)
            elif rev <= k < rev + 11:
                vc = max(-v, v - (k - rev + 1) * 15.0 * T)
            elif k >= rev + 11:
                vc = min(0.0, -v + (k - rev - 10) * 8.0 * T)
            else:
                vc = 0.0
            grip = 1.0 if k == rev else -1.0
            act = torch.tensor([[vc / env.Z_VEL_MAX, grip, grip]], device=dev).expand(N, -1)
            env.step(act.contiguous())
            cmd_log.append(vc)
        key = f"t{int(tilt)}"
        for k2, val in log.items():
            results[f"{key}_{k2}"] = np.stack(val, axis=1)
        results[f"{key}_cmd"] = np.array(cmd_log)
        results[f"{key}_rev_substep"] = np.array(rev * env.target_update_every)
        results[f"{key}_cos_close"] = np.array(cos_close)
        results[f"{key}_g_task"] = g_task.cpu().numpy()
    np.savez(args.out, frictions=np.array(args.frictions), tilts=np.array(args.tilts), dt=0.001,
             vpeak=args.vpeak, length=args.length, **results)
    print("saved", args.out)


if __name__ == "__main__":
    main()

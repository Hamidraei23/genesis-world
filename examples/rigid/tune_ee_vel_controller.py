"""Triangular z-velocity step response for the end-effector controller, with live tuning sliders.

Drives the EE velocity reference 0 -> +0.4 -> -0.4 -> 0 m/s and plots the commanded
velocity against the measured one, the same pair the real-robot velocity log draws.
The policy is bypassed: this runs the env's own target interpolation and
``_control_once`` loop directly, so no reward, termination or gripper pulse
interferes with what you are tuning.

Two sliders change the controller between runs:
  * filter damping ratio  -> the ``_blin`` term of the 2nd-order command filter
  * joint velocity damping -> a scale on the arm's kv = [450 450 350 350 200 200 200]
A third slider moves the filter's natural frequency, which is the delay knob.

Usage:
    python3 examples/rigid/tune_ee_vel_controller.py                  # GUI
    python3 examples/rigid/tune_ee_vel_controller.py --batch \
        --zeta 0.4 --wn 150 --kv-scale 0.6 --out /tmp/resp.png        # one run, no GUI
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import torch
import genesis as gs

from train_franka_ppo import load_env_class


# The env's own joint damping, read at startup so a slider scale of 1.0 always means
# "whatever env_franka_parallel.py currently sets".
BASE_MOTOR_KV = None


# --------------------------------------------------------------------------- #
# Controller knobs                                                            #
# --------------------------------------------------------------------------- #
def apply_filter(env, wn: float, zeta: float):
    """Rebuild the bilinear coefficients of the 2nd-order z-vel command filter."""
    T = env.dt
    k = 2.0 / T
    wn2 = wn * wn
    blin = 2.0 * zeta * wn
    a0 = k * k + blin * k + wn2
    env._filt_b0 = wn2 / a0
    env._filt_b1 = 2.0 * wn2 / a0
    env._filt_b2 = wn2 / a0
    env._filt_a1 = (-2.0 * k * k + 2.0 * wn2) / a0
    env._filt_a2 = (k * k - blin * k + wn2) / a0


def capture_base_kv(env):
    global BASE_MOTOR_KV
    kv = env.franka.get_dofs_kv(dofs_idx_local=env.motors_dof)
    BASE_MOTOR_KV = kv[0].detach().cpu().numpy().copy() if kv.ndim == 2 else kv.detach().cpu().numpy().copy()
    return BASE_MOTOR_KV


def apply_joint_kv(env, scale: float):
    kv = torch.tensor(BASE_MOTOR_KV * scale, dtype=torch.float32, device=env.device)
    envs_idx = torch.arange(env.num_envs, device=env.device)
    env.franka.set_dofs_kv(kv.unsqueeze(0).expand(env.num_envs, -1), env.motors_dof, envs_idx=envs_idx)


def current_filter_params(env):
    """Recover (wn, zeta) from the env's own bilinear coefficients, so the sliders start
    wherever the env currently sits instead of at a value hardcoded here.

    Inverting the discretisation in __init__:  a0 = 4k^2 / (1 + a2 - a1),
    wn^2 = b0 * a0,  and  blin = (a0 - k^2 - wn^2) / k  with k = 2 / dt.
    """
    k = 2.0 / env.dt
    a0 = 4.0 * k * k / (1.0 + env._filt_a2 - env._filt_a1)
    wn2 = env._filt_b0 * a0
    wn = float(np.sqrt(wn2))
    blin = (a0 - k * k - wn2) / k
    return wn, float(blin / (2.0 * wn))


# --------------------------------------------------------------------------- #
# Reference profile                                                           #
# --------------------------------------------------------------------------- #
def trapezoid_reference(target_period, *, vmax, ramp, plateau, hold, tail):
    """0 -> +vmax -> hold -> -vmax -> hold -> 0, sampled on the control grid.

    Built by slewing one control step at a time and clamping at each corner, so the
    commanded plateaus sit exactly at +/-vmax. Ramp slope is vmax / ramp; the crossing
    from +vmax to -vmax therefore takes twice the ramp time on its own.
    """
    dv = (vmax / ramp) * target_period
    n_plateau = max(1, int(round(plateau / target_period)))
    ref = [0.0] * int(round(hold / target_period))
    v = 0.0
    for goal, hold_after in ((vmax, n_plateau), (-vmax, n_plateau), (0.0, 0)):
        while abs(v - goal) > 1e-9:
            v += float(np.clip(goal - v, -dv, dv))
            ref.append(v)
        ref += [v] * hold_after
    ref += [v] * int(round(tail / target_period))
    return np.asarray(ref)


def describe_reference(ref, target_period, vmax):
    """Segment durations the grid actually produced, in ms."""
    ms = target_period * 1000.0
    tol = 1e-6
    pos = np.flatnonzero(np.abs(ref - vmax) < tol)
    neg = np.flatnonzero(np.abs(ref + vmax) < tol)
    moving = np.flatnonzero(np.abs(ref) > tol)
    if pos.size == 0 or neg.size == 0 or moving.size == 0:
        return None
    start = moving[0] - 1                      # last sample still at zero
    back_to_zero = np.flatnonzero(np.abs(ref[neg[-1]:]) < tol)
    fall = back_to_zero[0] if back_to_zero.size else len(ref) - neg[-1]
    return {
        "rise": (pos[0] - start) * ms,
        "hold_pos": (pos.size - 1) * ms,   # the arrival sample belongs to the rise
        "cross": (neg[0] - pos[-1]) * ms,
        "hold_neg": (neg.size - 1) * ms,
        "fall": fall * ms,
    }


# --------------------------------------------------------------------------- #
# One run                                                                     #
# --------------------------------------------------------------------------- #
def run_profile(env, ref_steps, *, hold_gripper=True):
    """Drive the env's controller directly. Returns a dict of 1 kHz traces."""
    env.reset(warmup_steps=50)

    rec = {k: [] for k in ("t", "request", "reference", "filtered", "actual", "ee_z")}
    grip = env.gripper_pos_min.unsqueeze(0).expand(env.num_envs, -1)

    for req in ref_steps:
        new_z_vel = torch.full((env.num_envs,), float(req), device=env.device)
        # Same acceleration clamp and trapezoid integration step() uses.
        max_dv = env.Z_ACC_MAX * env.target_period
        new_z_vel = new_z_vel.clamp(env.target_z_vel - max_dv, env.target_z_vel + max_dv)
        new_z_acc = (new_z_vel - env.target_z_vel) / env.target_period
        new_z = env.target_z + 0.5 * (env.target_z_vel + new_z_vel) * env.target_period

        env._seg_start = torch.stack([env.target_z, env.target_z_vel, env.target_z_acc], dim=-1)
        env._seg_end = torch.stack([new_z, new_z_vel, new_z_acc], dim=-1)
        env._seg_t0 = torch.full((env.num_envs,), env.sim_step * env.dt, device=env.device)
        env.target_z, env.target_z_vel, env.target_z_acc = new_z, new_z_vel, new_z_acc

        if hold_gripper:
            env.franka.control_dofs_position(grip, dofs_idx_local=env.fingers_dof)

        for local_step in range(env.target_update_every):
            t = (env.sim_step + local_step) * env.dt
            tz, tz_vel = env._sample_target_z(t)
            env._control_once(tz, tz_vel)
            env.scene.step()
            rec["t"].append(t + env.dt)
            rec["request"].append(float(req))
            rec["reference"].append(float(tz_vel[0]))
            rec["filtered"].append(float(env._filt_y1[0]))
            rec["actual"].append(float(env.ee_link.get_vel()[0, 2]))
            rec["ee_z"].append(float(env.ee_link.get_pos()[0, 2]))
        env.sim_step += env.target_update_every

    out = {k: np.asarray(v) for k, v in rec.items()}
    out["period"] = env.target_period
    return out


def metrics(rec, dt):
    """Lag from cross-correlation, peak overshoot per lobe, RMS error."""
    ref = rec["reference"] - rec["reference"].mean()
    act = rec["actual"] - rec["actual"].mean()
    n = len(ref)
    max_shift = int(round(0.15 / dt))
    xc = np.correlate(act, ref, mode="full")
    centre = n - 1
    lo, hi = centre - max_shift, centre + max_shift + 1
    lag_steps = int(np.argmax(xc[lo:hi])) - max_shift
    lag_ms = lag_steps * dt * 1000.0

    r, a = rec["reference"], rec["actual"]
    pos = a.max() / r.max() - 1.0 if r.max() > 1e-6 else float("nan")
    neg = a.min() / r.min() - 1.0 if r.min() < -1e-6 else float("nan")
    rms = float(np.sqrt(np.mean((a - r) ** 2)))
    return {"lag_ms": lag_ms, "overshoot_pos": 100.0 * pos, "overshoot_neg": 100.0 * neg, "rms": rms}


# --------------------------------------------------------------------------- #
# Plotting                                                                    #
# --------------------------------------------------------------------------- #
def draw(axes, rec, m, wn, zeta, kv_scale):
    ax_v, ax_e = axes
    ax_v.clear()
    ax_e.clear()

    t = rec["t"]
    rate = 1.0 / rec["period"] if "period" in rec else 50.0
    ax_v.plot(t, rec["request"], color="tab:gray", ls="--", lw=1.1,
              label=f"request ({rate:.0f} Hz)")
    ax_v.plot(t, rec["reference"], color="tab:blue", lw=1.6, label="commanded twist z")
    ax_v.plot(t, rec["filtered"], color="tab:orange", lw=1.2, alpha=0.8, label="after command filter")
    ax_v.plot(t, rec["actual"], color="tab:red", lw=1.6, label="actual twist z")
    ax_v.axhline(0.0, color="k", lw=0.8, alpha=0.5)
    ax_v.set_ylabel("Z velocity [m/s]")
    ax_v.legend(loc="upper right", fontsize=8, ncol=2)
    ax_v.grid(alpha=0.3)
    ax_v.set_title(
        f"wn {wn:.0f} rad/s   zeta {zeta:.2f}   joint kv x{kv_scale:.2f}   |   "
        f"lag {m['lag_ms']:+.0f} ms   peak err {m['overshoot_pos']:+.1f}% / {m['overshoot_neg']:+.1f}%   "
        f"rms {m['rms']:.4f} m/s",
        fontsize=9,
    )

    ax_e.plot(t, rec["actual"] - rec["reference"], color="tab:purple", lw=1.3)
    ax_e.axhline(0.0, color="k", lw=0.8, alpha=0.5)
    ax_e.set_ylabel("actual - cmd [m/s]")
    ax_e.set_xlabel("time [s]")
    ax_e.grid(alpha=0.3)


def free_port(start=8988, tries=20):
    """First port WebAgg can actually bind, so the URL we print is the one it serves on."""
    import socket
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start


def pick_interactive_backend(matplotlib, port=None):
    """First backend that actually imports; None when the environment is headless.

    WebAgg is the fallback that works from a container with no GUI toolkit: it serves
    the figure, sliders and all, over HTTP. Bind it to every interface so a browser
    on the host can reach the container.
    """
    for name in ("TkAgg", "QtAgg", "GTK3Agg", "WebAgg"):
        try:
            if name == "WebAgg":
                port = free_port() if port is None else port
                matplotlib.rcParams["webagg.address"] = "0.0.0.0"
                matplotlib.rcParams["webagg.port"] = port
                matplotlib.rcParams["webagg.port_retries"] = 1
                matplotlib.rcParams["webagg.open_in_browser"] = False
            import matplotlib.pyplot as _plt
            _plt.switch_backend(name)   # actually loads it, unlike matplotlib.use()
            if name == "WebAgg":
                import socket
                try:
                    host = socket.gethostbyname(socket.gethostname())
                except Exception:
                    host = "localhost"
                print("=" * 72)
                print(f"  OPEN THIS IN YOUR BROWSER:   http://{host}:{port}")
                print("  Ignore the 0.0.0.0 URL matplotlib prints below: 0.0.0.0 is the bind")
                print("  address, not a reachable host. This container is on a docker bridge")
                print("  with no published ports, so localhost on the host will not work either.")
                print("=" * 72)
            return name
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
def build_env(args):
    env_cls = load_env_class(args.env)
    return env_cls(num_envs=1, vis=args.vis, dt=args.dt, target_dt=args.target_dt)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default="env_franka_parallel")
    p.add_argument("--dt", type=float, default=0.001)
    p.add_argument("--target_dt", type=float, default=0.02)
    p.add_argument("--vmax", type=float, default=0.4, help="triangle peak velocity [m/s]")
    p.add_argument("--ramp", type=float, default=0.03,
                   help="seconds to go from 0 to vmax; 0.03 s at 0.4 m/s is 13.3 m/s^2")
    p.add_argument("--plateau", type=float, default=0.03,
                   help="seconds held at each of +vmax and -vmax")
    p.add_argument("--hold", type=float, default=0.1, help="quiet seconds before the triangle")
    p.add_argument("--tail", type=float, default=0.2, help="quiet seconds after the triangle")
    p.add_argument("--zeta", type=float, default=None, help="filter damping ratio [default: env value]")
    p.add_argument("--wn", type=float, default=None, help="filter natural frequency [rad/s]")
    p.add_argument("--kv-scale", type=float, default=1.0, help="scale on the arm joint kv")
    p.add_argument("--port", type=int, default=None,
                   help="port for the browser GUI [default: first free one from 8988]")
    p.add_argument("--batch", action="store_true", help="one run, save a PNG, no GUI")
    p.add_argument("--vis", action="store_true", help="open the Genesis viewer too")
    p.add_argument("--out", default=None, help="PNG path [default: ee_vel_response.png next to this file]")
    args = p.parse_args()

    import matplotlib
    if args.batch:
        matplotlib.use("Agg")
    else:
        backend = pick_interactive_backend(matplotlib, args.port)
        if backend is None:
            print("[warn] no interactive matplotlib backend in this environment "
                  "(install one with: apt-get install -y python3-tk), falling back to --batch")
            matplotlib.use("Agg")
            args.batch = True
        else:
            print(f"[ok] GUI backend: {backend}")
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Button, Slider

    gs.init(backend=gs.gpu, precision="32", logging_level="warning")
    env = build_env(args)

    capture_base_kv(env)
    print(f"  env joint kv: {np.array2string(BASE_MOTOR_KV, precision=1)}")
    wn0, zeta0 = current_filter_params(env)
    wn0 = args.wn if args.wn is not None else wn0
    zeta0 = args.zeta if args.zeta is not None else zeta0
    out = args.out or str(Path(__file__).resolve().parent / "ee_vel_response.png")

    ref_steps = trapezoid_reference(env.target_period, vmax=args.vmax, ramp=args.ramp,
                                    plateau=args.plateau, hold=args.hold, tail=args.tail)

    slope = args.vmax / args.ramp
    print(f"  ramp {args.ramp * 1000:.0f} ms to {args.vmax:.2f} m/s  ->  {slope:.1f} m/s^2 "
          f"(Z_ACC_MAX is {env.Z_ACC_MAX:.1f})")
    seg = describe_reference(ref_steps, env.target_period, args.vmax)
    if seg is not None:
        print(f"  on the {env.target_period * 1000:.0f} ms grid: rise {seg['rise']:.0f} ms, "
              f"hold +{args.vmax:.2f} for {seg['hold_pos']:.0f} ms, cross {seg['cross']:.0f} ms, "
              f"hold -{args.vmax:.2f} for {seg['hold_neg']:.0f} ms, fall {seg['fall']:.0f} ms")
    max_dv = env.Z_ACC_MAX * env.target_period
    need_dv = float(np.abs(np.diff(ref_steps)).max())
    if need_dv > max_dv + 1e-9:
        print(f"[warn] the profile asks for {need_dv:.3f} m/s per step but the acceleration clamp "
              f"allows {max_dv:.3f}; the triangle will be flattened by the clamp, not by the controller.")

    def one_run(wn, zeta, kv_scale):
        apply_filter(env, wn, zeta)
        apply_joint_kv(env, kv_scale)
        rec = run_profile(env, ref_steps)
        m = metrics(rec, env.dt)
        print(f"  wn {wn:6.1f}  zeta {zeta:4.2f}  kv x{kv_scale:4.2f}  ->  "
              f"lag {m['lag_ms']:+5.0f} ms   peak err {m['overshoot_pos']:+6.1f}% / "
              f"{m['overshoot_neg']:+6.1f}%   rms {m['rms']:.4f} m/s")
        return rec, m

    if args.batch:
        fig, axes = plt.subplots(2, 1, sharex=True, figsize=(11, 6),
                                 gridspec_kw={"height_ratios": [2.2, 1]})
        rec, m = one_run(wn0, zeta0, args.kv_scale)
        draw(axes, rec, m, wn0, zeta0, args.kv_scale)
        fig.tight_layout()
        fig.savefig(out, dpi=130)
        print(f"[ok] plot -> {out}")
        return

    # ---------------- interactive ----------------
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(11, 7.6),
                             gridspec_kw={"height_ratios": [2.2, 1]})
    fig.subplots_adjust(bottom=0.30)

    s_zeta = Slider(fig.add_axes([0.12, 0.19, 0.62, 0.03]), "filter damping  zeta",
                    0.05, 1.50, valinit=zeta0, valstep=0.01)
    s_wn = Slider(fig.add_axes([0.12, 0.14, 0.62, 0.03]), "filter freq  wn [rad/s]",
                  40.0, 600.0, valinit=wn0, valstep=5.0)
    s_kv = Slider(fig.add_axes([0.12, 0.09, 0.62, 0.03]), "joint kv  scale",
                  0.10, 2.00, valinit=args.kv_scale, valstep=0.05)
    b_run = Button(fig.add_axes([0.80, 0.15, 0.08, 0.05]), "Run")
    b_save = Button(fig.add_axes([0.89, 0.15, 0.08, 0.05]), "Save")

    state = {}

    def rerun(_event=None):
        wn, zeta, kv = s_wn.val, s_zeta.val, s_kv.val
        axes[0].set_title("running...", fontsize=9)
        fig.canvas.draw_idle()
        fig.canvas.flush_events()
        rec, m = one_run(wn, zeta, kv)
        state["rec"], state["m"], state["p"] = rec, m, (wn, zeta, kv)
        draw(axes, rec, m, wn, zeta, kv)
        fig.canvas.draw_idle()

    def save(_event=None):
        fig.savefig(out, dpi=130)
        print(f"[ok] plot -> {out}")

    b_run.on_clicked(rerun)
    b_save.on_clicked(save)

    print("Sliders set the controller; press Run to simulate again. Numbers print here each run.")
    rerun()
    plt.show()


if __name__ == "__main__":
    main()

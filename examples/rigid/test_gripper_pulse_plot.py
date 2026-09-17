"""Record one gripper open→close pulse in Genesis and plot it like the real-robot log.

Produces the same two panels the franka_controllers velocity log draws:
  top    — measured finger gap [mm]  (left finger joint + right finger joint)
  bottom — joint torques as % of each joint's own limit, with the ±100 % lines

Sampling is at the simulation rate (1 kHz by default), not the policy rate, so the
staircase of the finger response is visible the same way it is on hardware.

Usage:
    python3 examples/rigid/test_gripper_pulse_plot.py
    python3 examples/rigid/test_gripper_pulse_plot.py --pulse-length 6 --pulse-delay 1
    python3 examples/rigid/test_gripper_pulse_plot.py --sweep --out /tmp/pulse.png
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import torch
import genesis as gs

from train_franka_ppo import load_env_class


# Same limits the real-robot plotter uses, so the two figures are comparable.
TAU_LIMITS = np.array([87.0, 87.0, 87.0, 87.0, 12.0, 12.0, 12.0])

PHASE_COLORS = {"delay": "tab:gray", "open": "tab:green", "close": "tab:orange"}


class PulseRecorder:
    """Samples finger gap and joint torques after every simulator step."""

    def __init__(self, env):
        self.env = env
        self.t = []
        self.gap = []       # mm, measured
        self.tau = []       # Nm, 7 joints — PD/actuator command torque
        self.tau_int = []   # Nm, 7 joints — solver-internal dof force
        self.obj_z = []     # m, cuboid centre height
        self.grip_z = []    # m, fingertip midpoint height
        self._n = 0
        self._t0 = env.sim_step * env.dt
        self._orig_step = env.scene.step
        env.scene.step = self._step

    def _step(self, *args, **kwargs):
        self._orig_step(*args, **kwargs)
        self.sample()

    def sample(self):
        env = self.env
        q = env.franka.get_dofs_position(dofs_idx_local=env.fingers_dof)[0]
        tau = env.franka.get_dofs_control_force(dofs_idx_local=env.motors_dof)[0]
        tau_int = env.franka.get_dofs_force(dofs_idx_local=env.motors_dof)[0]
        self._n += 1
        self.t.append(self._t0 + self._n * env.dt)
        self.gap.append(float(q.sum()) * 1000.0)
        self.tau.append(tau.detach().cpu().numpy().copy())
        self.tau_int.append(tau_int.detach().cpu().numpy().copy())
        obj_z = float(env.cuboid.get_pos()[0, 2])
        mid_z = 0.5 * float(
            env._fingertip_pos(env.left_finger)[0, 2] + env._fingertip_pos(env.right_finger)[0, 2]
        )
        self.obj_z.append(obj_z)
        self.grip_z.append(mid_z)

    def detach(self):
        self.env.scene.step = self._orig_step

    def arrays(self, source="control"):
        tau = self.tau if source == "control" else self.tau_int
        return np.asarray(self.t), np.asarray(self.gap), np.asarray(tau)

    def object_arrays(self):
        return np.asarray(self.obj_z), np.asarray(self.grip_z)


def phase_of(counter, length):
    if counter > length:
        return "delay"
    if counter >= 2:
        return "open"
    if counter == 1:
        return "close"
    return "idle"


def run_pulse(env, *, settle_steps, after_steps, pulse_length=None, pulse_delay=None,
              pulses=1, spacing=20, spike=False):
    """Hold still, fire `pulses` pulses `spacing` high-level steps apart, keep holding."""
    env.reset()
    if pulse_length is not None:
        env._gripper_pulse_lengths[:] = pulse_length
    if pulse_delay is not None:
        env._gripper_pulse_delays[:] = pulse_delay

    used_L = int(env._gripper_pulse_lengths[0])
    used_D = int(env._gripper_pulse_delays[0])
    rec = PulseRecorder(env)
    spans = []
    cmd_mm = []   # (t0, t1, commanded gap in mm) per high-level step
    trigger_times = []
    closed = torch.tensor([[0.0, -1.0, -1.0]], device=env.device)
    opened = torch.tensor([[0.0, 1.0, 1.0]], device=env.device)

    total = settle_steps + after_steps
    for k in range(total):
        k_rel = k - settle_steps
        trigger = (
            k_rel >= 0
            and k_rel % spacing == 0
            and k_rel // spacing < pulses
        )
        counter = int(env._gripper_pulse_steps[0])
        length = int(env._gripper_pulse_lengths[0])
        delay = int(env._gripper_pulse_delays[0])
        if counter == 0 and trigger:
            counter = length + delay
        # A trained policy keeps asking for "open" while the pulse plays out. Dropping
        # back to "closed" right after the trigger (--spike) makes the fingers bounce
        # during the delay steps, which is a driver artefact, not env behaviour.
        want_open = trigger or (not spike and counter > 0)
        t0 = env.sim_step * env.dt
        ph = phase_of(counter, length)
        spans.append((t0, t0 + env.target_period, ph))
        if ph == "open":
            gap_cmd = 2.0 * float(env.gripper_pos_max[0])
        elif ph == "close":
            gap_cmd = 2.0 * float(env.gripper_pos_min[0])
        else:  # delay / idle -> the policy command passes through
            raw = 1.0 if want_open else -1.0
            gap_cmd = 2.0 * float(
                env.gripper_pos_min[0]
                + (raw + 1.0) * 0.5 * (env.gripper_pos_max[0] - env.gripper_pos_min[0])
            )
        cmd_mm.append((t0, t0 + env.target_period, gap_cmd * 1000.0))
        if trigger:
            trigger_times.append(t0)

        _, _, reset_buf, _ = env.step(opened if want_open else closed)
        if bool(reset_buf[0]):
            print(f"[warn] episode terminated at high-level step {k}; recording stops here")
            break

    rec.detach()
    # L/D are read before the run: a mid-run episode reset resamples them.
    return rec, spans, trigger_times, cmd_mm, used_L, used_D


def plot_pulse(rec, spans, trigger_times, cmd_mm, args, out_png, title):
    import matplotlib
    if args.headless:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t, gap, tau = rec.arrays(args.torque_source)
    tau_pct = 100.0 * tau / TAU_LIMITS

    lo = trigger_times[0] - args.pre
    hi = trigger_times[-1] + args.post
    m = (t >= lo) & (t <= hi)

    obj_z, grip_z = rec.object_arrays()
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 7.4),
                             gridspec_kw={"height_ratios": [2, 1.4, 2]})

    ax = axes[0]
    ax.plot(t[m], gap[m], color="tab:purple", lw=1.6, label="measured finger gap")
    if not args.no_command:
        ct = [v for span in cmd_mm for v in (span[0], span[1])]
        cg = [span[2] for span in cmd_mm for _ in range(2)]
        ax.plot(ct, cg, color="tab:gray", ls="--", lw=1.2, label="commanded finger gap")
    ax.set_ylabel("gripper gap [mm]")
    if args.gap_ylim:
        ax.set_ylim(*args.gap_ylim)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.3)
    ax.set_title(title, fontsize=10)

    ax = axes[1]
    ref = np.argmax(t >= trigger_times[0])
    ax.plot(t[m], 1000.0 * (obj_z[m] - obj_z[ref]), color="tab:blue", lw=1.5,
            label="object height")
    ax.plot(t[m], 1000.0 * ((obj_z - grip_z)[m] - (obj_z - grip_z)[ref]), color="tab:red",
            ls="--", lw=1.4, label="object slip vs fingertips")
    ax.axhline(0.0, color="k", lw=0.8, alpha=0.5)
    ax.set_ylabel("object motion [mm]")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[2]
    for i in range(7):
        ax.plot(t[m], tau_pct[m, i], lw=1.2, label=f"J{i + 1}")
    for level in (100.0, -100.0):
        ax.axhline(level, color="tab:red", ls="--", lw=1.0, alpha=0.7)
    ax.set_ylabel("torque [% of limit]")
    ax.set_xlabel("time [s]")
    ax.legend(loc="upper right", fontsize=8, ncol=7, columnspacing=0.8)
    ax.grid(alpha=0.3)

    for a in axes:
        for tt in trigger_times:
            a.axvline(tt, color="red", lw=1.4)
        if args.shade:
            for t0, t1, phase in spans:
                if phase in PHASE_COLORS and t1 > lo and t0 < hi:
                    a.axvspan(t0, t1, color=PHASE_COLORS[phase], alpha=0.12, lw=0)
        a.set_xlim(lo, hi)

    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    print(f"[ok] plot -> {out_png}")
    if not args.headless and matplotlib.get_backend().lower() not in ("agg", "pdf", "ps", "svg"):
        plt.show()


def report(env, spans, rec, trigger_times, pulse_length, pulse_delay):
    dt_ms = env.target_period * 1000.0
    t, gap, _ = rec.arrays()

    print()
    print(f"  control period          {dt_ms:.1f} ms   ({1000.0 / dt_ms:.0f} Hz)")
    print(f"  pulse_delay             {pulse_delay} steps  = {pulse_delay * dt_ms:.0f} ms of policy control")
    print(f"  pulse_length            {pulse_length} steps")
    print(f"  pulses fired            {len(trigger_times)}")

    for i, trig in enumerate(trigger_times):
        nxt = trigger_times[i + 1] if i + 1 < len(trigger_times) else float("inf")
        mine = [s for s in spans if trig <= s[0] < nxt]
        n_delay = sum(1 for s in mine if s[2] == "delay")
        opens = [s for s in mine if s[2] == "open"]
        closes = [s for s in mine if s[2] == "close"]
        t_open0 = opens[0][0] if opens else float("nan")
        t_close0 = closes[0][0] if closes else float("nan")
        m = (t >= trig) & (t < min(nxt, t[-1] + 1.0))
        peak = gap[m].max() if m.any() else float("nan")
        print(f"  -- pulse {i + 1}")
        print(f"     trigger at          {trig:.3f} s")
        print(f"     delay steps         {n_delay}  = {n_delay * dt_ms:.0f} ms")
        print(f"     forced-open window  {len(opens)} steps  = {len(opens) * dt_ms:.0f} ms")
        print(f"     forced-close step   {len(closes)} step   = {len(closes) * dt_ms:.0f} ms")
        print(f"     forced open -> close {(t_close0 - t_open0) * 1000.0:.0f} ms")
        print(f"     open duration        {(t_close0 - trig) * 1000.0:.0f} ms"
              f"   (trigger -> close step)")
        print(f"     peak gap            {peak:.2f} mm")
    if len(trigger_times) > 1:
        seps = [1000.0 * (b - a) for a, b in zip(trigger_times, trigger_times[1:])]
        print(f"  trigger-to-trigger      {', '.join(f'{v:.0f} ms' for v in seps)}")
    cmd_open_mm = 2000.0 * float(env.gripper_pos_max[0])
    cmd_closed_mm = 2000.0 * float(env.gripper_pos_min[0])
    print(f"  commanded gap           {cmd_closed_mm:.2f} mm closed -> {cmd_open_mm:.2f} mm open")
    ref = int(np.argmax(t >= trigger_times[0]))
    rest = gap[ref]
    print(f"  gap before 1st pulse    {rest:.2f} mm   (peak {gap.max():.2f} mm, "
          f"travel {gap.max() - rest:.2f} mm)")
    obj_z, _ = rec.object_arrays()
    print(f"  object drop over run    {1000.0 * (obj_z[ref] - obj_z[-1]):.1f} mm")


def build_env(args):
    env_cls = load_env_class(args.env)
    kwargs = {}
    if args.gripper_open is not None:
        kwargs["gripper_pos_max"] = args.gripper_open
    if args.gripper_closed is not None:
        kwargs["gripper_pos_min"] = args.gripper_closed
    return env_cls(
        num_envs=1,
        vis=args.vis,
        dt=args.dt,
        target_dt=args.target_dt,
        randomize=args.randomize,
        **kwargs,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default="env_franka_parallel", help="env module (same syntax as training)")
    p.add_argument("--dt", type=float, default=0.001)
    p.add_argument("--target_dt", type=float, default=0.02)
    p.add_argument("--randomize", action="store_true",
                   help="sample pulse delay/length per episode as training does with --randomize")
    p.add_argument("--pulse-length", type=int, default=None, help="override PULSE_LENGTH for this run")
    p.add_argument("--pulse-delay", type=int, default=None, help="override PULSE_DELAY_STEPS for this run")
    p.add_argument("--pulses", type=int, default=1, help="how many pulses to fire")
    p.add_argument("--spacing", type=int, default=20,
                   help="high-level steps between pulse triggers (must exceed pulse_delay + pulse_length)")
    p.add_argument("--settle", type=int, default=25, help="high-level steps of quiet hold before the pulse")
    p.add_argument("--after", type=int, default=40, help="high-level steps recorded after the pulse")
    p.add_argument("--pre", type=float, default=0.08, help="seconds of plot before the trigger")
    p.add_argument("--post", type=float, default=0.28, help="seconds of plot after the trigger")
    p.add_argument("--torque-source", choices=("control", "internal"), default="control",
                   help="'control' = PD actuator torque (closest to the measured tau_J on hardware); "
                        "'internal' = solver-internal dof force")
    p.add_argument("--gripper-open", type=float, default=None,
                   help="override the per-finger open target [m]; the commanded gap is twice this")
    p.add_argument("--gripper-closed", type=float, default=None,
                   help="override the per-finger closed target [m]")
    p.add_argument("--gap-ylim", type=float, nargs=2, default=(23.5, 25.5), metavar=("LO", "HI"),
                   help="y limits of the gripper-gap panel [mm]; pass 0 0 to autoscale")
    p.add_argument("--spike", action="store_true",
                   help="send the open action for one step only (makes the fingers bounce during "
                        "the delay steps); default holds it open for the whole pulse, as a policy does")
    p.add_argument("--no-command", action="store_true", help="hide the commanded-gap trace")
    p.add_argument("--shade", action="store_true", help="shade the delay / open / close phases")
    p.add_argument("--sweep", action="store_true",
                   help="also run pulse_length 3..6 and overlay the finger gaps")
    p.add_argument("--vis", action="store_true", help="open the Genesis viewer")
    p.add_argument("--cpu", action="store_true")
    p.add_argument("--headless", action="store_true", help="never open a plot window")
    p.add_argument("--out", default=None, help="output PNG [default: gripper_pulse.png next to this file]")
    args = p.parse_args()
    if tuple(args.gap_ylim) == (0.0, 0.0):
        args.gap_ylim = None

    gs.init(backend=gs.cpu if args.cpu else gs.gpu, precision="32", logging_level="warning")
    env = build_env(args)

    out = args.out or str(Path(__file__).resolve().parent / "gripper_pulse.png")

    rec, spans, trigger_times, cmd_mm, L, D = run_pulse(
        env, settle_steps=args.settle, after_steps=args.after,
        pulse_length=args.pulse_length, pulse_delay=args.pulse_delay,
        pulses=args.pulses, spacing=args.spacing, spike=args.spike,
    )
    if not trigger_times:
        print("[error] no pulse was fired; increase --after")
        return
    title = (f"Genesis gripper pulse x{len(trigger_times)} — target_dt {env.target_period * 1000:.0f} ms, "
             f"pulse_delay {D}, pulse_length {L}")
    plot_pulse(rec, spans, trigger_times, cmd_mm, args, out, title)
    report(env, spans, rec, trigger_times, L, D)

    if args.sweep:
        sweep(env, args, out)


def sweep(env, args, out):
    import matplotlib
    if args.headless:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 3.4))
    combos = [
        (D, L)
        for D in range(env.PULSE_DELAY_RANDOM_MIN, env.PULSE_DELAY_RANDOM_MAX + 1)
        for L in range(env.PULSE_LENGTH_RANDOM_MIN, env.PULSE_LENGTH_RANDOM_MAX + 1)
    ]
    for D, L in combos:
        rec, spans, trigger_times, _, _, _ = run_pulse(
            env, settle_steps=args.settle, after_steps=args.after,
            pulse_length=L, pulse_delay=D,
        )
        t, gap, _ = rec.arrays()
        trigger_t = trigger_times[0]
        m = (t >= trigger_t - args.pre) & (t <= trigger_t + args.post)
        open_ms = (D + L - 1) * env.target_period * 1000.0
        ax.plot(t[m] - trigger_t, gap[m], lw=1.5,
                label=f"delay {D}, length {L}  ({open_ms:.0f} ms open)")

    ax.axvline(0.0, color="red", lw=1.4)
    ax.set_xlabel("time since trigger [s]")
    ax.set_ylabel("gripper gap [mm]")
    if args.gap_ylim:
        ax.set_ylim(*args.gap_ylim)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_title("Finger gap over the randomised delay / length combinations", fontsize=10)
    fig.tight_layout()
    sweep_png = str(Path(out).with_name(Path(out).stem + "_sweep.png"))
    fig.savefig(sweep_png, dpi=130)
    print(f"[ok] sweep -> {sweep_png}")


if __name__ == "__main__":
    main()

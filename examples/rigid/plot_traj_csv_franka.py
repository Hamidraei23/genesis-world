"""
Plot the trajectory CSVs written by eval_pulse_grid_traj_franka.py.

Per file, one figure with three stacked panels sharing a time axis:

    1. z velocity   commanded (target_z_vel) vs measured (ee_vel_z)
    2. grasp slip   cuboid_rel_z against the goal and its +-5 mm success band
    3. z position   commanded (target_z) vs measured (ee_z), zeroed at t=0

Grip-released windows (the pulse) are shaded red, zero-hold windows blue, so the
velocity reversal that produces the slip lines up visually with the release.

With more than one file it also writes an overview figure overlaying every run's
slip curve, which is the quickest way to see how pulse timing changes the number
of pulses needed to reach 2 cm.

Time axis: row i is the reference at (i+1)*target_dt -- see README section 3 --
so the plots use (step+1)*dt, not the CSV's own t_s column.

Usage (inside the genesis container, from /workspace):
    # every CSV in a directory
    python3 examples/rigid/plot_traj_csv_franka.py logs/franka-lift-v1-student-ft3/traj_vis

    # specific files, custom output directory
    python3 examples/rigid/plot_traj_csv_franka.py logs/.../traj_d2_l4.csv --out-dir /tmp/plots

    # skip the position panel
    python3 examples/rigid/plot_traj_csv_franka.py logs/.../traj_vis --no-position

Outputs <out-dir>/<csv stem>.png plus overview_slip.png (default out-dir:
<csv dir>/plots).
"""

import argparse
import glob
import os

import matplotlib
matplotlib.use("Agg")  # headless - required inside Docker (no $DISPLAY)
import matplotlib.pyplot as plt
import numpy as np

GRIP_RELEASED_ABOVE = 0.010   # m per finger; the pulse commands 0.0124, closed is 0.000251
SUCCESS_BAND = 0.005          # m; |cuboid_rel_z - desired| <= this counts as on target


def load(path: str) -> dict:
    """CSV -> {column: np.ndarray}, plus the time base and the pulse/hold masks."""
    table = np.genfromtxt(path, delimiter=",", names=True)
    data = {name: np.asarray(table[name], dtype=float) for name in table.dtype.names}

    n = len(data["step"])
    dt = float(data["t_s"][1] - data["t_s"][0]) if n > 1 else 0.02
    data["_t"] = (data["step"] + 1.0) * dt
    data["_dt"] = dt
    data["_released"] = data["grip_cmd_l"] > GRIP_RELEASED_ABOVE
    data["_holding"] = data["zero_hold"] > 0
    data["_label"] = os.path.splitext(os.path.basename(path))[0]
    return data


def spans(mask: np.ndarray, t: np.ndarray, dt: float):
    """Contiguous True runs of `mask` as (t_start, t_end) pairs."""
    out = []
    start = None
    for i, on in enumerate(mask):
        if on and start is None:
            start = t[i] - dt
        elif not on and start is not None:
            out.append((start, t[i] - dt))
            start = None
    if start is not None:
        out.append((start, t[-1]))
    return out


def shade(ax, data: dict, legend: bool = False) -> None:
    """Red = grip released (pulse), blue = zero-hold."""
    for i, (t0, t1) in enumerate(spans(data["_released"], data["_t"], data["_dt"])):
        ax.axvspan(t0, t1, color="tab:red", alpha=0.13, lw=0,
                   label="grip released" if (legend and i == 0) else None)
    for i, (t0, t1) in enumerate(spans(data["_holding"], data["_t"], data["_dt"])):
        ax.axvspan(t0, t1, color="tab:blue", alpha=0.10, lw=0,
                   label="zero-hold" if (legend and i == 0) else None)


def plot_one(data: dict, out_path: str, with_position: bool) -> None:
    t = data["_t"]
    n_panels = 3 if with_position else 2
    fig, axes = plt.subplots(n_panels, 1, figsize=(11, 3.0 * n_panels + 1.0),
                             sharex=True, constrained_layout=True)

    # ---- 1. velocity -------------------------------------------------------
    ax = axes[0]
    shade(ax, data, legend=True)
    ax.plot(t, data["target_z_vel"], color="tab:blue", lw=2.0,
            label="commanded  target_z_vel")
    ax.plot(t, data["ee_vel_z"], color="tab:orange", lw=1.6, alpha=0.9,
            label="measured  ee_vel_z")
    ax.axhline(0.0, color="0.6", lw=0.8)
    ax.set_ylabel("z velocity  [m/s]")
    rms = float(np.sqrt(np.mean((data["target_z_vel"] - data["ee_vel_z"]) ** 2)))
    ax.set_title(f"{data['_label']}   |   tracking RMS {rms * 1e3:.1f} mm/s   |   "
                 f"peak |v| {np.abs(data['target_z_vel']).max():.2f} m/s")
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    ax.grid(alpha=0.25)

    # ---- 2. grasp slip -----------------------------------------------------
    ax = axes[1]
    shade(ax, data)
    goal = float(data["desired_rel_z"][-1])
    ax.axhspan(goal - SUCCESS_BAND, goal + SUCCESS_BAND, color="tab:green",
               alpha=0.12, lw=0, label=f"success band  {goal:+.3f} +-{SUCCESS_BAND * 1e3:.0f} mm")
    ax.axhline(goal, color="tab:green", lw=1.2, ls="--")
    ax.plot(t, data["cuboid_rel_z"], color="tab:purple", lw=2.0,
            label="cuboid_rel_z  (grasp point along the bar)")
    ax.axhline(0.0, color="0.6", lw=0.8)
    ax.set_ylabel("relative z  [m]")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)

    # ---- 3. position -------------------------------------------------------
    if with_position:
        ax = axes[2]
        shade(ax, data)
        z0 = data["target_z"][0] - 0.5 * data["target_z_vel"][0] * data["_dt"]
        ax.plot(t, data["target_z"] - z0, color="tab:blue", lw=2.0,
                label="commanded  target_z")
        ax.plot(t, data["ee_z"] - z0, color="tab:orange", lw=1.6, alpha=0.9,
                label="measured  ee_z")
        ax.axhline(0.0, color="0.6", lw=0.8)
        ax.set_ylabel("hand z travel  [m]")
        lag = float(np.mean(data["target_z"] - data["ee_z"]))
        ax.legend(loc="lower left", fontsize=8,
                  title=f"mean tracking offset {lag * 1e3:+.1f} mm", title_fontsize=8)
        ax.grid(alpha=0.25)

    axes[-1].set_xlabel("time  [s]")
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"wrote {out_path}")


def plot_overview(runs: list[dict], out_path: str) -> None:
    fig, ax = plt.subplots(figsize=(11, 6), constrained_layout=True)
    goal = float(runs[0]["desired_rel_z"][-1])
    ax.axhspan(goal - SUCCESS_BAND, goal + SUCCESS_BAND, color="tab:green",
               alpha=0.12, lw=0, label=f"success band  {goal:+.3f} +-{SUCCESS_BAND * 1e3:.0f} mm")
    ax.axhline(goal, color="tab:green", lw=1.2, ls="--")

    cmap = plt.get_cmap("viridis")
    for i, data in enumerate(sorted(runs, key=lambda d: d["_label"])):
        pulses = int(np.sum(np.diff(data["_released"].astype(int)) > 0))
        ax.plot(data["_t"], data["cuboid_rel_z"], lw=1.8,
                color=cmap(i / max(1, len(runs) - 1)),
                label=f"{data['_label']}  ({pulses} pulses, {data['_t'][-1]:.2f} s)")

    ax.axhline(0.0, color="0.6", lw=0.8)
    ax.set_xlabel("time  [s]")
    ax.set_ylabel("cuboid_rel_z  [m]")
    ax.set_title("Grasp-point slip to the 2 cm target, every pulse-timing combination")
    ax.legend(fontsize=8, ncol=2, loc="lower right")
    ax.grid(alpha=0.25)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    print(f"wrote {out_path}")


def main():
    p = argparse.ArgumentParser(
        description="Plot commanded vs measured z velocity and grasp slip for "
                    "trajectory CSVs from eval_pulse_grid_traj_franka.py")
    p.add_argument("paths", nargs="+",
                   help="CSV files, or directories to scan for traj_*.csv")
    p.add_argument("--out-dir", type=str, default=None,
                   help="Default: <directory of the first CSV>/plots")
    p.add_argument("--no-position", dest="position", action="store_false", default=True,
                   help="Drop the hand-position panel")
    p.add_argument("--no-overview", dest="overview", action="store_false", default=True,
                   help="Skip the combined slip figure")
    args = p.parse_args()

    files: list[str] = []
    for path in args.paths:
        if os.path.isdir(path):
            files.extend(sorted(glob.glob(os.path.join(path, "traj_*.csv"))))
        else:
            files.append(path)
    if not files:
        raise SystemExit("no CSV files found")

    out_dir = args.out_dir or os.path.join(os.path.dirname(files[0]) or ".", "plots")
    os.makedirs(out_dir, exist_ok=True)

    runs = []
    for path in files:
        data = load(path)
        runs.append(data)
        plot_one(data, os.path.join(out_dir, f"{data['_label']}.png"), args.position)

    if args.overview and len(runs) > 1:
        plot_overview(runs, os.path.join(out_dir, "overview_slip.png"))


if __name__ == "__main__":
    main()

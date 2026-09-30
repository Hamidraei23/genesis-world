"""Figures and numbers for the tilted-regrasp RL report.

    python3 reports/rl_tilted/make_figs.py

Reads the env constants from examples/rigid/env_franka_parallel_tilted.py (by
parsing, so no Genesis import is needed), the TensorBoard curves extracted to
reports/data/tb_curves.npz, the Genesis pulse sweeps and one policy trajectory.
"""

import ast
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "reports" / "tools"))

from figstyle import BLUE, C, DIVERGING, GRID, INK, INK2, MUTED, SHADE, TEXTWIDTH, label_end, save, setup  # noqa: E402
from slip_model import g_components, second_order_lowpass, simulate_slip  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, Polygon, Rectangle  # noqa: E402

FIGS = HERE / "figs"
FIGS.mkdir(exist_ok=True)
DATA = ROOT / "reports" / "data"
setup()
NUM = {}


def env_constants():
    """Class-level constants of FrankaEnvParallelTilted, read from source."""
    src = (ROOT / "examples" / "rigid" / "env_franka_parallel_tilted.py").read_text()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FrankaEnvParallelTilted")
    out = {}
    for node in cls.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


K = env_constants()
GAMMA = 0.99
T = 0.02
MAX_LEN = 450


# --------------------------------------------------------------------------- #
# Geometry                                                                    #
# --------------------------------------------------------------------------- #
def fig_geometry():
    th = math.radians(45)
    fig, ax = plt.subplots(figsize=(TEXTWIDTH * 0.62, 3.0))
    ax.set_aspect("equal")
    ax.axis("off")
    z = np.array([math.sin(th), math.cos(th)])  # tilted grasp axis in the (Y, Z) plane
    n = np.array([math.cos(th), -math.sin(th)])  # transverse, toward the lower finger
    o = np.array([0.35, 0.35])
    # small world frame in the corner
    w0 = np.array([-0.95, -0.85])
    for d, lab in ((np.array([0, 0.35]), "$Z$"), (np.array([0.35, 0]), "$Y$")):
        ax.annotate("", xy=w0 + d, xytext=w0, arrowprops=dict(arrowstyle="-|>", color=MUTED, lw=0.9))
        ax.text(*(w0 + d * 1.12), lab, color=INK2, fontsize=8, ha="center", va="center")
    ax.text(*(w0 + np.array([0.0, -0.13])), "world", color=INK2, fontsize=7.5)
    # vertical through the bar centre, for the tilt angle
    ax.plot([o[0], o[0]], [o[1], o[1] + 1.05], color=MUTED, lw=0.8, ls=":")
    half_len, half_w = 0.75, 0.07
    corners = [o + s_ * half_len * z + t * half_w * n for s_, t in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    ax.add_patch(Polygon(corners, closed=True, facecolor="#cde2fb", edgecolor=C[0], lw=1.2))
    ax.text(*(o + 0.55 * z + 0.16 * n), "bar (25 mm)", fontsize=8, color=INK2, ha="left", va="center")
    for side, name in ((1, "lower finger"), (-1, "upper finger")):
        c = o + side * (half_w + 0.045) * n
        fc = [c + s_ * 0.14 * z + t * 0.04 * n for s_, t in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        ax.add_patch(Polygon(fc, closed=True, facecolor="#d9d8d3", edgecolor=INK2, lw=1.0))
        tip = c + 0.14 * z
        ax.annotate(name, tip, xytext=tip + side * 0.35 * n + 0.28 * z, fontsize=7.5, color=INK2,
                    ha="left" if side > 0 else "right", va="center",
                    arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7))
    ax.annotate("", xy=o + 1.12 * z, xytext=o + 0.78 * z, arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.4))
    ax.text(*(o + 1.15 * z), r"$\hat z_t$ (grasp axis)", fontsize=8, color=INK, ha="left", va="bottom")
    arc = np.linspace(0, th, 30)
    ax.plot(o[0] + 0.95 * np.sin(arc), o[1] + 0.95 * np.cos(arc), color=INK2, lw=0.8)
    ax.text(o[0] + 0.3, o[1] + 1.0, r"$\theta$", fontsize=9, color=INK)
    L = 0.6
    g = np.array([0, -L])
    gc = -math.cos(th) * L * z
    gn = math.sin(th) * L * n
    ax.annotate("", xy=o + g, xytext=o, arrowprops=dict(arrowstyle="-|>", color=C[7], lw=1.6))
    ax.text(*(o + g + np.array([0.0, -0.06])), r"$g$", color=INK, fontsize=9, ha="center", va="top")
    ax.annotate("", xy=o + gc, xytext=o, arrowprops=dict(arrowstyle="-|>", color=C[1], lw=1.4))
    ax.text(*(o + gc + np.array([-0.3, -0.1])), r"$g\cos\theta$ (along axis)", color=INK, fontsize=8, ha="right",
            va="top")
    ax.annotate("", xy=o + gn, xytext=o, arrowprops=dict(arrowstyle="-|>", color=C[2], lw=1.4))
    ax.text(*(o + gn + np.array([0.05, -0.02])), r"$g\sin\theta$ (load on lower finger)", color=INK, fontsize=8,
            ha="left", va="top")
    ax.set_xlim(-1.1, 2.0)
    ax.set_ylim(-1.0, 1.55)
    save(fig, FIGS / "geometry.pdf")


# --------------------------------------------------------------------------- #
# Pulse state machine and the windows the reward reads                        #
# --------------------------------------------------------------------------- #
def fig_timing():
    D, L = K["PULSE_DELAY_STEPS"], K["PULSE_LENGTH"]
    lock = int(round(K["PULSE_LOCKOUT_DURATION"] / T))
    settle = int(round(K["BLOCKADE_SETTLE_DURATION"] / T))
    prep = int(round(K["BLOCKADE_PREP_DURATION"] / T))
    refund, free = K["SMOOTHNESS_REFUND_STEPS"], K["SMOOTHNESS_FREE_STEPS_AFTER_PULSE"] + 1
    rows = [
        ("gripper", [(0, D, C[3], ""), (D, L - 1, C[1], ""),
                     (D + L - 1, 1, INK2, ""), (D + L, lock - D - L, "#d9d8d3", "clamped closed")]),
        ("new pulse", [(0, lock, "#d9d8d3", f"lockout {lock} steps = {K['PULSE_LOCKOUT_DURATION']} s")]),
        ("blockade cost", [(settle + 1, lock - prep - settle - 1, C[6], "motion penalised while waiting")]),
        ("smoothness", [(-refund, refund, C[2], "refunded"), (0, free, C[0], "free")]),
    ]
    fig, ax = plt.subplots(figsize=(TEXTWIDTH, 1.9))
    for r, (name, segs) in enumerate(rows):
        y = len(rows) - 1 - r
        ax.text(-10.5, y, name, ha="right", va="center", fontsize=8, color=INK)
        for x0, w, col, lab in segs:
            ax.add_patch(Rectangle((x0, y - 0.32), w, 0.64, facecolor=col, edgecolor="white", lw=1.5))
            if lab:
                txt_col = "white" if col in (C[1], C[6], C[0], INK2) else INK
                ax.text(x0 + w / 2, y, lab, ha="center", va="center", fontsize=6.8, color=txt_col)
    ax.axvline(0, color=C[7], lw=1.3)
    y = len(rows) - 1
    ax.annotate(f"delay {D} steps (policy, open)", (D / 2, y + 0.32), xytext=(-6, 0.5 + y), textcoords="data",
                fontsize=6.8, color=INK, ha="right", va="center", arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7))
    ax.text(D + (L - 1) / 2, y + 0.45, f"forced open {(L - 1) * 20} ms", fontsize=6.8, color=INK, ha="center",
            va="bottom")
    ax.annotate("close", (D + L - 0.5, y + 0.32), xytext=(D + L + 6, y + 0.55), fontsize=6.8, color=INK,
                va="center", arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7))
    ax.text(0.5, -0.55, "trigger (rising edge of the finger action)", color=INK, fontsize=7, ha="left", va="center")
    ax.set_xlim(-10, lock + 3)
    ax.set_ylim(-0.8, len(rows) - 0.1 + 0.3)
    ax.set_yticks([])
    ax.set_xlabel("policy steps after the trigger (20 ms each)")
    ax.spines["left"].set_visible(False)
    ax.grid(axis="y", visible=False)
    save(fig, FIGS / "pulse_timing.pdf")


# --------------------------------------------------------------------------- #
# Low-level reference: Hermite + 2nd-order filter, and what Genesis does      #
# --------------------------------------------------------------------------- #
def fig_control():
    d = np.load(DATA / "pulse_hires.npz")
    cmd = d["t45_cmd"]
    hv = d["t45_hand_v"][list(d["frictions"]).index(0.75)]
    dt = 1e-3
    n = len(cmd) * 20
    t = np.arange(n) * dt
    # Hermite velocity with trapezoid-consistent knots = linear interpolation of the knots
    knots = np.concatenate([[0.0], cmd])
    ref = np.interp(t, np.arange(len(knots)) * T, knots)
    filt = second_order_lowpass(ref, dt, 75.0, 0.33)
    sel = (t > 0.25) & (t < 0.62)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(TEXTWIDTH, 2.4), gridspec_kw={"width_ratios": [1, 1.6]})
    w = np.logspace(0, 3.3, 400)
    H = 75.0**2 / ((1j * w) ** 2 + 2 * 0.33 * 75.0 * 1j * w + 75.0**2)
    a1.semilogx(w / (2 * np.pi), 20 * np.log10(np.abs(H)), color=C[0])
    a1.axhline(0, color=INK2, lw=0.6)
    a1.axvline(75 / (2 * np.pi), color=MUTED, lw=0.8, ls=":")
    a1.text(75 / (2 * np.pi) * 1.1, -25, r"$\omega_n$ = 75 rad/s", fontsize=7, color=INK2)
    a1.set_xlabel("frequency [Hz]")
    a1.set_ylabel("|H| [dB]")
    a1.set_title(r"(a) $H(s)=\omega_n^2/(s^2+2\zeta\omega_n s+\omega_n^2)$")
    ts = (t[sel] - t[sel][0]) * 1e3
    a2.step(np.arange(len(cmd)) * T * 1e3 - t[sel][0] * 1e3 + 20, cmd, where="pre", color=INK2, lw=1.0,
            label="policy command (50 Hz)")
    a2.plot(ts, ref[sel], color=C[0], lw=1.4, ls="--", label="Hermite reference")
    a2.plot(ts, filt[sel], color=C[2], lw=1.4, label="after $H(s)$")
    a2.plot(ts, hv[: n][sel], color=C[1], lw=2.0, label="hand, Genesis (45°)")
    a2.set_xlim(0, ts[-1])
    a2.set_xlabel("time [ms]")
    a2.set_ylabel("velocity along the axis [m/s]")
    a2.set_title("(b) one throw: command to hand")
    a2.legend(loc="lower left", fontsize=6.8)
    fig.tight_layout()
    save(fig, FIGS / "control_chain.pdf")


# --------------------------------------------------------------------------- #
# Reward terms                                                                #
# --------------------------------------------------------------------------- #
def proximity(d):
    k, R, M = K["PROXIMITY_REWARD_SHARPNESS"], K["PROXIMITY_REWARD_RANGE"], K["PROXIMITY_REWARD_MAX"]
    return np.clip(M * (np.exp(-k * d / R) - math.exp(-k)) / (1 - math.exp(-k)), 0, M)


def regrasp_raw(imp):
    raw = (np.maximum(imp, -0.05) * 7500.0) * (np.abs(imp) / 0.0075) ** 3
    raw = np.minimum(raw, K["REGRASP_BONUS_CAP"])
    return np.where(raw < 0, raw * 5.0, raw)


def fig_rewards():
    fig, axes = plt.subplots(2, 2, figsize=(TEXTWIDTH, 4.2))
    a = axes[0, 0]
    d = np.linspace(0, 0.06, 300)
    a.plot(d * 1e3, proximity(d), color=C[0])
    a.axvspan(0, 5, color=SHADE, lw=0)
    a.text(0.5, 3, "success band", fontsize=7, color=INK2)
    a.set_xlabel(r"gap $d=|r_z-r_z^*|$ [mm]")
    a.set_ylabel("reward per step")
    a.set_title("(a) proximity")
    a = axes[0, 1]
    imp = np.linspace(0.0005, 0.035, 300)
    w = K["REWARD_TERM_WEIGHTS"]["regrasp_bonus"]
    a.semilogy(imp * 1e3, w * regrasp_raw(imp), color=C[1], label="improvement")
    a.semilogy(imp * 1e3, -w * regrasp_raw(-imp), color=C[6], ls="--", label="worsening (magnitude)")
    a.axhline(K["SUCCESS_REWARD"], color=INK2, lw=0.8, ls=":")
    a.text(0.5, K["SUCCESS_REWARD"] * 1.25, "success reward", fontsize=7, color=INK2)
    a.set_xlabel(r"change of in-hand error per regrasp [mm]")
    a.set_ylabel("weighted bonus")
    a.set_title(r"(b) regrasp bonus, $\propto\delta^4$")
    a.legend(loc="lower right")
    a = axes[1, 0]
    acc = np.linspace(-15, 15, 300)
    a.plot(acc, -K["Z_ACC_PENALTY_WEIGHT"] * (acc / K["Z_ACC_MAX"]) ** 2, color=C[2], label="acceleration")
    a.plot(acc, -K["JERK_PENALTY_WEIGHT"] * (acc / (2 * K["Z_ACC_MAX"])) ** 2, color=C[3],
           label=r"jerk, per $\Delta a$ on the axis")
    a.set_xlabel(r"$a$ or $\Delta a$ per step [m/s$^2$]")
    a.set_ylabel("reward per step")
    a.set_title("(c) smoothness (before refunds)")
    a.legend(loc="lower center")
    a = axes[1, 1]
    v = np.linspace(0, 0.45, 300)
    ex = np.maximum(v - K["BLOCKADE_VEL_TOLERANCE"], 0)
    a.plot(v, -K["SPEED_PENALTY_WEIGHT"] * (ex / K["Z_VEL_MAX"]) ** 2, color=C[0], label="speed, always")
    a.plot(v, -(ex / K["BLOCKADE_VEL_SCALE"]) ** 2, color=C[6], label="blockade, measured speed")
    a.set_ylim(-20, 0.5)
    a.set_xlabel("speed along the axis [m/s]")
    a.set_ylabel("reward per step")
    a.set_title("(d) speed and blockade")
    a.legend(loc="lower left")
    fig.tight_layout()
    save(fig, FIGS / "reward_terms.pdf")
    for x in (0.0075, 0.010, 0.015, 0.020):
        NUM[f"bonus_{int(x*1e4)}"] = float(w * regrasp_raw(np.array(x)))
    for x in (0.0, 0.005, 0.010, 0.020):
        NUM[f"prox_{int(x*1e3)}"] = float(proximity(np.array(x)))


def discounted(stream, gamma=GAMMA):
    return float(np.sum(stream * gamma ** np.arange(len(stream))))


def fig_returns():
    """Discounted value of finishing after k pulses vs loitering just outside the band."""
    alive, succ, tpen = K["ALIVE_PENALTY"], K["SUCCESS_REWARD"], K["SUCCESS_TIME_PENALTY"]
    fail = K["TIMEOUT_PENALTY"] + alive * MAX_LEN + K["FAIL_PENALTY_MARGIN"]
    NUM["fail_penalty"] = fail
    ts = np.arange(10, MAX_LEN + 1)
    fig, a = plt.subplots(figsize=(TEXTWIDTH * 0.62, 2.5))
    v_succ = [discounted(np.r_[np.full(t - 1, -alive), succ - tpen * t]) for t in ts]
    a.plot(ts * T, v_succ, color=C[0], label="succeed at time t")
    for dmm, col in ((6, C[1]), (12, C[2])):
        stream = np.r_[np.full(MAX_LEN - 1, -alive + float(proximity(np.array(dmm / 1e3)))), -K["TIMEOUT_PENALTY"]]
        val = discounted(stream)
        a.axhline(val, color=col, lw=1.4, ls="--")
        a.text(8.9, val + 120, f"loiter at {dmm} mm to timeout", fontsize=7, color=INK2, ha="right")
        NUM[f"loiter_{dmm}"] = val
    start, lock = K["PULSE_START_MIN_STEPS"], int(round(K["PULSE_LOCKOUT_DURATION"] / T))
    for k in range(1, 6):
        tk = start + lock * (k - 1) + 12
        vk = discounted(np.r_[np.full(tk - 1, -alive), succ - tpen * tk])
        a.plot(tk * T, vk, "o", color=C[0], ms=5, mec="white", mew=1)
        off, ha = ((5, 3), "left") if k <= 3 else ((-6, -9), "right")
        a.annotate(f"{k} pulse{'s' if k > 1 else ''}", (tk * T, vk), xytext=off, textcoords="offset points",
                   fontsize=7, color=INK2, ha=ha)
        NUM[f"value_{k}_pulses"] = vk
    a.set_xlabel("time of success [s]")
    a.set_ylabel(r"discounted return, $\gamma=0.99$")
    a.set_xlim(0, 9.1)
    a.legend(loc="upper right")
    save(fig, FIGS / "returns.pdf")


# --------------------------------------------------------------------------- #
# Training curves                                                             #
# --------------------------------------------------------------------------- #
RUNS = [
    ("tilt45-gru", C[0]),
    ("tilt45-gru-jerk", C[1]),
    ("tilt45-gru-speed", C[2]),
    ("tilt45-gru-speed-only-up", C[3]),
]


def smooth(y, k=15):
    if len(y) < k:
        return y
    pad = np.r_[np.full(k // 2, y[0]), y, np.full(k - 1 - k // 2, y[-1])]
    return np.convolve(pad, np.ones(k) / k, mode="valid")


def fig_learning():
    tb = np.load(DATA / "tb_curves.npz")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(TEXTWIDTH, 2.5))
    for run, col in RUNS:
        s = tb[f"{run}|Episodes/success_rate"]
        y = s[1] * (100 if s[1].max() <= 1.0 else 1)
        a1.plot(s[0], smooth(y), color=col, label=run)
        L = tb[f"{run}|Episodes/length_mean"]
        a2.plot(L[0], smooth(L[1]) * T, color=col, label=run)
        NUM[f"succ_{run}"] = float(np.mean(y[-50:]))
        NUM[f"succ_best_{run}"] = float(smooth(y, 20).max())
        NUM[f"iters_{run}"] = int(s[0][-1])
        NUM[f"len_{run}"] = float(np.mean(L[1][-50:]) * T)
    a1.set_xlabel("PPO iteration")
    a1.set_ylabel("episodes ending in success [%]")
    a1.set_ylim(0, 100)
    a1.set_title("(a) success rate (15-iteration mean)")
    a2.set_xlabel("PPO iteration")
    a2.set_ylabel("mean episode length [s]")
    a2.set_title("(b) episode length")
    a1.legend(loc="lower right", fontsize=6.8)
    fig.tight_layout()
    save(fig, FIGS / "learning.pdf")

    run = "tilt45-gru-speed"
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(TEXTWIDTH, 2.4))
    for (tag, name), col in zip((("success_rate", "success"), ("fail_rate", "failure"), ("timeout_rate", "timeout")),
                                (C[0], C[1], C[2])):
        s = tb[f"{run}|Episodes/{tag}"]
        y = s[1] * (100 if s[1].max() <= 1.0 else 1)
        a1.plot(s[0], smooth(y), color=col, label=name)
        NUM[f"outcome_{tag}"] = float(np.mean(y[-50:]))
    a1.set_ylim(0, 100)
    a1.set_xlabel("PPO iteration")
    a1.set_ylabel("share of all episodes [%]")
    a1.set_title(f"(a) outcomes, {run}")
    a1.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=3, fontsize=6.8)
    causes = [("fail_regrasp_limit", "7th regrasp"), ("fail_ee_low", "hand below limit"),
              ("fail_rel_z", "bar out of hand"), ("fail_rel_x", "bar slid sideways")]
    for (tag, name), col in zip(causes, (C[0], C[1], C[2], C[3])):
        key = f"{run}|Episodes/{tag}"
        if key not in tb.files:
            continue
        s = tb[key]
        a2.plot(s[0], smooth(s[1]), color=col, label=name)
        NUM[f"cause_{tag}"] = float(np.mean(s[1][-50:]))
    a2.set_ylim(0, 100)
    a2.set_xlabel("PPO iteration")
    a2.set_ylabel("share of failed episodes [%]")
    a2.set_title("(b) why the failures fail")
    a2.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=3, fontsize=6.8)
    fig.tight_layout()
    save(fig, FIGS / "failures.pdf")


# --------------------------------------------------------------------------- #
# One policy episode (the tilt-45 trajectory shipped to the real cell)        #
# --------------------------------------------------------------------------- #
def fig_episode():
    path = ROOT / "franka_controllers" / "traj_vis" / "tilt45" / "traj_d2_l5.csv"
    rows = list(csv.DictReader(open(path)))
    col = {k: np.array([float(r[k]) for r in rows]) for k in rows[0]}
    t = (col["step"] + 1) * T
    open_ = col["grip_cmd_l"] > 0.005
    fig, axes = plt.subplots(3, 1, figsize=(TEXTWIDTH, 4.3), sharex=True)

    def shade(ax):
        idx = np.where(open_)[0]
        if len(idx):
            groups = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
            for g in groups:
                ax.axvspan(t[g[0]] - T, t[g[-1]], color="#fde7c8", lw=0, zorder=0)

    a = axes[0]
    shade(a)
    a.step(t, col["target_z_vel"], where="pre", color=INK2, lw=1.1, label="policy command")
    a.plot(t, col["ee_vel_z"], color=C[1], lw=1.8, label="hand (measured)")
    a.set_ylabel("axial velocity [m/s]")
    a.legend(loc="lower left", fontsize=6.8)
    a = axes[1]
    shade(a)
    a.plot(t, (col["ee_z"] - K["HOME_TASK_Z"]) * 1e3, color=C[0], lw=1.8)
    a.set_ylabel("hand along axis [mm]")
    a = axes[2]
    shade(a)
    a.plot(t, col["cuboid_rel_z"] * 1e3, color=C[2], lw=1.8, label="bar in hand $r_z$")
    a.axhline(col["desired_rel_z"][0] * 1e3, color=INK, lw=1.0, ls="--", label="target $r_z^*$")
    a.axhspan((col["desired_rel_z"][0] - 0.005) * 1e3, (col["desired_rel_z"][0] + 0.005) * 1e3, color=SHADE, lw=0)
    a.set_ylabel("in-hand offset [mm]")
    a.set_xlabel("time [s]  (shaded: gripper open)")
    a.legend(loc="upper left", fontsize=6.8)
    fig.align_ylabels(axes)
    fig.tight_layout()
    save(fig, FIGS / "episode.pdf")
    i = np.where(open_)[0]
    NUM["ep_open_steps"] = int(len(i))
    NUM["ep_slip"] = float((col["cuboid_rel_z"][i[-1] + 3] - col["cuboid_rel_z"][i[0] - 1]) * 1e3)
    NUM["ep_final"] = float(col["cuboid_rel_z"][-1] * 1e3)
    NUM["ep_len"] = float(t[-1])
    NUM["ep_vmax"] = float(col["target_z_vel"].max())
    NUM["ep_amin"] = float(col["target_z_acc"].min())


# --------------------------------------------------------------------------- #
# Genesis pulse sweep                                                         #
# --------------------------------------------------------------------------- #
def fig_sweep():
    d = np.load(DATA / "pulse_slip.npz")
    v, L, o = d["vpeak"], d["length"], d["offset"]
    tilts = [int(x) for x in d["tilts"]]
    vs, Ls = np.unique(v), np.unique(L)
    fig, axes = plt.subplots(2, 2, figsize=(TEXTWIDTH, 3.9), sharey=True, sharex=True)
    axes = axes.ravel()
    lim = 30
    for ax, tilt in zip(axes, tilts):
        k = f"t{tilt}"
        rel, trig, ee = d[k + "_rel_z"], d[k + "_trigger"], d[k + "_ee_z"]
        idx = np.arange(len(v))
        slip = (rel[idx, np.minimum(trig + L + 3, rel.shape[1] - 1)] - rel[idx, trig - 1]) * 1e3
        reset = (np.abs(np.diff(ee, axis=1)) > 0.03).any(axis=1)
        M = np.full((len(vs), len(Ls)), np.nan)
        for i, vp in enumerate(vs):
            for j, l in enumerate(Ls):
                m = (v == vp) & (L == l) & (o == 0)
                if not reset[m][0]:
                    M[i, j] = slip[m][0]
        im = ax.imshow(M, origin="lower", cmap=DIVERGING, vmin=-lim, vmax=lim, aspect="auto",
                       extent=[(Ls[0] - 1.5) * 20, (Ls[-1] - 0.5) * 20, -0.5, len(vs) - 0.5])
        for i in range(len(vs)):
            for j, l in enumerate(Ls):
                if not np.isnan(M[i, j]):
                    ax.text((l - 1) * 20, i, f"{M[i, j]:.0f}", ha="center", va="center", fontsize=7,
                            color="white" if abs(M[i, j]) > 18 else INK)
        ax.set_title(rf"$\theta={tilt}^\circ$")
        ax.grid(False)
        ax.set_yticks(range(len(vs)))
        ax.set_yticklabels([f"{x:.2f}" for x in vs])
        NUM[f"sweep_t{tilt}_best045"] = float(np.nanmax(M[-1]))
    for ax in axes[2:]:
        ax.set_xlabel("forced-open window [ms]")
        ax.set_xticks([(l - 1) * 20 for l in Ls])
    for ax in axes[::2]:
        ax.set_ylabel("peak speed [m/s]")
    fig.colorbar(im, ax=axes, label="slip per pulse [mm]", shrink=0.9, pad=0.02)
    save(fig, FIGS / "sweep.pdf")


def fig_physics():
    d = np.load(DATA / "pulse_hires.npz")
    i = list(d["frictions"]).index(0.75)
    k = "t45"
    hv, ov, rel = d[k + "_hand_v"][i], d[k + "_obj_v"][i], d[k + "_rel_z"][i]
    r0 = int(d[k + "_rev_substep"])
    t = np.arange(len(hv)) * 1e-3
    sel = slice(r0 - 30, r0 + 170)
    ms = (t[sel] - t[r0]) * 1e3
    F = (d[k + "_f_l"][i] + d[k + "_f_r"][i]) / 2
    j = r0 + 100
    while F[j] < 1.0:
        j += 1
    tr = t[r0] + 0.011
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(TEXTWIDTH, 2.4))
    a1.axvspan(0, 120, color="#fde7c8", lw=0)
    a1.plot(ms, hv[sel], color=INK, lw=1.8, label="hand")
    a1.plot(ms, ov[sel], color=C[1], lw=1.8, label="bar")
    a1.axhline(0, color=INK2, lw=0.6)
    a1.set_xlabel("time after commanded reversal [ms]")
    a1.set_ylabel("axial velocity [m/s]")
    a1.set_title(r"(a) Genesis, $\theta=45^\circ$, $\mu=0.75$")
    a1.legend(loc="upper right")
    a2.axvspan(0, 120, color="#fde7c8", lw=0)
    a2.plot(ms, (rel[sel] - rel[r0 - 5]) * 1e3, color=C[1], lw=2.2, label="Genesis")
    for mu, col, ls, lab in ((0.75, C[0], "-", r"model, $\mu=0.75$ on lower finger"),
                             (0.0, C[0], "--", r"model, free fall $g\cos\theta$")):
        tw, w, dd = simulate_slip(t, hv, tr, t[j], 45.0, mu=mu)
        a2.plot((tw - t[r0]) * 1e3, dd * 1e3, color=col, ls=ls, lw=1.4, label=lab)
    a2.axhline(0, color=INK2, lw=0.6)
    a2.set_xlabel("time after commanded reversal [ms]")
    a2.set_ylabel("slip [mm]")
    a2.set_title("(b) slip: Genesis vs 1-D model")
    a2.legend(loc="upper left", fontsize=6.8)
    fig.tight_layout()
    save(fig, FIGS / "physics.pdf")


if __name__ == "__main__":
    fig_geometry()
    fig_timing()
    fig_control()
    fig_rewards()
    fig_returns()
    fig_learning()
    fig_episode()
    fig_sweep()
    fig_physics()
    (HERE / "numbers.json").write_text(json.dumps(NUM, indent=1, default=float))
    print(json.dumps(NUM, indent=1, default=float))

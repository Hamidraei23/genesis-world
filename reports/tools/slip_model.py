"""Analytical and 1-D numerical model of in-hand slip during a gripper pulse.

Coordinates: s is position along the fixed grasp axis, positive up the axis.
The hand follows v_h(t); the bar is carried until the effective release t_r,
then moves under the axial gravity component g_a = g cos(theta) and, when the
bar rests on the lower finger, Coulomb friction mu * g_n with g_n = g sin(theta).
Relative quantities are object minus hand: w = v_o - v_h, Delta = s_o - s_h.
"""

import numpy as np

G = 9.81


def g_components(tilt_deg):
    th = np.deg2rad(tilt_deg)
    return G * np.cos(th), G * np.sin(th)


# --------------------------------------------------------------------------- #
# Closed forms: release at the start of a constant-deceleration reversal      #
# --------------------------------------------------------------------------- #
def slip_closed(V, A, Tp, g_a):
    """Slip after Tp for a swing V = v0 + v1 at deceleration A (valid for V/A <= Tp)."""
    return V * Tp - V**2 / (2.0 * A) - 0.5 * g_a * Tp**2


def slip_closed_any(V, A, Tp, g_a):
    """Same, but also valid while the hand is still reversing at Tp (V/A > Tp)."""
    Tr = V / A
    inside = 0.5 * (A - g_a) * Tp**2
    return np.where(Tr >= Tp, inside, slip_closed(V, A, Tp, g_a))


def w_end(V, A, Tp, g_a):
    Tr = V / A
    return np.where(Tr >= Tp, (A - g_a) * Tp, V - g_a * Tp)


def v_star(Tp, g_a):
    """Symmetric peak speed (v0 = v1) giving zero relative velocity at re-grip."""
    return 0.5 * g_a * Tp


def slip_star(Tp, A, g_a):
    """Slip at the matched swing V* = g_a Tp: the apex of the relative motion."""
    return 0.5 * g_a * Tp**2 * (1.0 - g_a / A)


def tp_for_slip(delta, A, g_a):
    """Pulse length whose matched (zero-w) slip equals delta."""
    return np.sqrt(2.0 * delta / (g_a * (1.0 - g_a / A)))


def timing_sensitivity(V, A, Tp, g_a, a_before):
    """dDelta/dt_r just before and just after the reversal start [m per s of release delay]."""
    we = w_end(V, A, Tp, g_a)
    return we + Tp * (a_before + g_a), we - Tp * (A - g_a)


# --------------------------------------------------------------------------- #
# Numerical: any hand profile, Coulomb friction on the lower finger           #
# --------------------------------------------------------------------------- #
def simulate_slip(t, v_h, t_release, t_grip, tilt_deg, mu=0.0, resting=True):
    """Integrate the bar's motion relative to the hand between release and re-grip.

    t, v_h      hand velocity along the axis, uniformly sampled
    resting     the bar lies on the lower finger (transverse gravity along the
                finger axis); friction acts only then
    Returns (t, w, delta) over [t_release, t_grip].
    """
    g_a, g_n = g_components(tilt_deg)
    f = mu * g_n if resting else 0.0
    dt = t[1] - t[0]
    a_h = np.gradient(v_h, dt)
    i0 = int(np.searchsorted(t, t_release))
    i1 = int(np.searchsorted(t, t_grip))
    w, d = 0.0, 0.0
    ws, ds = [w], [d]
    for i in range(i0, i1):
        drive = -g_a - a_h[i]  # relative acceleration with no friction
        if abs(w) < 1e-6 and abs(drive) <= f:
            w = 0.0  # static friction holds the bar on the finger
        else:
            direction = np.sign(w) if abs(w) >= 1e-6 else np.sign(drive)
            w_new = w + (drive - f * direction) * dt
            # kinetic friction cannot reverse the sliding direction by itself
            if abs(w) >= 1e-6 and np.sign(w_new) != np.sign(w) and abs(drive) <= f:
                w_new = 0.0
            w = w_new
        d += w * dt
        ws.append(w)
        ds.append(d)
    return t[i0 : i1 + 1], np.array(ws), np.array(ds)


# --------------------------------------------------------------------------- #
# Hand profiles                                                               #
# --------------------------------------------------------------------------- #
def trapezoid(v0, v1, a_up, A, t_hold, dt=1e-4, pre=0.0, t_after=0.3):
    """0 -> v0 at a_up, v0 -> -v1 at A, hold -v1. Returns (t, v, t_reversal_start)."""
    t_up = v0 / a_up
    t_rev = (v0 + v1) / A
    t_end = pre + t_up + t_rev + t_hold + t_after
    t = np.arange(0.0, t_end, dt)
    v = np.zeros_like(t)
    s = t - pre
    up = (s >= 0) & (s < t_up)
    v[up] = a_up * s[up]
    rev = (s >= t_up) & (s < t_up + t_rev)
    v[rev] = v0 - A * (s[rev] - t_up)
    v[s >= t_up + t_rev] = -v1
    return t, v, pre + t_up


def controller_chain(t, v_profile, rate=50.0, arm_wn=75.0, arm_zeta=0.33, max_accel=19.0,
                     lp_wn=400.0, lp_zeta=0.6125):
    """Commanded profile -> what the FR3 hand does, stage by stage.

    publish   50 Hz samples of the profile, held (what goes on the topic)
    interp    `cubic`, interp_lag 1.0: the segment ending at the newest command,
              i.e. the knots joined by straight lines one command period late
    lowpass   the controller's 2nd-order reference filter (400 rad/s, zeta 0.6125)
    slew      max_accel on the final reference
    arm       2nd-order stand-in for the arm's own tracking (the Genesis env's filter,
              tuned against the real arm)
    """
    dt = t[1] - t[0]
    T = 1.0 / rate
    k = np.floor(t / T).astype(int)
    knots_t = np.arange(k.max() + 2) * T
    knots_v = np.interp(knots_t, t, v_profile)
    published = knots_v[k]
    ref = np.interp(t - T, knots_t, knots_v, left=0.0)
    lp = second_order_lowpass(ref, dt, lp_wn, lp_zeta)
    slew = np.empty_like(lp)
    prev = 0.0
    for i, x in enumerate(lp):
        prev = prev + np.clip(x - prev, -max_accel * dt, max_accel * dt)
        slew[i] = prev
    arm = second_order_lowpass(slew, dt, arm_wn, arm_zeta)
    return {"published": published, "reference": ref, "lowpass": slew, "arm": arm}


def second_order_lowpass(u, dt, wn, zeta):
    """Tustin-discretised wn^2 / (s^2 + 2 zeta wn s + wn^2), as in the env and controller."""
    k = 2.0 / dt
    wn2 = wn * wn
    blin = 2.0 * zeta * wn
    a0 = k * k + blin * k + wn2
    b0, b1, b2 = wn2 / a0, 2 * wn2 / a0, wn2 / a0
    a1, a2 = (-2 * k * k + 2 * wn2) / a0, (k * k - blin * k + wn2) / a0
    y = np.zeros_like(u)
    u1 = u2 = y1 = y2 = 0.0
    for n, un in enumerate(u):
        yn = b0 * un + b1 * u1 + b2 * u2 - a1 * y1 - a2 * y2
        y[n] = yn
        u2, u1, y2, y1 = u1, un, y1, yn
    return y

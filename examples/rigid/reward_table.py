"""Per-iteration reward-term table for PPO training runs.

Accumulates ``env.last_reward_terms`` over each rollout and prints a table
after rsl_rl's own iteration block, so the contribution of every reward term
(z_improvement, regrasp_bonus, jerk_penalty, ...) is visible while training.

Usage:
    from reward_table import RewardTermTracker
    tracker = RewardTermTracker(env)
    tracker.attach(runner)
"""

from __future__ import annotations

import torch


# Keys that are diagnostics rather than additive reward components; they are
# shown in a second table without a weight or contribution share.
_DIAGNOSTIC_HINTS = (
    "z_improvement", "avg_force", "regrasp_event", "success", "fail",
    "timeout", "steps", "count", "rel_z", "force",
)

TOTAL_KEY = "__reward__"

# Keys the env publishes to describe how an episode ended (ep_done, ep_length,
# ep_fail_<reason>, ...). They are only meaningful on the terminal step, so they are
# kept out of the per-step tables and summarised per episode instead.
EPISODE_PREFIX = "ep_"
FAIL_REASON_PREFIX = "ep_fail_"
OUTCOME_KEYS = ("ep_success", "ep_fail", "ep_timeout")


def _is_reward_term(key: str) -> bool:
    if any(hint in key for hint in _DIAGNOSTIC_HINTS):
        return False
    return key.endswith(("_reward", "_bonus", "_penalty", "_cost")) or key in ("reward",)


def _fmt(x: float, width: int) -> str:
    a = abs(x)
    if a == 0.0:
        s = "0"
    elif a >= 1000.0:
        s = f"{x:.1f}"
    elif a >= 0.01:
        s = f"{x:.3f}"
    else:
        s = f"{x:.2e}"
    return f"{s:>{width}}"


class RewardTermTracker:
    """Collects reward-term statistics across a rollout and prints them."""

    def __init__(self, env, width: int = 80):
        self.env = env
        self.width = width
        self.weights = dict(getattr(env, "REWARD_TERM_WEIGHTS", {}) or {})
        self._steps: list[dict] = []
        self._samples = 0
        self._regrasp_events = None
        # Per-episode accounting. Running sums live per env and span iterations; the
        # finished-episode rows are cleared at every report.
        self._ep_keys: list[str] | None = None
        self._ep_running: torch.Tensor | None = None   # (N, K+1), last column = rew_buf
        self._ep_rows: list[dict[str, torch.Tensor]] = []

    # -- collection ---------------------------------------------------- #

    def record(self) -> None:
        """Snapshot the env's reward terms for the step that just finished."""
        terms = getattr(self.env, "last_reward_terms", None)
        if not terms:
            return
        self._record_episodes(terms)

        mask = terms.get("regrasp_event")
        if mask is not None:
            mask = (mask.detach().float().reshape(-1) > 0.5).float()

        step: dict[str, torch.Tensor] = {}
        n = 0
        for key, val in terms.items():
            if not torch.is_tensor(val) or key.startswith(EPISODE_PREFIX):
                continue
            v = val.detach().float().reshape(-1)
            n = v.numel()
            event_sum = (v * mask).sum() if mask is not None and mask.numel() == n else torch.zeros((), device=v.device)
            step[key] = torch.stack([v.sum(), v.amin(), v.amax(), (v != 0).float().sum(), event_sum])

        rew = getattr(self.env, "rew_buf", None)
        if torch.is_tensor(rew):
            v = rew.detach().float().reshape(-1)
            n = n or v.numel()
            step[TOTAL_KEY] = torch.stack(
                [v.sum(), v.amin(), v.amax(), (v != 0).float().sum(), torch.zeros((), device=v.device)]
            )

        if not step:
            return
        if mask is not None:
            self._regrasp_events = mask.sum() if self._regrasp_events is None else self._regrasp_events + mask.sum()
        self._samples += n
        self._steps.append(step)

    def _record_episodes(self, terms: dict) -> None:
        """Accumulate weighted reward terms per env and harvest envs that just ended."""
        done = terms.get("ep_done")
        rew = getattr(self.env, "rew_buf", None)
        if done is None or not torch.is_tensor(rew):
            return
        done = done.detach().reshape(-1) > 0.5
        if self._ep_keys is None:
            self._ep_keys = [
                k for k, v in terms.items()
                if torch.is_tensor(v) and not k.startswith(EPISODE_PREFIX)
                and (k in self.weights or _is_reward_term(k))
            ]
            self._ep_running = torch.zeros(done.numel(), len(self._ep_keys) + 1, device=done.device)

        cols = []
        for k in self._ep_keys:
            v = terms.get(k)
            cols.append(
                self.weights.get(k, 1.0) * v.detach().float().reshape(-1)
                if torch.is_tensor(v) else torch.zeros_like(done, dtype=torch.float32)
            )
        cols.append(rew.detach().float().reshape(-1))
        self._ep_running += torch.stack(cols, dim=-1)

        if not bool(done.any()):
            return
        idx = done.nonzero(as_tuple=False).squeeze(-1)
        row = {"sums": self._ep_running[idx].clone()}
        for k, v in terms.items():
            if k.startswith(EPISODE_PREFIX) and k != "ep_done" and torch.is_tensor(v):
                row[k] = v.detach().float().reshape(-1)[idx].clone()
        self._ep_running[idx] = 0.0
        self._ep_rows.append(row)

    def _episode_lines(self, it: int, writer) -> list[str]:
        """Outcome rates, length distribution, fail reasons and per-episode returns."""
        rows, self._ep_rows = self._ep_rows, []
        if not rows:
            return ["", " Episodes: none ended this iteration"]

        def cat(k: str) -> torch.Tensor:
            return torch.cat([r[k] for r in rows]).cpu() if k in rows[0] else torch.zeros(0, device="cpu")

        length = cat("ep_length")
        n = length.numel()
        pct = lambda mask: 100.0 * float(mask.float().sum()) / max(n, 1)
        succ, fail, tout = (cat(k) > 0.5 for k in OUTCOME_KEYS)
        n_fail = int(fail.sum())

        lines = ["", f" Episodes ended: {n}   success {pct(succ):5.1f}%   "
                     f"fail {pct(fail):5.1f}%   timeout {pct(tout):5.1f}%"]

        # Genesis makes CUDA the default device, so pin q to wherever length lives.
        q = torch.quantile(length, torch.tensor([0.1, 0.5, 0.9], device=length.device)).tolist()
        lines.append(f" length    mean {float(length.mean()):6.1f}   p10 {q[0]:5.0f}   p50 {q[1]:5.0f}"
                     f"   p90 {q[2]:5.0f}   max {float(length.max()):5.0f}")
        if bool(succ.any()):
            lines.append(f" success   mean length {float(length[succ].mean()):6.1f}")

        max_len = int(getattr(self.env, "max_episode_length", 450))
        edges = [0, 50, 100, 150, 300, max_len + 1]
        buckets = []
        for lo, hi in zip(edges, edges[1:]):
            label = f"{lo}-{hi - 1}" if hi <= max_len else f"{lo}-{max_len}"
            buckets.append(f"{label} {pct((length >= lo) & (length < hi)):4.1f}%")
        lines.append(" histogram " + "   ".join(buckets))

        reasons = sorted(k for k in rows[0] if k.startswith(FAIL_REASON_PREFIX))
        if n_fail > 0 and reasons:
            rates = {k[len(FAIL_REASON_PREFIX):]: 100.0 * float((cat(k)[fail] > 0.5).sum()) / n_fail
                     for k in reasons}
            ordered = sorted(rates.items(), key=lambda kv: -kv[1])
            lines.append(f" fail reason, % of {n_fail} failed episodes (several can fire together)")
            lines.append("   " + "   ".join(f"{name} {r:5.1f}%" for name, r in ordered))
        else:
            rates = {}

        sums = torch.cat([r["sums"] for r in rows]).cpu()
        keys = list(self._ep_keys or []) + [TOTAL_KEY]
        lines.append(f" {'per-episode return (weighted)':<32}{'mean':>11}{'min':>11}{'max':>11}")
        for j, key in enumerate(keys):
            col = sums[:, j]
            name = "TOTAL (rew_buf)" if key == TOTAL_KEY else key
            lines.append(f" {name:<32}{_fmt(float(col.mean()), 11)}{_fmt(float(col.min()), 11)}"
                         f"{_fmt(float(col.max()), 11)}")

        if writer is not None and hasattr(writer, "add_scalar"):
            writer.add_scalar("Episodes/count", n, it)
            writer.add_scalar("Episodes/success_rate", pct(succ), it)
            writer.add_scalar("Episodes/fail_rate", pct(fail), it)
            writer.add_scalar("Episodes/timeout_rate", pct(tout), it)
            writer.add_scalar("Episodes/length_mean", float(length.mean()), it)
            writer.add_scalar("Episodes/length_p50", q[1], it)
            for name, r in rates.items():
                writer.add_scalar(f"Episodes/fail_{name}", r, it)
            for j, key in enumerate(keys):
                name = "reward" if key == TOTAL_KEY else key
                writer.add_scalar(f"EpisodeReturn/{name}", float(sums[:, j].mean()), it)
        return lines

    # -- reporting ------------------------------------------------------ #

    def _aggregate(self) -> tuple[dict[str, dict[str, float]], float]:
        keys = list(self._steps[0].keys())
        stacked = {k: torch.stack([s[k] for s in self._steps if k in s]) for k in keys}
        events = float(self._regrasp_events) if self._regrasp_events is not None else 0.0
        out = {}
        for key, t in stacked.items():
            col = t.cpu()
            out[key] = {
                "mean": float(col[:, 0].sum()) / max(self._samples, 1),
                "min": float(col[:, 1].min()),
                "max": float(col[:, 2].max()),
                "active": float(col[:, 3].sum()) / max(self._samples, 1) * 100.0,
                "on_regrasp": float(col[:, 4].sum()) / events if events > 0 else 0.0,
            }
        return out, events

    def report(self, it: int, writer=None) -> None:
        """Print the table for this iteration and reset the accumulators."""
        if not self._steps:
            return
        stats, events = self._aggregate()
        self._steps = []
        n_samples, self._samples = self._samples, 0
        self._regrasp_events = None

        term_keys = [k for k in stats if k != TOTAL_KEY and (k in self.weights or _is_reward_term(k))]
        diag_keys = [k for k in stats if k != TOTAL_KEY and k not in term_keys]

        weighted = {k: self.weights.get(k, 1.0) * stats[k]["mean"] for k in term_keys}
        total_abs = sum(abs(v) for v in weighted.values()) or 1.0

        lines = ["-" * self.width]
        lines.append(f" Reward terms  |  iteration {it}  |  {n_samples} env-steps  |  {int(events)} regrasps")
        lines.append(
            f" {'term':<26}{'weight':>7}{'mean/step':>12}{'share':>8}{'min':>11}{'max':>11}{'active%':>9}"
        )
        for key in term_keys:
            s = stats[key]
            w = self.weights.get(key, 1.0)
            share = abs(weighted[key]) / total_abs * 100.0
            lines.append(
                f" {key:<26}{w:>7.2f}{_fmt(s['mean'], 12)}{share:>7.1f}%"
                f"{_fmt(s['min'], 11)}{_fmt(s['max'], 11)}{s['active']:>8.1f}%"
            )
        if TOTAL_KEY in stats:
            s = stats[TOTAL_KEY]
            lines.append(
                f" {'TOTAL (rew_buf)':<26}{'':>7}{_fmt(s['mean'], 12)}{'':>8}{_fmt(s['min'], 11)}{_fmt(s['max'], 11)}"
            )

        if diag_keys:
            lines.append("")
            lines.append(
                f" {'diagnostic':<26}{'mean':>12}{'min':>11}{'max':>11}{'on-regrasp':>13}"
            )
            for key in diag_keys:
                s = stats[key]
                lines.append(
                    f" {key:<26}{_fmt(s['mean'], 12)}{_fmt(s['min'], 11)}{_fmt(s['max'], 11)}{_fmt(s['on_regrasp'], 13)}"
                )
        lines.extend(self._episode_lines(it, writer))
        lines.append("-" * self.width)
        print("\n".join(lines), flush=True)

        if writer is not None and hasattr(writer, "add_scalar"):
            for key, s in stats.items():
                name = "reward" if key == TOTAL_KEY else key
                group = "RewardTerms" if key in term_keys or key == TOTAL_KEY else "Diagnostics"
                writer.add_scalar(f"{group}/{name}", s["mean"], it)
            if events > 0:
                for key in diag_keys:
                    writer.add_scalar(f"Diagnostics/{key}_on_regrasp", stats[key]["on_regrasp"], it)

    # -- wiring ---------------------------------------------------------- #

    def attach(self, runner) -> None:
        """Hook env.step (to collect) and logger.log (to print) in place."""
        env, logger = self.env, runner.logger
        env_step, logger_log = env.step, logger.log

        def step(actions):
            out = env_step(actions)
            self.record()
            return out

        def log(*args, **kwargs):
            logger_log(*args, **kwargs)
            self.report(kwargs.get("it", args[0] if args else -1), getattr(logger, "writer", None))

        env.step = step
        logger.log = log

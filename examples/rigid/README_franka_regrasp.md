# Franka Z regrasping, reward v3

Select `--env env_franka_regrasp` in the existing PPO trainer or policy player.
The original environment file is unchanged by this addition. The new class
inherits its scene, reset distribution, observation construction, action scaling,
gripper pulses and domain randomization.

## Interface and motion

- Same 14 observations, in the same order, with the same fixed normalization.
- Same 3 actions: Z velocity scaled to ±0.6 m/s and two finger positions.
- Same positive/negative relative-Z targets with `--mix`.
- Same MLP architecture and checkpoint tensor shapes.

This version bounds the **complete Cartesian velocity reference**, including
position correction, to 15 m/s² of acceleration, the measured limit itself. It
replaces the underdamped velocity filter and
evaluates the original constant-acceleration reference directly, avoiding
float32 cancellation in the cubic derivative. Consequently an old policy's
trajectory can change even though its input/output interface is compatible.

Measured acceleration is `norm((ee_velocity_after - ee_velocity_before) / dt)`
in world coordinates, including X/Y, sampled at every physics step. Two summaries
are taken per policy step: the **peak** over the 20 samples and their **RMS**.

## Acceleration limit enforcement

Reward v2 failed the episode whenever the peak crossed 15 m/s². A single 1 ms
sample crosses it on the gripper's closing contact, which the policy cannot
avoid, so the termination fired precisely when the policy performed the maneuver
the task requires. v3 keeps the limit but stops treating one impulse sample as a
violation of it:

Termination needs headroom above what the servo may command, or a correctly
executed pulse at the limit would terminate itself. The command bound is 15 and
the failure bound is 18, and everything between them is tracking error or
contact rather than a choice the policy made.

| Signal | Meaning | Consequence |
|---|---|---|
| Peak above 15 m/s² | Commanded, or a contact impulse | Squared-hinge cost above 15 |
| Peak above 18 m/s² on one step | Usually a contact impulse | Logged only, `acceleration_violation` |
| Peak above 18 m/s² for 5 consecutive policy steps | Sustained excursion | Episode failure |
| Step RMS above 18 m/s² | Servo runaway, impulse-insensitive | Episode failure |

The excess cost is weighted 30 because it is the standing bound rather than a
supplement to the termination. It is never annealed down. Success gating uses the
step RMS with a 4 m/s² threshold, so an impulse can no longer reset a hold that
is otherwise stable.

## Reward

Reward revision `regrasp-v3.7`. Restart or resume training to load these changes;
an already running Python process continues using the reward it imported at
startup.

Running costs are multiplied by the actual policy period `T`, 0.02 s by default,
and by the effort anneal scale `k`. Let `e = object_relative_z - desired_relative_z`
and let grasp quality be the two-scale potential

```
q = 0.5 * exp(-0.5 * (e / 0.025)^2) + 0.5 * exp(-0.5 * (e / 0.005)^2)
```

| Component | Contribution per policy step |
|---|---|
| Stable success / failure / timeout | `+100 * pose_quality` / -25 / 0; timeout remains a bootstrapped truncation |
| Success pose quality | `0.5 + 0.5 exp(-0.5 ((ee_z - 0.75) / 0.06)^2)`, evaluated when the hold completes |
| Time cost | `-1.5 T`, which is -13.5 over a full episode |
| Dense progress | `4 (0.99 q_next - q_previous)`; true terminal potential is zero |
| Goal dwell | `+4` for each new longest qualifying run reached this episode |
| Confirmed regrasp | `40 g^2` for `g = clip(error_removed_this_cycle / 0.02, -1, 1)`, doubled and negated when `g < 0`, first 4 cycles only |
| Attempt bonus | `+8` per confirmed regrasp, first 4 per episode, scaled by `1 - anneal` |
| Arm torque | `-5 k T mean_substeps,joints((tau / tau_limit)^2)` |
| Acceleration | `-T (k (0.3 (norm(a)/15)^2 + 3 (relu(norm(a)-12)/3)^2) + 30 (relu(norm(a)-15)/3)^2)`, mean over substeps |
| Against gravity | `-2 k T mean_substeps((relu(a_z)/15)^2)` |
| Command jerk | `-0.015 k T ((acc_cmd - previous_acc_cmd) / (150 T))^2` |
| Peak speed | `-1 k T (relu(max_substeps(norm(ee_vel)) - 0.2) / 0.4)^2` |
| Centering | `-0.5 k T sum((relative_xy / 0.02)^2)` |
| Post-regrasp settle | During 0.2 s after a regrasp within 10 mm: `-T (2 (ee_vz/0.1)^2 + ((ee_z-hold_z)/0.025)^2 + 0.5 raw_velocity_action^2)` |
| Home height | `-20 T (relu(abs(ee_z - 0.75) - 0.03) / 0.08)^2` |
| Grip bound | `-20 T (relu(abs(relative_z) - 0.06) / 0.09)^2` |
| Efficiency ceiling | every `k`-scaled cost above is scaled down together so their sum never exceeds `0.05 * 100 / 450` per step |
| Forced zero hold | `-T (2 (ee_vz/0.1)^2 + ((ee_z-hold_z)/0.025)^2)`, no command term |
| Optional velocity cost | With `--minimze_vel`: `-0.2 k T raw_velocity_action^2` |

### Why the terminal numbers are what they are

The v2 numbers made holding still the optimal policy. Freezing paid the timeout
plus the time cost, about -14.5, while any attempt risked -20, and the +80 for
success was unreachable by exploration because it required eight conditions to
hold simultaneously for 200 ms. Potential-based shaping cannot repair that, since
it is policy-invariant by construction. v3 changes the base reward instead, under
two constraints that the test suite asserts directly:

- **Failure must stay more expensive than surviving to the timeout**, otherwise
  the policy learns to drop the object on the first step. Failure is -20 against
  a worst case of -9.5 for running the episode out.
- **Four attempt bonuses must outweigh the cost of a full episode**, otherwise
  freezing dominates every attempt a fresh policy can make. Four attempts pay
  +12 against that same -9.5.

The tolerance ladder replaces the single unreachable +80 with graded credit that
exploration can actually find. Reaching 5 mm pays the sum of all four rungs, 45,
and the stable hold adds 40 on top, so a complete success is worth 85. Each rung
pays once per episode and is ratcheted, so backing off and returning earns
nothing. The ladder requires a completed regrasp, so it cannot be collected by
holding the object where it started.

The fine Gaussian at 5 mm exists because the coarse one is nearly flat inside the
success tolerance, which left the final approach with no gradient. Both are
normalized so `q` still runs from 0 to 1.

### What the first v3 run showed

v3.0 trained for 314 iterations and reached a stable local optimum with a 100%
failure rate, episodes lasting 56 steps, and 4.7 regrasps each. Reading the term
columns explains it exactly:

| Term per episode | Value | Reading |
|---|---|---|
| ladder | +18.3 | reaching 10 to 15 mm of error |
| attempt | +4.4 | about 3 of the 4 paid attempts, at the iteration-314 anneal |
| regrasp | +4.8 | quality records genuinely being set |
| base | -20.6 | failure on every episode, 20 plus 0.56 of time cost |
| progress | -9.1 | almost entirely the terminal potential correction on failure |
| hold | -13.9 | the settle window fighting the policy for 84% of the episode |

The policy had learned to spam pulses as fast as the gripper allows, one every 12
steps, collect the rungs and the attempt bonuses, and then crash. The harvest of
27.5 nearly covered the 29.1 cost of dying, and crashing ended the episode before
the time cost and the hold penalty could accumulate. The regrasp count settling
just above `ATTEMPT_BONUS_MAX_EVENTS` is the signature: it stopped once the
bonuses were exhausted.

v3.1 closes it with three changes plus the diagnostics that should have been
there from the start.

**The ladder is refunded on failure.** Rungs are a claim on finishing the
episode, not a payout to bank before crashing. A failing step pays back the sum
of every rung earned, so a failed episode's ladder total is exactly zero. Rungs
survive a timeout, so a policy that improves and then merely runs out of clock
keeps its credit.

**The settle window arms only within 10 mm of the target.** Armed after every
regrasp, it covered 84% of the episode and charged `2 (ee_vz/0.1)^2` while the
policy was building the velocity its next pulse needs. Since one pulse moves the
object only a few millimetres in the hard direction, a chain of pulses is the
intended strategy and the settle window was taxing it. Settling is now asked for
only where it matters, which is also where success is decided.

**A soft workspace band on EE height.** Nothing previously pulled the arm back
from the hard bounds at 0.6 and 0.96 m: the settle reference tracks whatever
height the last regrasp happened at, so the arm could march to a limit over a few
pulses and die there with no prior warning in the reward. The band costs
`-20 T (outside([0.70, 0.86]) / 0.05)^2`, matching the success height window, and
like the acceleration excess cost it is never annealed down. Check the `wspace`
column on the first few iterations of a run: it should be near zero, since the
arm starts inside the band. A large value from iteration 1 means the band is
misplaced relative to where this scene actually puts the hand.

**Per-cause failure reporting.** Every terminal condition is now published as
`fail_<cause>` and summarised on a line under the training table:

```
 fail causes: ee_low 71% | slip_out 19% | lateral 6% | ee_high 4%
```

Read that line first on the next run. It says whether the arm is leaving its
height window, the object is escaping the fingers, or the servo is running away,
and each points at a different fix.

### What the v3.2 run showed, and the rescale it forced

v3.2 trained 1000 iterations and ended with 0% success, 33% failure and 67%
timeout, at 441-step episodes and 2.06 regrasps each. Two things were wrong, and
neither was the one that looks obvious.

**The effort penalties were not suppressing motion.** Torque, acceleration,
gravity, jerk and speed together came to -1.66 per episode against a total of
-40.9, which is 4%. What suppressed the pulsing was a per-event cost of 1.5 that
annealed to full weight by iteration 500, at exactly the point where the attempt
bonus annealed to zero. From iteration 500 onward every release/regrasp cycle
cost 1.5 and paid nothing, and the regrasp column read -2.85 for 2.06 cycles,
pure cost. The policy correctly learned to stop opening the gripper. That cost is
removed: after the anneal an attempt is free, never punished.

**The scale had drifted far from the reward that trained.** Normalising both to a
success worth 100:

| As a percentage of one success | Original | v3.2 | v3.3 |
|---|---|---|---|
| Failure penalty | 8.3 | 23.5 | 25.0 |
| Time cost over a full episode | 18.8 | 5.3 | 13.5 |
| Regrasp bonuses available | 66.7 | 94.1 | 160.0 |
| Effort cost ceiling | 3.0 | 23.5 | 5.0 |

v3.2 charged four times too little for burning the clock, which is why two thirds
of its episodes simply timed out, and capped effort eight times higher than the
reward that worked. v3.3 restores those proportions and makes progress bonuses
the dominant positive signal, as they were originally.

### One achievement bonus, not three

v3.0 through v3.2 had three overlapping ways to reward getting closer: potential
shaping, a grasp-quality record bonus, and the tolerance ladder. The ladder paid
for *proximity*, which is farmable, and produced the harvest-then-crash exploit;
the refund that patched it then destroyed the learning signal, and the ladder
column read exactly 0.00 for the whole 1000-iteration run.

v3.3 keeps two, and the achievement half is the mechanism the original reward
used: pay for the error a single release/regrasp cycle actually removed, measured
against the position snapshotted when the fingers opened. Proximity the policy
did not produce pays nothing, so there is nothing to bank and no refund needed.

Two guards, both from the original:

- **Backsliding is charged at twice the curve.** Releasing high to regrasp low
  and repeating would otherwise farm the bonus. An oscillation now loses money.
- **Only the first four cycles are eligible,** matching the parent environment's
  bonus count.

### One decisive slip, not a chain of nudges

The original bonus was linear in the improvement, so five small cycles paid what
one large one paid. This one is quadratic, capped at the 20 mm reference.

| Strategy | Regrasp bonus |
|---|---|
| One cycle removing 20 mm | 40.0 |
| Five cycles removing 4 mm each | 6.4 |

Whole-episode discounted returns now rank a single decisive regrasp above a
three-cycle approach to the same target, which is what the quadratic is for.
Note the physics caveat below: in the direction where one pulse only moves the
object a few millimetres, this bonus is largely out of reach and the chain of
pulses remains the only route. It is a preference, not a requirement.

### Bounding the efficiency costs

Torque, jerk, speed, posture and gravity costs all push toward standing still,
and they apply on every step of an episode while the task reward arrives once.
Rather than hand-tuning each weight against that risk, they share a ceiling:

```
per-step ceiling = EFFORT_BUDGET_FRACTION * SUCCESS_BONUS / max_episode_length
                 = 0.5 * 40 / 450 = 0.0444
```

When their sum would exceed it, every term is scaled by the same factor, so each
keeps its share of the blame and the reward still equals the sum of the logged
terms. The guarantee that falls out:

| Quantity | Value |
|---|---|
| Worst-case efficiency cost, full 450-step episode | -20.0 |
| Success bonus plus a full ladder | +85.0 |
| Typical cost on a successful episode | about -4 |

The acceleration excess cost, the home-height pull and the grip bound sit outside
the ceiling. They are bounds on where the arm and the object may go, not
preferences about how they get there, so they must keep biting even when
everything else saturates.

The grip bound is new in v3.3 and answers the only failure cause left standing in
the v3.2 run, `slip_out` at 42%, meaning the object slid out of the fingers
entirely. The hard cutoff at 0.15 m gave no warning on the way there. The bound
is free out to 0.06 m, well beyond the largest target, and steep after that.

Torque dropped from 8 to 5 and jerk from 0.05 to 0.015. Jerk was the largest
single efficiency cost in the v3.0 run, at roughly -12 over a full episode at
full anneal, charged for exactly the fast velocity changes the task needs.

### Acceleration direction and speed

Upward acceleration fights gravity and draws more joint torque than the same
magnitude downward, so it is charged separately at weight 2, against 0.3 for the
symmetric acceleration cost. At equal magnitude an upward command costs more
than seven times a downward one before the torque term is even counted.

Peak Cartesian speed is measured, not commanded, so a servo that overshoots its
reference is charged for what the arm actually did. Motion below 0.2 m/s is free
and the cost is quadratic in the excess up to the 0.6 m/s action limit. Together
with the slip bonus this points at the slowest single command that still moves
the object, rather than a fast chain of small ones.

### Home height

The hand is pulled toward 0.75 m, free within 3 cm and quadratic outside, steep
well before the hard bounds at 0.6 and 0.96 m. This replaces the v3.1 flat band
across the success window, which had no preference inside it. The settle
reference after a regrasp is still the height where that regrasp happened, so
the two do not fight during a settle.

### Effort annealing

All effort costs are scaled by `k`, which ramps from 0.1 to 1.0 over PPO
iterations 100 to 500. The attempt bonus fades from 1.0 to 0.0 on the same
schedule. The task is learned first, then optimized. The trainer wires the live
PPO iteration into the environment, so the schedule is correct across `--resume`
and across both phases of a `--randomize-at` run. Without a source the
environment uses its fully annealed weights, which is what replay and evaluation
want. `effort_scale` and `attempt_scale` are logged per episode.

The acceleration excess cost is deliberately outside the anneal, so the limit is
enforced from iteration zero.

### Other v3 changes

The forced zero hold no longer charges the raw policy velocity command. Its
duration is randomized and absent from the observation, so that term was
unlearnable noise in the advantage estimate. The physical stability terms still
apply during both hold windows, and the post-regrasp hold still charges the
command, since the policy controls it there.

All dense terms use smooth exponentials, squares or squared hinges. Contact
events, ladder rungs, record updates and terminal conditions are necessarily
discrete. Progress shaping uses the PPO discount, 0.99. Keep those discounts
equal. Its terminal correction prevents collecting progress simply by moving the
object toward the target and then dropping it.

### The success condition

A release requires both fingers below 0.15 N. A regrasp requires both fingers at
least 0.75 N for 60 ms. Those still define the regrasp state machine, but v3.5
removed them from the success test, along with the end-effector height window.
Success now requires all of the following at once, continuously for 200 ms, which
is 10 consecutive policy steps. One bad step resets the counter to zero.

| Condition | Threshold |
|---|---|
| Relative Z error | within 5 mm of the target |
| Lateral offset | within 15 mm |
| End-effector Z speed | below 0.02 m/s |
| End-effector lateral speed | below 0.05 m/s |
| Object slip speed in the hand | below 0.01 m/s |
| Regrasps completed this episode | at least one |
| Step RMS acceleration | at most 4 m/s² |

The hold is 100 ms, which is 5 consecutive policy steps. Velocity is judged per
axis: Z is what the policy commands, while lateral velocity is servo tracking
noise that sits at 0.02 m/s on its own. A shared 3-D budget of 0.02 spent most of
itself on motion the policy never asked for.

Dropping the two-finger force gate is safe because nothing in free fall holds a
near-zero speed relative to the hand for 200 ms, so the slip-speed condition
already implies the object is held.

The height window became a **graded payout** rather than a gate. A success far
from the home pose is still clearly worth having, it is simply worth less:

| Hand height at success | Payout |
|---|---|
| 0.75 m | 100.0 |
| 0.80 m or 0.70 m | 85.3 |
| 0.86 m | 59.3 |
| 0.61 m | 53.3 |

The hold reference is the height at the confirmed regrasp, not a fixed 0.8 m.
Existing workspace and drop checks still terminate the episode, and the
seven-regrasp cutoff applies only when `--limit-regrasp` is enabled.

## Making the hard direction reachable

The two target signs are not equally difficult, and the positive one is the
interesting case: the object must move **up** inside the hand, which the hand can
only cause by diving faster than gravity. Everything below 9.81 m/s² of hand
acceleration produces nothing at all, so the usable acceleration is whatever the
servo allows minus gravity, and the slip grows with the square of the open
window. Both are configured here rather than inherited.

| Hand acceleration | 60 ms window | 80 ms | 100 ms |
|---|---|---|---|
| 12 m/s², before v3.4 | 3.9 mm | 7.0 mm | 10.9 mm |
| 15 m/s², now | 9.3 mm | 16.6 mm | 16.6 mm |

v3.4 raises the servo bound to the measured 15 m/s² limit and lengthens the open
window from 3 steps to 4, giving 80 ms. That takes a pulse from 3.9 mm to
16.6 mm, so a 20 to 40 mm target needs 2 or 3 pulses rather than 5 to 10.

Past 80 ms the ±0.6 m/s action range runs out before the acceleration does: a
sustained 15 m/s² dive uses the whole range in exactly 80 ms. Lengthening the
window further buys nothing without raising `Z_VEL_MAX`, which would change the
action scale and the observation normalization. That is the next lever if 2 to 3
pulses still proves too long a chain.

The regrasp bonus references a 15 mm single-cycle slip, matching what one good
pulse can now deliver, so a well-executed pulse earns full credit rather than the
1.5 out of 40 it earned when the reference was 20 mm and the ceiling was 3.9 mm.

The negative direction is unaffected and remains far easier: the object falls
away from the hand on its own, giving up to 39 mm per window.

### Why v3.6 still never succeeded, measured

Checkpoint 720 of `franka-regrasp-v35` was replayed across 256 environments.
It succeeded on 4 of 512 episodes, which rounds to 0.0% in the training table.

Two things were wrong, and the first one was mine.

**The dense shaping was the largest term in the reward, and it charged for being
near the target.** Potential shaping bleeds `weight * (1 - gamma) * phi` on every
step. At weight 20 over a 450-step horizon that is 4.5 times the useful
telescoped signal, up to -90 against a success bonus of 100. Worse, the leak is
proportional to `phi`, so sitting perfectly on target cost 0.2 per step while
sitting far away cost nothing. The observed `progress` column was -46, larger in
magnitude than every other term combined. The weight is now 4, which caps the
leak at -18.

| Progress weight | Leak per full episode |
|---|---|
| 20, v3.6 | -90.0 |
| 4, v3.7 | -18.0 |

**The hold was a cliff, not a ramp.** Four qualifying steps in a row paid
nothing; five paid a hundred. The measured run lengths stopped at four:

| Candidate run reached | Runs, out of 40 |
|---|---|
| 1 step | 40 |
| 2 steps | 14 |
| 3 steps | 8 |
| 4 steps | 3 |
| 5 steps, what success needed | 0 |

Every single break was the end-effector Z velocity. Nothing in the reward pulled
the policy across that last step, so v3.7 adds a dwell ramp: each new longest
qualifying run this episode pays 4, ratcheted, so reaching the hold pays 20 on
the way in and then the success bonus on arrival. Re-entering the goal region
pays nothing until the policy holds it longer than it ever has that episode.

### Why v3.5 never succeeded, measured

Checkpoint 260 of `franka-regrasp-v34` was replayed across 256 environments for
900 steps, 230,400 samples, tallying every success sub-condition.

Accuracy was never the problem. The best relative-Z error reached per episode had
a median of 1.4 mm against a 5 mm tolerance, and a quarter of episodes got inside
0.3 mm. The full six-condition test passed on 0.81% of steps, so the target state
was being reached regularly.

The binding constraint was the hold, by one step:

| Candidate run reached | Runs, out of 1271 |
|---|---|
| 1 step | 1271 |
| 5 steps | 64 |
| 8 steps | 58 |
| 10 steps, what success needed | 0 |

The longest run ever observed was 9 steps, 180 ms, against a 200 ms requirement.
The policy had therefore never once received the success payout, so it could not
learn that holding pays, which is a deadlock the reward cannot break on its own.

Of the runs that ended early, 95% were ended by the end-effector speed check, and
the lateral component of that speed was already sitting at the 0.02 m/s
threshold on its own. The policy was being judged on motion it does not command.

v3.6 halves the hold to 100 ms and splits the velocity gate per axis. Replaying
the same unchanged checkpoint under the new gate:

| Outcome | Share of 516 episodes |
|---|---|
| Success | 17.2% |
| Failure | 1.2% |
| Timeout | 81.6% |

Raise `SUCCESS_HOLD_SECONDS` back toward 200 ms once successes are routine. The
short hold exists to break the deadlock, not because 100 ms is the right spec.

### What is left of the asymmetry

Even at the raised envelope the signs are not equal, and no reward change makes
them so. Per 80 ms window the negative sign gets about 79 mm of available slip
against the positive sign's 16.6 mm, because one direction adds gravity and the
other subtracts it. The `against_gravity` cost also taxes the easy direction,
which is deliberate but widens the gap further.

With `--mix` the same policy and the same reward scale must serve both. If a
mixed run stalls while a single-sign run does not, that asymmetry is the reason,
and the remedies are to shorten the target magnitude for the positive sign or to
train the signs in separate stages.

## Training in the running Docker container

Start a separate experiment for the new reward:

```bash
docker exec -it -w /workspace genesis python examples/rigid/train_franka_ppo.py \
  --env env_franka_regrasp -B 1024 --max_iterations 1001 \
  -e franka-regrasp-v3 --mix --normalization
```

To transfer an old actor instead of starting randomly, append:

```bash
  --warm-start logs/franka-lift-v1-student-ft3/model_960.pt
```

`--warm-start` loads only actor weights, including its action distribution. It
starts the critic, optimizer and iteration counter fresh because the reward
scale and value function changed. Keep normalization consistent with the source
checkpoint. `--resume` instead restores the full training state and is appropriate
for continuing a policy already trained with this reward. The trainer refuses to
overwrite an existing experiment on a fresh run.

Because the anneal follows the PPO iteration counter, a `--warm-start` run
restarts the schedule at iteration 0 while a `--resume` run continues where the
checkpoint left off.

For the second stage, replace the checkpoint below with the actual saved model:

```bash
docker exec -it -w /workspace genesis python examples/rigid/train_franka_ppo.py \
  --env env_franka_regrasp -B 1024 --max_iterations 1001 \
  -e franka-regrasp-v3-hold --mix --normalization --zero --randomize \
  --resume logs/franka-regrasp-v3/model_1000.pt
```

As in the original training environment, `--zero` arms a pause after a gripper
pulse and starts it at the next nonzero policy velocity sign reversal. It forces
zero velocity and closed fingers for 0.1 s, or a sampled 0.1–0.3 s with
`--randomize`. The new player delegates this to the environment instead of using
its separate legacy 0.5 s override.

`--max_iterations` is the number of additional PPO updates, including on resume.
The network remains the existing [256, 128, 64] ELU MLP with PPO and gamma=0.99;
this change does not introduce a new observation-dependent policy architecture.

## Replay and checks

Evaluate your existing actor under the new reward and controller:

```bash
docker exec -it -w /workspace genesis python examples/rigid/run_policy_franka_parallel.py \
  --env env_franka_regrasp -e franka-lift-v1-student-ft3 --ckpt 960 \
  --normalization --randomize --zero --control-error --plot --no-vis --steps 450
```

For a new experiment, the player reads the environment module from `env_cfg.pkl`
when `--env` is omitted. Continue passing the desired normalization/randomization
flags explicitly. Use `--mix` to evaluate both target signs, or `--negative` for
negative targets only. Remove `--no-vis` for the interactive viewer.

Reward plots include the ladder, attempt bonus, torque, acceleration and
progress. A separate `*_effort.png` plots physics-step peak acceleration against
15 m/s², peak joint torque fraction, and mean squared normalized torque. Episode
reward totals and effort metrics are logged to TensorBoard, with the principal
terms also in the training table. The per-step diagnostics include peak and RMS
acceleration, peak normalized joint torque, acceleration-limit violations,
runaway terminations, the ladder rung reached, confirmed regrasp events and
success or failure.

Run the deterministic reward contract tests without additional packages:

```bash
docker exec -w /workspace genesis python -m unittest discover -s examples/rigid/tests -v
```

The 50 tests check reward direction, two-finger confirmation, stable success,
timeout/failure precedence, bonus farming, ladder ratcheting, anneal behavior,
impulse tolerance in the acceleration limit, hold-penalty observability and
accounting. Two of them roll whole mocked episodes and assert the discounted
return ranks converging above attempting above freezing above dropping, at both
ends of the anneal schedule, so a future weight edit that restores v2's freezing
optimum fails the suite. A third asserts that harvesting the ladder and then
crashing loses to keeping the object, which is the v3.0 exploit. These are contract tests: they establish reward
structure, not convergence.

### What has been verified for v3.7

- All 50 deterministic reward tests pass. They drive the real reward and state
  machine through mocked simulator handles, so they need no GPU.
- v3.6 was measured on GPU. Checkpoint 260 of `franka-regrasp-v34` was replayed
  across 256 environments for 900 steps, before and after the gate change, and
  the numbers above come from those two runs.
- v3.0 did train: 314 PPO iterations at 9,000 steps per second. It is what
  produced the exploit described under **What the first v3 run showed**.

Simulation and PPO results below were collected with the **v2** reward and are
kept only as a baseline:

- GPU checks passed for finite observations/rewards, all 20 physics samples per
  action, selective reset isolation, zero hold, timeout reset, and episode metrics.
- Checkpoint 960 replayed for 120 steps with normalization, randomization, zero
  hold and control error: peak acceleration 12.069 m/s², no acceleration-limit
  violations, one failed episode and no completed successes in that short replay.
- Fresh PPO completed two updates with 16 environments. Actor warm-start PPO
  completed one update with 1,024 environments, finite losses, and some stable
  successes.

Compare success rate, completion time, torque cost and acceleration violations
after training, separately for both target signs.

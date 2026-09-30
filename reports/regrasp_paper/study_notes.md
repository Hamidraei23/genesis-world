# Source audit and literature notes

Prepared 28 September 2026 against the working-tree files, including the existing uncommitted environment and training-script changes. These notes are editorial support and are not part of the conference manuscript.

**Revision:** The author subsequently requested a maximum of three pages for these sections, targeting RA-L, and inclusion of the existing gripper/free-body drawing. `main.tex` now contains that condensed version, with an editable adaptation in `figures/gripper_fbd.tex`. Detailed derivations and implementation auditing below remain background notes; they should not be read as a list of equations still present in the short manuscript. RA-L's initial-submission conference format is retained in accordance with its [author instructions](https://www.ieee-ras.org/publications/ra-l/ra-l-information-for-authors/).

## Scope agreed with the author

The real-world MLP/GRU experiments are not completed. OptiTrack is the selected position-measurement system. The requested deliverable is the abstract, introduction, preliminaries, and methodology; the author will add results and the remaining sections. Physics and PPO learning receive priority.

## Internal evidence used

| Source | Use in the draft |
| --- | --- |
| [Regrasp pulse mathematics](../regrasp_pulse_math/regrasp_pulse_math.tex), accompanying PDF | Relative dynamics, static friction, ideal brake--coast calculation, and timing sensitivity motivation. |
| [Tilted RL report](../rl_tilted/rl_tilted_regrasp_report.tex), accompanying PDF | Task frame, observation/action design, controller, reward, recurrent learning, and restrictions on interpreting existing logs. |
| [Controller pulse report](../controller_pulse/pulse_velocity_analysis.tex), accompanying PDF | Distinction between commanded and realized motion, release latency, lower-finger friction, and hardware-model limitations. |
| [Current environment](../../examples/rigid/env_franka_parallel_tilted.py) | Primary authority for current constants and actual pulse, reward, reset, and termination behavior. |
| [MLP training](../../examples/rigid/train_franka_ppo.py), [GRU training](../../examples/rigid/train_franka_ppo_gru.py) | Network widths, PPO parameters, recurrent defaults, and different rollout lengths. |
| [Pulse analysis implementation](../../franka_controllers/scripts/assess_angle.py) | Independent numerical verification of the manuscript's piecewise displacement formula against its event-driven integrator. |
| [OptiTrack package](../../hrii_optitrack/README.md), [publisher](../../hrii_optitrack/src/optitrack_ros_publisher.cpp), [frame calibration](../../hrii_optitrack/src/robot_to_optitrack_world_tf_broadcaster.cpp) | Existing measurement/frame infrastructure; does not establish an operational policy observation adapter. |
| [Hardware controller documentation](../../franka_controllers/README.md) | Available arm/gripper workflow and Robotiq hardware distinction. |

The provided reports have editable LaTeX alongside their PDFs; the audit used that source for precise equations and checked behavioral claims against Python/C++ where relevant.

## Decisions that prevent overstatement

1. **Reference limits are not realized-motion bounds.** The 0.45 m/s and 15 m/s² constants constrain reference generation. Filtering and tracking can produce a different hand trajectory. The analytical model assumes these bounds apply to the actual gripper. Its displacement is therefore not a strict upper bound for the full Genesis system.
2. **The brake--coast profile is not globally optimal in every friction regime.** The integral proof applies to frictionless motion or upward sliding without finite sticking intervals. The corresponding optimality statement also requires the candidate profile to satisfy that sliding assumption. A negative brake--coast displacement does not prove that all trajectories fail: static-friction holding can help. The manuscript avoids the broader “positive regrasp iff this value is positive” wording found in parts of the source report/script.
3. **125 ms is an analytical effective-release assumption.** The current commanded pulse has a 40 ms policy-pass-through phase, 120 ms forced opening, and a 20 ms closing-command step. Neither 120 nor 160 ms automatically equals the interval between physical release and recapture. The policy can influence the initial phase, so it is not a pure dead-time model.
4. **The current delay is not randomized.** Both nominal pulse length bounds equal seven steps, and delay is fixed at two steps. Friction and finger gains are randomized at reset; optional noise randomization is additional. No robustness claim for unseen timing distributions is made.
5. **Tilt is fixed for a run and hidden from the actor.** The supported 0–45 degree configuration range is not evidence of one policy generalizing across that range.
6. **GRU is a motivated architecture, not a proven advantage.** Hidden state may retain information about contact and pulse history, but the training objective does not identify those variables explicitly. The current MLP and GRU scripts also use different rollout lengths (32 versus 64). A controlled comparison must account for rollout length, sample budget, critic choice, and parameter count.
7. **Existing percentages are training diagnostics.** The RL report's 95.5846% value is a mean of the last 50 logged success-rate values for `tilt45-gru-speed`, not a new held-out evaluation. The runs lack source snapshots and span reward revisions. None of those figures appears as a performance result in this draft.
8. **OptiTrack does not replace contact sensing.** The current policy expects two simulated net finger-contact-force magnitudes. Hardware force availability and equivalence are unresolved. Supplying zeros or treating motor current as an interchangeable force measurement would change the observation model and cannot be assumed valid.
9. **Calibration and time alignment are requirements, not completed work.** The marker-body-to-object-center offset is separate from the robot-to-mocap calibration. The existing publisher aligns world axes and stamps its outgoing pose arrays/transforms with `ros::Time::now()`. That is not proof of acquisition-time synchronization. The draft describes the required interface, without inventing camera rate, latency, accuracy, or calibration results. The OptiTrack package is ROS 1 while the available controller documentation describes a ROS 2 workflow; the observation transport/bridge also needs to be specified before claiming integration.
10. **Reward properties are described narrowly.** The quartic event reward is not potential-based shaping. The smoothness refund is exact only without discounting. The source report's broad suggestion that loitering cannot beat finishing is not asserted: its own discounted-return examples depend on how many pulses finishing requires.

## Closest literature and positioning

The BibTeX entries link to primary papers, proceedings, or author/institutional publication pages. No secondary summary is used as the basis of the related-work claims.

| Reference | Established idea | Positioning of this draft |
| --- | --- | --- |
| [Chavan-Dafle et al., ICRA 2014](https://publications.ri.cmu.edu/extrinsic-dexterity-in-hand-manipulation-with-external-forces) | A simple gripper can exploit gravity, external forces, and arm motion for scripted regrasp operations. | Use extrinsic dexterity as the physical starting point; do not claim simple-gripper regrasping is new. |
| [Shi et al., T-RO 2017](https://robotics.northwestern.edu/documents/publications/dynamic-in-hand-sliding-manipulation-tro.pdf) | Contact-based planning of inertial sliding, with iterative execution to reduce modeling/tracking errors; the experimental setup includes motion capture. | This is the closest foundational comparator. Learning pulse coordination under hidden actuator/contact state is the distinction to investigate, not iteration or motion capture itself. |
| [Wei et al., arXiv 2023](https://arxiv.org/abs/2309.15455) | Demonstration-based learning of gravity-driven sliding with object-position feedback. | Discuss as prior learned regrasping, distinguish its GAIL/demonstration setup from PPO with the present reward and pulse interface. Cite it as a preprint; no conference acceptance is assumed. |
| [Ma and Adelson, ICRA RoboLetics workshop 2025](https://openreview.net/pdf?id=W9Lbb7quQ1) | Asynchronous vision helps refine a throw-and-catch strategy using planning and computed torque control. | Close feedback comparator; do not describe all previous dynamic approaches as open loop. Workshop status is confirmed by the [author's CV](https://yuxiang-ma.github.io/files/Resume_Yuxiang.pdf), not treated as an ICRA main-conference paper. The indexed primary PDF was available through search, although direct OpenReview access intermittently returned a browser challenge. |
| [Peng et al., ICRA 2018](https://arxiv.org/abs/1710.06537) | Dynamics randomization for transfer of learned robot control. | Supports the rationale for contact variability, not proof that the present randomization covers the real gripper. |
| [OpenAI et al., IJRR 2020](https://arxiv.org/abs/1808.00177) | Learned in-hand manipulation on a multifingered hand using extensive simulation randomization. | Provides broader context without transferring its experimental claims to parallel-gripper regrasping. |
| [Ni et al., ICML 2022](https://proceedings.mlr.press/v162/ni22a.html) | Recurrent model-free control can perform well in POMDPs when architecture and training are chosen carefully. | Motivates GRU memory; does not demonstrate a GRU advantage on this task. |
| [Schulman et al., PPO](https://arxiv.org/abs/1707.06347) and [GAE](https://arxiv.org/abs/1506.02438) | Policy optimization and advantage estimation. | Algorithmic references for the actual training formulation. |

The defensible research question is: can a policy learn to coordinate bounded axial commands and a delayed, constrained gripper interface, correcting residual position error over successive pulses, and does memory help when relevant execution state is hidden? A calibrated analytical controller remains a legitimate comparator; the physics does not show that model-based control must fail.

## Checks completed and items for the future results section

The earlier draft's brake--coast formula was compared against `relative_motion` over ten inclinations, six friction values, and five durations (300 cases). The maximum discrepancy was approximately 2.60e-17 m. The vertical illustrative value is 8.859375 mm. These are equation checks, not new task-performance experiments. This extended derivation and numerical example have been removed from the three-page draft.

Before making experimental claims, record the exact source snapshot, environment configuration, training seeds, number of environments, total transitions, checkpoints, and evaluation episode counts. Compare MLP and GRU with controlled training budgets and conditions; evaluate calibrated fixed pulses and iterative analytical control as baselines. Report final error, settled success, attempts, completion time, and failure causes. For hardware, additionally report frame calibration, measurement age, gripper release/recapture latency and jitter, the force-observation solution, and measured versus commanded arm motion. No outcomes for these studies have been invented or inserted.

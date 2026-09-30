# Iterative dynamic regrasping manuscript

The condensed RA-L draft is at most **three pages including its figure and references**. It contains an abstract, an introduction with related work, system and physical formulation, and learning methodology. It prioritizes `regrasp_pulse_math` and `rl_tilted`; `controller_pulse` supports the discussion of realized motion and timing. As requested, it contains no experimental results section, comparative performance claims, or completed hardware-transfer claims. Authors and affiliations remain a visible placeholder.

- [Compiled manuscript](main.pdf)
- [Editable LaTeX](main.tex)
- [BibTeX references](references.bib)
- [Source audit and literature notes](study_notes.md)

Build from this directory:

```bash
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

The document uses the official RAS `ieeeconf` class, US Letter, 10 pt, and two columns. [RA-L author instructions](https://www.ieee-ras.org/publications/ra-l/ra-l-information-for-authors/) specify the RAS conference layout for initial submissions and a journal layout for accepted final versions. The three-page cap here is the author's budget for these preliminary sections, not RA-L's full-paper limit. The unmodified class comes from the [RAS PaperCept template archive](https://ras.papercept.net/conferences/support/files/ieeeconf.zip). Standard TeX Live packages, TikZ, `balance`, and `IEEEtran.bst` supply the remaining dependencies.

For Overleaf, upload [the source ZIP](regrasp_paper_source.zip) and select `main.tex` as the main document. It includes `figures/gripper_fbd.tex`, an editable two-panel vector adaptation of the grasped/released free-body diagram in `../regrasp_pulse_math/fbd_section.tex`. The manuscript explicitly references the figure when defining the geometry and contact dynamics. Keep the `figures/` directory when moving the source.

The source ends with a comment marking where to add Experiments, Results, Discussion, and Conclusion. The present abstract deliberately describes scope and method without announcing experimental outcomes; revise its final sentences after evaluation.

The shorter version removes the long brake--coast derivation, full controller equations, reward-coefficient bookkeeping, and the PPO parameter table. The source audit remains in `study_notes.md` for future expansion.

Validation performed: PDFLaTeX/BibTeX compilation; citation and figure/equation-reference resolution; page count, embedded fonts, and visual layout checks. The earlier full derivation was checked against the workspace's event-driven integrator over 300 parameter combinations; that derivation is omitted from the condensed manuscript. No training, new simulation experiments, or robot execution was performed.

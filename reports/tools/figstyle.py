"""Shared matplotlib style for the report figures (print, light surface)."""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

# Categorical slots, fixed order (validated reference palette, light mode).
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e6e5e1"
SURFACE = "#ffffff"
SHADE = "#f0efec"
# Sequential blue ramp, light -> dark (ordinal steps start at 250 for contrast).
BLUE = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]
# Diverging blue <-> red with a neutral gray midpoint.
DIVERGING = LinearSegmentedColormap.from_list(
    "bluegrayred", ["#184f95", "#6da7ec", "#f0efec", "#ec8a86", "#b3261e"]
)

TEXTWIDTH = 6.5  # inches


def setup():
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8.5,
            "axes.titlesize": 9,
            "axes.titleweight": "bold",
            "axes.labelsize": 8.5,
            "axes.edgecolor": INK2,
            "axes.labelcolor": INK,
            "axes.linewidth": 0.7,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": INK2,
            "ytick.color": INK2,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7.5,
            "legend.frameon": False,
            "lines.linewidth": 1.8,
            "lines.solid_capstyle": "round",
            "figure.dpi": 150,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.03,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
        }
    )


def save(fig, path):
    fig.savefig(path)
    plt.close(fig)
    print("wrote", path)


def label_end(ax, x, y, text, color=INK2, dx=4, dy=0, ha="left", **kw):
    """Direct label next to a line end, in text ink (never the series color)."""
    ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points", va="center", ha=ha,
                fontsize=7.5, color=color, **kw)

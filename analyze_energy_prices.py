"""
German household electricity prices, adjusted for inflation.

Reads data/Strompreise_deutschland.xlsx and writes three charts plus a CSV
of the computed numbers into output/.

The workbook has one row per year with:
  - the total electricity price in ct/kWh
  - three cost components that add up to that total
  - the consumer price inflation rate for that year

Run it with:
    python analyze_energy_prices.py
"""

from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")  # render to files, no interactive window needed

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
DATA_FILE = PROJECT_DIR / "data" / "Strompreise_deutschland.xlsx"
OUTPUT_DIR = PROJECT_DIR / "output"

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# Every real (inflation-adjusted) price is expressed in the money of this year.
# 2026 is the last year in the workbook, so the charts read as "in today's money".
BASE_YEAR = 2026

# The workbook jumps from 1998 straight to 2010 and gives no inflation rates for
# 1999-2009, so the chain of yearly rates cannot bridge that gap on its own.
# This one constant closes it, using the official German consumer price index
# (Destatis Verbraucherpreisindex, base 2020 = 100): 1998 = 74.4, 2010 = 88.6.
# Change this single number if you want to use a different price index.
CPI_1998_TO_2010 = 88.6 / 74.4  # ~1.191, i.e. about 19% cumulative inflation

# --------------------------------------------------------------------------
# Colors (validated categorical palette; see README)
# --------------------------------------------------------------------------

COLOR_TAXES = "#2a78d6"  # blue
COLOR_GRID = "#eb6834"  # orange
COLOR_PROCUREMENT = "#1baf7a"  # aqua
COLOR_TOTAL = "#3b3a37"  # near-black: the total is a sum, not a peer category

INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#8a8a84"
SURFACE = "#fcfcfb"
GRIDLINE = "#e4e3df"

# Column names as they appear in the workbook, mapped to short English labels.
COMPONENTS = {
    "Steuern, Abgaben, Umlagen (ct/kWh)": "Taxes, levies & surcharges",
    "Netznutzungsentgelte (ct/kWh)": "Grid fees",
    "Strombeschaffung, Vertrieb (ct/kWh)": "Procurement & sales",
}
COMPONENT_COLORS = {
    "Taxes, levies & surcharges": COLOR_TAXES,
    "Grid fees": COLOR_GRID,
    "Procurement & sales": COLOR_PROCUREMENT,
}
TOTAL_LABEL = "Total price"


# --------------------------------------------------------------------------
# Step 1: load the workbook
# --------------------------------------------------------------------------


def load_data() -> pd.DataFrame:
    """Read the Excel file and return a tidy table indexed by year."""
    raw = pd.read_excel(DATA_FILE, sheet_name=0)

    # Drop the empty trailing columns Excel leaves behind.
    raw = raw.loc[:, ~raw.columns.astype(str).str.startswith("Unnamed")]

    df = pd.DataFrame(index=raw["Jahr"].astype(int))
    df.index.name = "year"
    df[TOTAL_LABEL] = raw["Strompreis in ct/kWh"].to_numpy(dtype=float)

    for german_name, english_label in COMPONENTS.items():
        df[english_label] = raw[german_name].to_numpy(dtype=float)

    # "0,8 %" -> 0.008. German decimal comma, percent sign, non-breaking spaces.
    inflation_text = (
        raw["Inflationsrate"]
        .astype(str)
        .str.replace("%", "", regex=False)
        .str.replace("\xa0", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.strip()
    )
    # .to_numpy() matters here: `raw` is numbered 0, 1, 2... while `df` is indexed
    # by year, and assigning a Series would line the two indexes up and give NaN.
    df["inflation_rate"] = inflation_text.to_numpy(dtype=float) / 100.0

    return df


# --------------------------------------------------------------------------
# Step 2: turn yearly inflation rates into a price-level index
# --------------------------------------------------------------------------


def build_price_index(df: pd.DataFrame) -> pd.Series:
    """
    Build a consumer price index for every year in the table.

    The index is anchored at 1.0 in the first year that has an unbroken chain of
    yearly inflation rates (2010), then multiplied forward one year at a time.
    1998 is placed below 2010 using the CPI_1998_TO_2010 constant, because the
    workbook has no inflation rates for 1999-2009.
    """
    years = list(df.index)
    chain_start = min(y for y in years if y >= 2010)

    index = pd.Series(index=df.index, dtype=float)
    index.loc[chain_start] = 1.0

    previous_year = chain_start
    for year in years:
        if year <= chain_start:
            continue
        # The rate listed for a year is that year's rise over the year before.
        index.loc[year] = index.loc[previous_year] * (1.0 + df.loc[year, "inflation_rate"])
        previous_year = year

    for year in years:
        if year < chain_start:
            index.loc[year] = index.loc[chain_start] / CPI_1998_TO_2010

    return index


def to_real_prices(df: pd.DataFrame, price_index: pd.Series) -> pd.DataFrame:
    """Convert nominal ct/kWh into BASE_YEAR money."""
    value_columns = [TOTAL_LABEL] + list(COMPONENTS.values())
    deflator = price_index.loc[BASE_YEAR] / price_index
    return df[value_columns].mul(deflator, axis=0)


# --------------------------------------------------------------------------
# Shared chart styling
# --------------------------------------------------------------------------


def style_axes(ax, ylabel: str) -> None:
    """Recessive grid and axes, so the data lines stay the loudest thing."""
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    ax.set_ylabel(ylabel, color=INK_SECONDARY, fontsize=10.5, labelpad=10)
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRIDLINE)
    ax.tick_params(colors=INK_SECONDARY, labelsize=9.5, length=0)


def add_titles(fig, title: str, subtitle: str) -> None:
    fig.text(0.055, 0.965, title, fontsize=16, color=INK_PRIMARY, fontweight="bold", va="top")
    fig.text(0.055, 0.915, subtitle, fontsize=10.5, color=INK_SECONDARY, va="top")


def add_source_note(fig, note: str) -> None:
    fig.text(0.055, 0.025, note, fontsize=8.5, color=INK_MUTED, va="bottom")


# --------------------------------------------------------------------------
# Chart 1: line chart of inflation-adjusted prices
# --------------------------------------------------------------------------


def chart_real_price_lines(real: pd.DataFrame, output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 7))
    style_axes(ax, f"Real price in ct/kWh ({BASE_YEAR} money)")

    years = np.array(real.index, dtype=float)
    chain_years = years[years >= 2010]

    # Short names for the labels sitting at the right edge; the legend carries
    # the full ones, so there is no room to lose here.
    series_to_draw = [(TOTAL_LABEL, "Total", COLOR_TOTAL, 2.6)] + [
        (label, short, COMPONENT_COLORS[label], 2.0)
        for label, short in zip(
            COMPONENTS.values(), ["Taxes & levies", "Grid fees", "Procurement"]
        )
    ]

    for label, _short, color, width in series_to_draw:
        values = real[label]
        ax.plot(
            chain_years, values.loc[values.index >= 2010],
            color=color, linewidth=width, marker="o", markersize=4.5,
            markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3, label=label,
        )
        # 1998 sits alone on the other side of the gap: a point, not a line.
        if not np.isnan(values.loc[1998]):
            ax.plot(
                1998, values.loc[1998], marker="o", markersize=9, color=color,
                markeredgecolor=SURFACE, markeredgewidth=1.4, zorder=3,
            )
            ax.annotate(
                f"{values.loc[1998]:.1f}", xy=(1998, values.loc[1998]),
                xytext=(-13, 0), textcoords="offset points",
                color=color, fontsize=9.5, fontweight="bold", ha="right", va="center",
            )

    # Axis limits have to be fixed before anything is positioned relative to them.
    ax.set_xlim(1996.0, 2032.0)
    tick_years = [1998] + list(range(2010, BASE_YEAR + 1, 2))
    ax.set_xticks(tick_years)
    ax.set_xticklabels([str(year) for year in tick_years])
    ax.set_ylim(0, real[TOTAL_LABEL].max() * 1.12)

    # Shade the stretch the workbook has no data for, so the gap is explicit.
    ax.axvspan(1999.0, 2009.2, color="#f2f1ed", zorder=0)
    ax.text(
        2004.1, ax.get_ylim()[1] * 0.50, "no data\n1999-2009",
        ha="center", va="center", fontsize=10, color=INK_MUTED, linespacing=1.5,
    )

    # Direct labels at the right edge, nudged apart so they never overlap.
    label_positions = sorted(
        (
            (real.loc[BASE_YEAR, full_label], short, color)
            for full_label, short, color, _ in series_to_draw
        ),
        key=lambda item: item[0],
    )
    minimum_gap = (ax.get_ylim()[1] - ax.get_ylim()[0]) * 0.062
    placed_y: list[float] = []
    for value, short, color in label_positions:
        y = value if not placed_y else max(value, placed_y[-1] + minimum_gap)
        placed_y.append(y)
        ax.annotate(
            f"{short}  {value:.1f}",
            xy=(BASE_YEAR, value), xytext=(BASE_YEAR + 0.6, y),
            color=color, fontsize=10.5, fontweight="bold", va="center",
        )

    ax.legend(
        loc="upper left", bbox_to_anchor=(0.005, 0.99), frameon=False,
        fontsize=10, labelcolor=INK_SECONDARY, handlelength=1.6,
    )

    add_titles(
        fig,
        "Inflation-adjusted electricity prices in Germany",
        f"Household price and its three cost components, in constant {BASE_YEAR} "
        f"cents per kWh. 1998 shown separately: the data jumps to 2010.",
    )
    add_source_note(
        fig,
        "Source: Strompreise_deutschland.xlsx. Deflated with the yearly inflation rates in the "
        "workbook; the 1998-2010 gap uses the Destatis consumer price index. 2026 is a forecast.",
    )
    fig.subplots_adjust(left=0.075, right=0.985, top=0.845, bottom=0.10)
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


# --------------------------------------------------------------------------
# Chart 2: 100% stacked columns of each component's share of the price
# --------------------------------------------------------------------------


def chart_share_columns(df: pd.DataFrame, output_path: Path) -> None:
    """
    One column per year, each split into the shares the three components make up
    of the total price. Shares are the same whether you use nominal or real
    prices, because inflation scales every component by the same factor.
    """
    fig, ax = plt.subplots(figsize=(13, 7))
    style_axes(ax, "Share of the total electricity price")

    years = list(df.index)
    x_positions = np.arange(len(years))
    bar_width = 0.72

    taxes_label, grid_label, procurement_label = list(COMPONENTS.values())

    shares = pd.DataFrame(index=df.index)
    for label in COMPONENTS.values():
        shares[label] = df[label] / df[TOTAL_LABEL] * 100.0

    # 1998 reports only the tax share; grid fees and procurement were still
    # bundled together back then, so show them as one hatched segment.
    combined_1998 = 100.0 - shares.loc[1998, taxes_label]

    bottoms = np.zeros(len(years))
    for label in COMPONENTS.values():
        heights = shares[label].to_numpy(dtype=float)
        heights = np.nan_to_num(heights, nan=0.0)
        ax.bar(
            x_positions, heights, bottom=bottoms, width=bar_width,
            color=COMPONENT_COLORS[label], label=label,
            linewidth=1.6, edgecolor=SURFACE, zorder=2,
        )
        # Label the segment when it is tall enough to hold a number.
        for x, height, bottom in zip(x_positions, heights, bottoms):
            if height >= 9:
                ax.text(
                    x, bottom + height / 2, f"{height:.0f}",
                    ha="center", va="center", fontsize=8.5,
                    color="#ffffff", fontweight="bold", zorder=4,
                )
        bottoms = bottoms + heights

    index_1998 = years.index(1998)
    ax.bar(
        index_1998, combined_1998, bottom=shares.loc[1998, taxes_label], width=bar_width,
        color=COLOR_GRID, hatch="//", edgecolor=SURFACE, linewidth=1.6, alpha=0.8, zorder=2,
    )
    ax.text(
        index_1998, shares.loc[1998, taxes_label] + combined_1998 / 2,
        f"{combined_1998:.0f}", ha="center", va="center", fontsize=8.5,
        color="#ffffff", fontweight="bold", zorder=4,
    )

    ax.set_xticks(x_positions)
    ax.set_xticklabels([str(y) for y in years], rotation=0, fontsize=9)
    ax.set_ylim(0, 100)
    ax.set_yticks(range(0, 101, 20))
    ax.set_yticklabels([f"{t}%" for t in range(0, 101, 20)])

    handles = [Patch(facecolor=COMPONENT_COLORS[label], label=label) for label in COMPONENTS.values()]
    handles.append(
        Patch(facecolor=COLOR_GRID, hatch="//", alpha=0.8,
              label="Grid fees + procurement (1998, reported together)")
    )
    ax.legend(
        handles=handles, loc="lower center", bbox_to_anchor=(0.5, -0.185),
        ncol=2, frameon=False, fontsize=10, labelcolor=INK_SECONDARY,
    )

    add_titles(
        fig,
        "What you actually pay for, year by year",
        "Each component as a percentage of the total electricity price. "
        "Columns always add up to 100%, so the shifting split is the story.",
    )
    add_source_note(
        fig,
        "Source: Strompreise_deutschland.xlsx. Shares are the same before and after inflation "
        "adjustment, because inflation scales every component equally. 2026 is a forecast.",
    )
    fig.subplots_adjust(left=0.075, right=0.985, top=0.845, bottom=0.20)
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


# --------------------------------------------------------------------------
# Chart 3: how much each component really grew between 2010 and the base year
# --------------------------------------------------------------------------


def chart_real_change_summary(real: pd.DataFrame, output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 6))
    style_axes(ax, f"Real change 2010 to {BASE_YEAR}")

    labels = [TOTAL_LABEL] + list(COMPONENTS.values())
    changes = [
        (real.loc[BASE_YEAR, label] / real.loc[2010, label] - 1.0) * 100.0
        for label in labels
    ]
    colors = [COLOR_TOTAL] + [COMPONENT_COLORS[label] for label in COMPONENTS.values()]

    bars = ax.bar(
        range(len(labels)), changes, width=0.6, color=colors,
        linewidth=1.6, edgecolor=SURFACE, zorder=2,
    )

    for bar, change in zip(bars, changes):
        offset = 1.6 if change >= 0 else -1.6
        ax.text(
            bar.get_x() + bar.get_width() / 2, change + offset,
            f"{change:+.0f}%", ha="center",
            va="bottom" if change >= 0 else "top",
            fontsize=12, fontweight="bold", color=INK_PRIMARY, zorder=4,
        )

    ax.axhline(0, color=INK_SECONDARY, linewidth=1.1, zorder=3)
    ax.spines["bottom"].set_visible(False)  # the zero line is the baseline here
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels([label.replace(" & ", " &\n") for label in labels], fontsize=10)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=0))
    span = max(abs(min(changes)), abs(max(changes)))
    ax.set_ylim(min(0, min(changes)) - span * 0.25, max(0, max(changes)) + span * 0.25)

    add_titles(
        fig,
        f"Real growth since 2010, after inflation",
        f"Change in constant {BASE_YEAR} cents per kWh. Above zero means the component "
        f"outpaced inflation.",
    )
    add_source_note(fig, "Source: Strompreise_deutschland.xlsx. 2026 is a forecast.")
    fig.subplots_adjust(left=0.10, right=0.975, top=0.825, bottom=0.13)
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    df = load_data()
    price_index = build_price_index(df)
    real = to_real_prices(df, price_index)

    # Write the computed numbers out so the charts can be checked against a table.
    table = pd.DataFrame(index=df.index)
    table["price_index"] = price_index.round(4)
    for label in [TOTAL_LABEL] + list(COMPONENTS.values()):
        table[f"{label} (nominal)"] = df[label].round(2)
        table[f"{label} (real {BASE_YEAR})"] = real[label].round(2)
    csv_path = OUTPUT_DIR / "energy_prices_real.csv"
    table.to_csv(csv_path)

    chart_real_price_lines(real, OUTPUT_DIR / "01_real_prices_lines.png")
    chart_share_columns(df, OUTPUT_DIR / "02_price_shares_columns.png")
    chart_real_change_summary(real, OUTPUT_DIR / "03_real_change_since_2010.png")

    print(f"Wrote {csv_path.relative_to(PROJECT_DIR)}")
    for name in sorted(p.name for p in OUTPUT_DIR.glob("*.png")):
        print(f"Wrote output/{name}")

    print(f"\nReal prices in {BASE_YEAR} cents per kWh:")
    print(real.round(1).to_string())


if __name__ == "__main__":
    main()

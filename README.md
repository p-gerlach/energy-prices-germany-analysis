# German electricity prices, adjusted for inflation

Reads a spreadsheet of German household electricity prices and produces three
charts showing how the real, inflation-adjusted price and its cost components
have moved between 1998 and 2026.

## How to run it

```bash
pip install -r requirements.txt
python analyze_energy_prices.py
```

Everything lands in `output/`: three PNG charts and one CSV with the numbers
behind them.

## The data

`data/Strompreise_deutschland.xlsx` has one row per year covering 1998 and then
every year from 2010 to 2026. Each row gives the total household price in cents
per kilowatt-hour, the three cost components that add up to that total, and the
consumer price inflation rate for the year.

| Column in the workbook | Used here as |
| --- | --- |
| `Strompreis in ct/kWh` | Total price |
| `Steuern, Abgaben, Umlagen (ct/kWh)` | Taxes, levies & surcharges |
| `Netznutzungsentgelte (ct/kWh)` | Grid fees |
| `Strombeschaffung, Vertrieb (ct/kWh)` | Procurement & sales |
| `Inflationsrate` | Yearly consumer price inflation |

## How the inflation adjustment works

A price in 2010 cents and a price in 2026 cents are not the same thing, so the
nominal figures in the workbook cannot be compared directly. The script converts
all of them into 2026 money in three steps.

1. **Chain the yearly inflation rates into a price index.** The index starts at
   1.0 in 2010 and is multiplied by one plus each following year's rate. By 2026
   it reaches 1.4188, meaning the general price level rose about 42 percent.
2. **Divide out the price level.** Each year's nominal price is multiplied by the
   ratio of the 2026 index to that year's index.
3. **Compare.** Every figure is now in constant 2026 cents per kilowatt-hour, so
   a rise on the chart is a real rise in what electricity costs relative to
   everything else.

### The 1998 gap, stated plainly

The workbook skips 1999 to 2009 and gives no inflation rates for those years, so
the chain cannot reach back from 2010 to 1998 on its own. One constant at the top
of the script closes that gap, using the official German consumer price index:

```python
CPI_1998_TO_2010 = 88.6 / 74.4  # Destatis Verbraucherpreisindex, base 2020 = 100
```

That is the only number in the analysis that does not come from the workbook.
It is isolated in one place so you can swap in a different index and re-run.
The charts also mark 1999 to 2009 as a shaded "no data" band and draw 1998 as a
standalone point rather than connecting a line across the gap, so the missing
stretch is never mistaken for a trend.

Two further caveats worth knowing. The 2026 row is a forecast, not a measurement.
And 1998 reports only the total price and the tax component, because grid fees
and procurement were still billed together before the market was unbundled.

## The charts

### 1. `01_real_prices_lines.png`

A line chart with all four series on one set of axes: the total price and each of
the three components, in constant 2026 cents per kilowatt-hour. This is the chart
that answers "did electricity really get more expensive, or did everything?"

### 2. `02_price_shares_columns.png`

A 100 percent stacked column chart, one column per year, splitting each year's
price into the percentage share each component makes up.

Columns rather than pie charts, deliberately. Eighteen pies would each need to be
read separately and then held in memory to compare, and the human eye judges
angles poorly. Eighteen stacked columns put the same information on a shared
baseline where a share that grows or shrinks is visible as a change in height
across the row. The 1998 column shows grid fees and procurement as one hatched
segment, since the workbook does not split them for that year.

### 3. `03_real_change_since_2010.png`

A bar chart summarising the whole period: how much each component grew or shrank
in real terms between 2010 and 2026.

## What the numbers say

- **The total real price rose about 11 percent between 2010 and 2026.** Nominally
  it went from 23.7 to 37.2 cents, which looks like a 57 percent jump, but most of
  that is ordinary inflation rather than electricity specifically getting dearer.
- **The composition changed far more than the total did.** Taxes, levies and
  surcharges were 53 percent of the bill in 2017 and are 34 percent in 2026.
  Procurement and sales went the other way, from a low of 22 percent in 2017 to
  41 percent in 2026.
- **2022 is the outlier.** The real total price spiked to 52.5 cents, and almost
  all of that came from procurement, which more than doubled in one year.
- **Taxes and levies are the one component that fell in real terms**, down about
  8 percent since 2010, mostly through the wind-down of the renewables surcharge
  after 2021.
- **Grid fees are the quiet riser**, up about 13 percent in real terms since 2010.
  They peaked at 11.9 real cents in 2024, well after the 2022 energy shock had
  passed, and the 2026 forecast pulls them back to 9.3.

## Chart colors

The three components use a categorical palette checked with a colorblind-safety
validator: blue `#2a78d6`, orange `#eb6834`, aqua `#1baf7a`. The total price uses
a near-black instead of a fourth hue, because it is the sum of the other three
rather than a category alongside them. Every series is also labelled directly on
the chart, so color is never the only thing carrying identity.

## Files

```
analyze_energy_prices.py   the whole analysis, top to bottom
data/                      the source spreadsheet
output/                    charts and the computed CSV
requirements.txt           pandas, matplotlib, openpyxl
```

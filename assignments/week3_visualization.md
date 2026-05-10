---
slug: week3_visualization
week: 3
title: "Data Visualization"
max_credit_questions: 5
categories:
  - name: histogram
  - name: scatter
  - name: boxplot
  - name: lineplot
  - name: interpretation
  - name: code
  - name: workbook
---

## Question q1

```yaml
qid: q1
category: histogram
type: image
max_attempts: 6
rubric: |
  - Axes labeled with units (e.g., "Net Income ($M)")
  - Bin count is reasonable for the sample size (no over- or under-binning)
  - Title or caption identifies the variable and time period
  - Histogram shape (skew, modality) is visually readable
```

Using the S&P 500 firm-year panel in `data/sp500_fundamentals.csv`, build a
histogram of annual `net_income` for the most recent fiscal year. Choose a bin
count that surfaces the distribution shape (long right tail) without hiding
detail. Upload a PNG of the chart.

## Question q2

```yaml
qid: q2
category: histogram
type: image
max_attempts: 6
rubric: |
  - Two histograms displayed on a shared x-axis (or overlaid with transparency)
  - Clear legend distinguishing the two industries
  - Bins comparable across the two groups
  - Caption notes any extreme outliers excluded
```

Compare the distribution of `gross_margin` between the Technology and Retail
industries using two histograms on a shared axis. Decide whether to overlay
with transparency or to stack vertically, and justify the choice in a one-line
caption.

## Question q3

```yaml
qid: q3
category: scatter
type: image
max_attempts: 6
rubric: |
  - x-axis is `revenue`, y-axis is `net_income`, both clearly labeled with units
  - Each point represents one firm-year
  - Point transparency / size chosen so overplotting in the dense region is readable
  - Title or annotation identifies the year(s) covered
```

Build a scatter plot of `net_income` against `revenue` for all firm-years in
the panel. Address overplotting in the dense low-revenue region — try alpha
blending or smaller marker size. Upload the PNG.

## Question q4

```yaml
qid: q4
category: scatter
type: text
max_attempts: 6
rubric: |
  - Identifies that high-revenue, low-margin firms cluster differently than
    high-revenue, high-margin firms
  - References at least one specific industry visible in the cluster pattern
  - Distinguishes correlation from causation in plain language
  - 3–6 sentences, no jargon dump
```

Look at the scatter you produced in q3 and write a 3–6 sentence interpretation:
what does the shape of the cloud tell you about the relationship between firm
size and profitability across industries? Be specific about which firms or
industries are driving any clusters you see.

## Question q5

```yaml
qid: q5
category: boxplot
type: image
max_attempts: 6
rubric: |
  - One box per industry on the same axis
  - y-axis is `return_on_assets` (ROA), labeled with units (percent or decimal)
  - Outliers visible (not clipped) and either drawn as dots or annotated
  - Industries ordered in a way that aids comparison (e.g., by median)
```

Produce a boxplot of `return_on_assets` by `industry`, with one box per
industry on a single axis. Order the boxes by median ROA so the reader can
scan from lowest to highest. Keep outliers visible.

## Question q6

```yaml
qid: q6
category: boxplot
type: image
max_attempts: 6
rubric: |
  - Side-by-side boxplots split by both `industry` and `fiscal_year`
  - Axis labels include units; legend identifies the year groups
  - Whiskers and IQR boxes visually distinguishable
  - Caption identifies which year shows the largest spread and in which industry
```

Extend q5 by splitting each industry's box into two side-by-side boxes: one for
`fiscal_year = 2019` and one for `fiscal_year = 2020`. The point is to make
year-over-year shifts visible at a glance. Add a one-line caption noting the
industry with the largest 2019→2020 shift in median ROA.

## Question q7

```yaml
qid: q7
category: lineplot
type: image
max_attempts: 6
rubric: |
  - x-axis is `fiscal_year`, y-axis is the chosen aggregate metric, both labeled
  - One line per industry with a clear legend
  - Recession or shock years annotated (e.g., a vertical band in 2020)
  - Y-axis starts at a value justified in a one-line caption (zero vs. non-zero)
```

Plot a multi-line time series of mean `operating_margin` by `industry` over
the years available in the panel. Include a vertical band or annotation for
2020 (COVID year) and a one-line caption justifying whether your y-axis starts
at zero or not.

## Question q8

```yaml
qid: q8
category: lineplot
type: text
max_attempts: 6
rubric: |
  - Picks the lineplot from q7 over the boxplot from q5/q6 OR vice versa, with reasoning
  - Reasoning ties the chart choice to the *question* the audience is asking
  - Notes a specific failure mode of the rejected chart for this audience
  - 3–6 sentences
```

Suppose your audience is the audit committee, and they want to know whether
operating-margin trends across industries diverged during 2018–2021. Would you
present them the lineplot from q7 or the boxplots from q5/q6? In 3–6 sentences,
defend your choice and name one thing the rejected chart would obscure.

## Question q9

```yaml
qid: q9
category: interpretation
type: text
max_attempts: 6
rubric: |
  - Names a specific reason a histogram is preferred over a single summary
    statistic (mean / median) for income or asset data
  - References at least one feature of the distribution (skew, outliers,
    multimodality, or tail behavior) that a single statistic would hide
  - Distinguishes "what shape looks like" from "what shape implies" in plain
    accounting terms (e.g. that a long right tail in revenue suggests a few
    very large firms dominate the sample)
  - 2–4 sentences, no jargon dump
```

In 2–4 sentences, explain why a histogram of firm-level annual `net_income`
is more informative than reporting only the mean and median. Reference at
least one feature of the distribution that a single summary statistic would
hide, and connect that feature back to what it tells the reader about the
underlying firms.

## Question q10

```yaml
qid: q10
category: code
type: python
max_attempts: 6
rubric: |
  - Reads `data/sp500_fundamentals.csv` via pandas (or polars) and selects
    the most recent `fiscal_year`
  - Computes a histogram of `net_income` using matplotlib or seaborn
  - Sets explicit axis labels with units and a title or caption identifying
    the variable and year
  - Chooses a bin count (or rule, e.g. Freedman–Diaconis) that is justified
    in a comment rather than left at the matplotlib default
  - Saves the figure to disk (e.g. via `plt.savefig`) or shows it explicitly
```

Write a self-contained Python script that loads the S&P 500 firm-year panel
from `data/sp500_fundamentals.csv`, filters to the most recent fiscal year,
and produces a labeled histogram of `net_income`. Pick a bin strategy
deliberately (not the matplotlib default) and leave a one-line comment
justifying it. Upload your `.py` file.

## Question q11

```yaml
qid: q11
category: workbook
type: excel
max_attempts: 6
rubric: |
  - A `Summary` sheet (or equivalent) lists each industry with a computed
    mean and median of `return_on_assets` for the most recent fiscal year
  - The mean and median cells use formulas (`AVERAGEIF`, `MEDIAN` with an
    array, or a pivot) — not hard-coded numbers
  - A chart object on the same workbook visualizes those industry means or
    medians (bar / column chart is fine)
  - Axis labels and a chart title identify the metric and units
  - The raw firm-year rows are present on a separate sheet, not deleted
```

Build an `.xlsx` workbook from `data/sp500_fundamentals.csv` that summarizes
mean and median `return_on_assets` by `industry` for the most recent fiscal
year. Use Excel formulas (not Python or hard-coded numbers) to compute the
aggregates, and add a labeled bar or column chart for the industry means.
Upload the `.xlsx` file.

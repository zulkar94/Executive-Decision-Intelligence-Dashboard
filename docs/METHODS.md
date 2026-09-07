# Methods

What each calculation does, what it assumes, and where it stops being trustworthy.

## Normalisation

**Weighted sum** uses min-max scaling per criterion: `(x - min) / (max - min)`, then
`1 - v` for cost criteria. Scores land in [0, 1] and are directly comparable across
criteria measured in dollars, days, and 1-10 judgement scales.

A constant column carries no discriminating information. Rather than divide by zero, it is
mapped to 0.5 for every option, which leaves the ranking unchanged and keeps the
contribution breakdown honest about that criterion's non-contribution.

Min-max is *relative*: adding a fourth option can rescale the other three. This is
inherent to the method, not a defect, but it means scores are comparable within one
analysis and not across two.

**TOPSIS** uses vector normalisation (`x / sqrt(sum(x^2))`), which is scale-invariant and
not affected by the option set in the same way. Distances to the ideal and anti-ideal
points give a closeness coefficient in [0, 1]. When the two methods disagree, the
disagreement itself is information: the ranking depends on the normalisation choice, which
means it is not robust.

## Sensitivity

For each criterion, its weight is swept across [0, 1] on a grid while the remaining
weights keep their relative proportions and absorb the remainder. Two numbers come out:

- **swing** — the range of the leading option's score across the sweep. Large swing means
  the outcome is highly leveraged on that one judgement.
- **flip weight** — the smallest swept weight at which a different option takes the lead,
  or `null` if the incumbent survives the entire sweep.

This is one-at-a-time analysis. It will not find interactions where two weights must move
together to flip the result; Monte Carlo with `weight_jitter` covers that case.

## Monte Carlo

Each cell is drawn from `Normal(value, uncertainty)`, the matrix is re-normalised, and the
options are re-ranked. Repeated a few thousand times, the win frequency approximates the
probability that an option is genuinely best given the stated uncertainty.

With `weight_jitter > 0`, the weight vector is drawn from a Dirichlet distribution
concentrated on the stated weights, where higher jitter means lower concentration and more
dispersion. This models the fact that stakeholder weights are estimates too.

Assumptions worth stating plainly: errors are Normal, symmetric, and independent across
cells. Real estimates are often skewed (costs overrun more often than they underrun) and
correlated (an optimistic estimator is optimistic everywhere). Treat the output as a
robustness check, not a probability you would price an option on.

The seed is returned with every result, so any figure quoted in a board pack can be
reproduced exactly.

## AHP

Weights come from the principal eigenvector of a Saaty pairwise matrix, extracted by power
iteration. The consistency ratio is `(lambda_max - n) / ((n - 1) * RI)` with Saaty's random
index table. Above 0.10, judgements are too self-contradictory to derive weights from, and
the API refuses to apply them rather than returning arithmetic that looks authoritative.

## Forecasting

Holt's linear-trend exponential smoothing. Alpha and beta are chosen by grid search
minimising in-sample SSE — a defensible default that avoids shipping a tuning UI nobody
would use. There is no seasonal term: for weekly or monthly business seasonality, extend
to Holt-Winters.

The prediction band is `z * residual_std * sqrt(1 + cumsum(alpha^2 (1+beta)^2))`, which
widens with horizon as accumulated trend error compounds. It is an approximation, not an
exact predictive distribution, and it assumes the residuals are roughly Normal and
homoscedastic.

Backtesting holds out the tail, refits on the head, and reports MAE, RMSE, and MAPE
out-of-sample. In-sample MAPE flatters every model; the holdout number is the one to read.

## Anomalies

Robust z-scores from the median and MAD, scaled by 1.4826 for normal consistency. Median
and MAD have a 50% breakdown point, so a handful of outliers cannot mask the rest — unlike
mean and standard deviation, which are inflated by the very points being hunted.

When the MAD collapses to zero (a majority-constant series), the scale falls back to the
mean absolute deviation scaled by 1.253314, per Iglewicz and Hoaglin. Falling back to the
standard deviation instead would be inflated by the outlier and hide it — a spike of 9 in
a series of 1s scores 3.1 that way, and 6.4 the correct way.

With `window > 0`, the baseline rolls: each point is compared against the preceding window
only, which tolerates slow drift and reports genuine local excursions. Use the rolling mode
on trending series and the global mode on stationary ones.

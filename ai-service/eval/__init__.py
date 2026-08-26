"""Session 16 — evaluation of the DEPLOYED system (golden set + harness + dashboard).

Not to be confused with the sibling ``evals/`` package, which is older and
measures different things:

* ``evals/``  — Sessions 4, 10 and 11. Runs IN PROCESS against the pipeline, and
  scores the conversational estimator, retrieval quality and RAGAS metrics.
* ``eval/``   — Session 16. Runs OVER HTTP against the deployed AI service and
  scores end-to-end estimate quality, abstention safety, latency and cost.

The two coexist on purpose: one asks "is the pipeline any good", the other asks
"is the thing we deployed any good". They are not the same question.
"""

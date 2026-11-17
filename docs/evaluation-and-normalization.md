# Evaluation metrics and transcript normalization

## Scoring contract

Every run pins an `evaluation_profile_id` and profile version. The score pipeline stores:

1. provider raw transcript and, where allowed, raw structured response;
2. reference text as supplied by the dataset;
3. normalized reference and normalized hypothesis produced by the same profile;
4. per-sample substitution/deletion/insertion counts and token counts;
5. metric implementation/version and profile configuration.

Never overwrite source transcripts with normalized text. A normalization change creates a new metric version and does not rewrite the original run evidence.

## WER and CER

WER = (S + D + I) / N, with S substitutions, D deletions, I insertions, and N reference words. WER may exceed 100%; do not clamp it. Report counts as well as rate. Compute aggregate corpus WER from summed counts (`sum(S+D+I) / sum(N)`), not the unweighted mean of utterance percentages. Also show per-sample distributions and sample count. Use JiWER behind a thin adapter and lock behavior with project-owned examples.

CER is supplementary, not a replacement for WER in English dictation. For languages without whitespace segmentation, select and version a language-aware tokenizer/character metric and clearly label the metric. Avoid comparing WER across languages if tokenization contracts differ.

## Normalization policy

Use one locale-aware, versioned profile for both reference and every provider hypothesis. Initial English profile candidates:

- Unicode normalization (NFKC), case folding, whitespace normalization;
- consistent handling of punctuation and apostrophes;
- explicit treatment of disfluencies (retain by default for dictation fidelity; an optional profile can score them separately);
- carefully specified common number/date/currency verbalization equivalences, enabled only when locale and reference conventions make the mapping unambiguous;
- no provider-specific “forgiving” rewrites.

Maintain at least two views:

- **normalized lexical WER**: primary comparison, with a published policy that removes irrelevant formatting differences;
- **format-sensitive view**: raw text diff and, where feasible, checks for dates, amounts, numbers, capitalization and punctuation important to the product.

For ambiguous forms (`05/06/2026`, `1,234`, currencies, units), do not guess silently. Preserve the raw-form result and either use the dataset's locale/style guide or mark the sample/entity as ambiguous. Numeric semantic equivalence is a separate metric/profile rule, not a hidden provider-specific exception. Any rule that canonicalizes numbers must apply symmetrically to reference and hypotheses and have adversarial test cases.

## Complementary quality metrics

- **CER**: character-level robustness, especially useful for names and scripts without spaces.
- **Entity/critical-term accuracy**: exact or normalized match for names, dates, amounts, addresses, product vocabulary; report precision/recall or exact match with labeled spans.
- **Formatting fidelity**: punctuation/casing/numeric formatting, evaluated separately from lexical correctness.
- **Hallucination/empty-output rate**: non-speech or silence samples that receive invented text, and speech samples returning empty output.
- **Coverage and failure rate**: completed samples / eligible samples; timeouts, provider errors and unusable outputs.
- **Human correction effort (future)**: edits or keystrokes to reach accepted dictation, measured on blinded review; strongest practical complement to WER.
- **Confidence calibration (optional)**: only when providers expose comparable confidence semantics; do not rank unlike confidence scales as if calibrated.

Do not use an LLM judge or cross-provider consensus as ground truth. Without verified references, show latency/cost/availability and disagreement-based review candidates, but label accuracy as unavailable.

## Ranking

Display dimensions separately. Offer a configurable score only as a secondary view with visible weights and normalized component definitions. Prefer Pareto comparison or user-selected weights over a single default “winner.” Always show the actual WER, p50/p95 latency, estimated cost, coverage and uncertainty/sample count alongside any composite score. Provide per-category results so a strong clean-speech score cannot hide noisy-speech failures.

For comparative claims, support paired bootstrap confidence intervals over the same audio samples and flag results whose differences are within measurement uncertainty. This can be a post-MVP enhancement; sample count and intervals should not be fabricated for tiny runs.

"""ATC domain knowledge: lexicon, normalization, alignment, entities, quality flags.

Pure functions only: no knowledge of models, audio, storage or scoring
policy. Ported from v1's validated behaviour (docs/v1-audit), unified into one
module so there is exactly one normalizer per job.
"""

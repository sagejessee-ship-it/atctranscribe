/**
 * Plain-language definitions of the review grid's columns and the agreement and model
 * filters, shown by the (i) buttons. Kept next to the code that computes them:
 * src/aerochorus/atc/agreement.py (groups), atc/align.py (similarity), atc/normalize.py.
 */

export interface ColumnHelp {
  id: string;
  short: string;
  name: string;
  text: string;
}

export function columnHelp(nearThreshold: number | null | undefined): ColumnHelp[] {
  const near = nearThreshold ?? 0.8;
  return [
    {
      id: "results",
      short: "Mdl",
      name: "Models",
      text:
        "How many voting models have a result for this segment, including models that heard no speech (abstained) " +
        "or failed. Models that don't vote are not counted here.",
    },
    {
      id: "exact_providers",
      short: "ExP",
      name: "Exact providers",
      text:
        "The size of the largest group of models that wrote exactly the same words (ignoring capitals and " +
        "punctuation). Two models of the same architecture family count twice.",
    },
    {
      id: "exact_families",
      short: "ExF",
      name: "Exact families",
      text:
        "How many different architecture families are in that largest identical group. Models of one family " +
        "count once, so this is the real measure of independent agreement: 2 or more means unrelated models " +
        "wrote exactly the same words.",
    },
    {
      id: "near_families",
      short: "NrF",
      name: "Near families",
      text:
        `How many families agree approximately: their transcripts match one anchor transcript with a similarity of ` +
        `${near} or more, so small differences (e.g. "niner" vs "nine", a missing word) still count as agreement.`,
    },
    {
      id: "near_similarity",
      short: "NrSim",
      name: "Near similarity",
      text:
        "The weakest similarity inside that near-agreement group, from 0 to 1: the share of words two transcripts " +
        "have in the same order, measured against the longer one. 1.00 means identical words. It is a " +
        "similarity, not a probability of being correct.",
    },
    {
      id: "text",
      short: "Transcript",
      name: "Transcript",
      text:
        "H: a human transcript. M: the models' best guess, which is the exact or near agreement when there is " +
        "one, otherwise the most typical model output. Highlighted words are stretches several families agree on.",
    },
    {
      id: "risk",
      short: "Risk",
      name: "Risk",
      text:
        "E: models that failed. A: models that heard no speech. F: quality flags (e.g. hallucination-like " +
        "output). S: span annotations. U: partial agreement (an utterance agreed by several families " +
        "although the whole segment isn't).",
    },
    {
      id: "review",
      short: "Review",
      name: "Review",
      text: "Whether a person has looked at it: unreviewed, reviewed (accepted as is) or corrected.",
    },
    {
      id: "training",
      short: "Training",
      name: "Training label",
      text:
        "How the segment may be used for training: none, candidate, silver (agreed by 2+ families), gold " +
        "(confirmed by a person) or rejected.",
    },
  ];
}

export const FAMILY_TEXT =
  "An architecture family is a model design, such as whisper, parakeet or canary. Models of one family tend " +
  "to make the same mistakes, so agreement is counted per family, not per model.";

export const VOTING_TEXT =
  "Voting models are the ones agreement is computed from (chosen on the Transcribe page, under model voting). " +
  "Models that don't vote still transcribe, and their results show in the segment's inspector, but they are " +
  "never part of an agreement group.";

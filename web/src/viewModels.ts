/** UI-only view models. Wire DTOs and conversion live in api.ts once the API schema is fixed. */

export type Track = "batch" | "streaming";

export interface DatasetChoice {
  key: string;
  title: string;
  manifestRef: string;
  revision: string | null;
  declaredRevision: string | null;
  revisionStatus: "precomputed" | "computed_on_run";
  revisionNote: string;
  sampleCount: number;
  groundTruthCount: number;
  reproducible: boolean;
  license: string | null;
  language: string | null;
  locale: string | null;
}

export interface ModelChoice {
  key: string;
  title: string;
  tracks: Track[];
}

export interface ProviderChoice {
  key: string;
  title: string;
  configured: boolean;
  models: ModelChoice[];
}

export interface ProviderSelection {
  providerKey: string;
  modelKey: string;
  optionsJson: string;
  timestamps: boolean;
  selected: boolean;
}

export interface ProgressCounts {
  completed: number;
  running: number;
  pending: number;
  retrying: number;
  failed: number;
  skipped: number;
  total: number;
}

export interface LeaderboardRow {
  key: string;
  provider: string;
  model: string;
  wer: number | null;
  cer: number | null;
  latencyP50Ms: number | null;
  latencyP95Ms: number | null;
  ttftP50Ms: number | null;
  ttftP95Ms: number | null;
  finalizationP50Ms: number | null;
  estimatedCostUsd: number | null;
  coverage: number;
  sampleCount: number;
  successCount: number;
  failureCount: number;
}

export interface SampleRow {
  sampleId: string;
  attemptNo: number | null;
  provider: string;
  model: string;
  status: string;
  hasGroundTruth: boolean;
  referenceText: string | null;
  hypothesisText: string | null;
  wer: number | null;
  cer: number | null;
  ttftMs: number | null;
  finalizationMs: number | null;
  totalMs: number | null;
  error: string | null;
}

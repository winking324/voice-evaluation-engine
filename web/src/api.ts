/** Wire DTOs and HTTP adapter for the documented `/api/v1` API. */

export type EvaluationTrack = "batch" | "streaming";
export type RunStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";
export type NormalizationProfileId = "english-lexical" | "raw-text";

export interface DatasetDto {
  dataset_id: string;
  manifest_ref: string;
  declared_revision: string | null;
  resolved_revision: string | null;
  license: string | null;
  language: string | null;
  locale: string | null;
  sample_count: number;
  ground_truth_count: number;
  reproducible: boolean;
}

export interface DatasetCatalogDto {
  items: DatasetDto[];
}

export interface ProviderDto {
  provider_id: string;
  capabilities: EvaluationTrack[];
  configured: boolean;
  default_models: Record<EvaluationTrack, string | null>;
}

export interface ProviderCatalogDto {
  items: ProviderDto[];
}

export interface RunProviderRequestDto {
  provider_id: string;
  model: string;
  options: Record<string, unknown>;
  language?: string | null;
  locale?: string | null;
  timestamps?: boolean;
}

export interface StreamingRunRequestDto {
  sample_rate_hz: number;
  channels: number;
  encoding: string;
  chunk_duration_ms: number;
  completion_timeout_ms: number;
  endpointing_ms: number | null;
  pacing_multiplier: number;
  language?: string | null;
  locale?: string | null;
  interim_results?: boolean;
}

export interface CreateRunRequestDto {
  dataset_manifest: string;
  track: EvaluationTrack;
  sample_limit: number;
  concurrency: number;
  max_attempts: number;
  providers: RunProviderRequestDto[];
  normalization_profile: NormalizationProfileId;
  streaming: StreamingRunRequestDto | null;
}

export interface ProgressDto {
  total_samples: number;
  queued_samples: number;
  running_samples: number;
  retrying_samples: number;
  succeeded_samples: number;
  failed_samples: number;
  cancelled_samples: number;
  interrupted_samples: number;
}

export interface RunProgressDto {
  status: RunStatus;
  progress: ProgressDto;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface LeaderboardDto {
  provider_id: string;
  model: string;
  coverage: number;
  sample_count: number;
  success_count: number;
  failure_count: number;
  wer: number | null;
  cer: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
  post_audio_finalization_p50_ms: number | null;
  estimated_cost_usd: number | null;
}

export interface RunDto {
  run_id: string;
  status: RunStatus;
  track: EvaluationTrack;
  dataset: Record<string, unknown>;
  providers: Array<Record<string, unknown>>;
  progress: ProgressDto;
  leaderboard: LeaderboardDto[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested: boolean;
}

export type RunAcceptedDto = RunDto;

export interface SampleScoreDto {
  wer: number | null;
  cer: number | null;
}

export interface SampleProviderResultDto {
  provider_id: string;
  model: string;
  status: string;
  request_id: string | null;
  transcript: string | null;
  score: SampleScoreDto | null;
  latency_ms: number | null;
  ttft_ms: number | null;
  post_audio_finalization_ms: number | null;
  error: { code: string; message: string } | null;
}

export interface SampleResultDto {
  sample_id: string;
  status: string;
  reference_text: string | null;
  providers: SampleProviderResultDto[];
}

export interface SamplePageDto {
  items: SampleResultDto[];
  limit: number;
  offset: number;
  total: number;
}

export interface ApiOptions {
  signal?: AbortSignal;
}

const API_ROOT = "/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function getDatasets(options?: ApiOptions): Promise<DatasetCatalogDto> {
  return request<DatasetCatalogDto>(`${API_ROOT}/datasets`, options);
}

export async function getProviders(options?: ApiOptions): Promise<ProviderCatalogDto> {
  return request<ProviderCatalogDto>(`${API_ROOT}/providers`, options);
}

export async function createRun(
  payload: CreateRunRequestDto,
  idempotencyKey: string,
): Promise<RunAcceptedDto> {
  return request<RunAcceptedDto>(`${API_ROOT}/runs`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  });
}

export async function getRun(runId: string, options?: ApiOptions): Promise<RunDto> {
  return request<RunDto>(`${API_ROOT}/runs/${encodeURIComponent(runId)}`, options);
}

export async function getRunProgress(
  runId: string,
  options?: ApiOptions,
): Promise<RunProgressDto> {
  return request<RunProgressDto>(
    `${API_ROOT}/runs/${encodeURIComponent(runId)}/progress`,
    options,
  );
}

export async function getRunSamples(
  runId: string,
  limit: number,
  offset: number,
  options?: ApiOptions,
): Promise<SamplePageDto> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<SamplePageDto>(
    `${API_ROOT}/runs/${encodeURIComponent(runId)}/samples?${query}`,
    options,
  );
}

export async function cancelRun(runId: string): Promise<void> {
  await request<unknown>(`${API_ROOT}/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    body: "{}",
  });
}

export async function retryFailed(
  runId: string,
  idempotencyKey: string,
): Promise<RunAcceptedDto> {
  return request<RunAcceptedDto>(
    `${API_ROOT}/runs/${encodeURIComponent(runId)}/retry-failed`,
    { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: "{}" },
  );
}

export async function downloadExport(
  runId: string,
  format: "csv" | "json",
): Promise<void> {
  const response = await fetch(
    `${API_ROOT}/runs/${encodeURIComponent(runId)}/export?format=${format}`,
  );
  if (!response.ok) throw await toApiError(response);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = `voice-evaluation-${runId}.${format}`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown = null;
  let message = `Request failed (${response.status})`;
  try {
    detail = await response.json();
    if (typeof detail === "object" && detail !== null && "detail" in detail) {
      const value = detail.detail;
      message = typeof value === "string" ? value : JSON.stringify(value);
    }
  } catch {
    // Keep the HTTP status as a useful error when the server has no JSON body.
  }
  return new ApiError(message, response.status, detail);
}

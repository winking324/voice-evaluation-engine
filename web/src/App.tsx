import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  cancelRun,
  createRun,
  downloadExport,
  getDatasets,
  getProviders,
  getRun,
  getRunProgress,
  getRunSamples,
  retryFailed,
} from "./api";
import type {
  CreateRunRequestDto,
  DatasetDto,
  LeaderboardDto,
  ProgressDto,
  ProviderDto,
  RunProgressDto,
  RunStatus,
  SampleResultDto,
} from "./api";
import type {
  DatasetChoice,
  LeaderboardRow,
  ModelChoice,
  ProviderChoice,
  ProviderSelection,
  SampleRow,
  Track,
} from "./viewModels";

const SAMPLE_PAGE_SIZE = 50;
const TERMINAL_STATES: RunStatus[] = ["succeeded", "failed", "cancelled", "interrupted"];

function App() {
  const [datasets, setDatasets] = useState<DatasetChoice[]>([]);
  const [providers, setProviders] = useState<ProviderChoice[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [track, setTrack] = useState<Track>("batch");
  const [datasetKey, setDatasetKey] = useState("");
  const [sampleLimit, setSampleLimit] = useState(10);
  const [language, setLanguage] = useState("en");
  const [locale, setLocale] = useState("en-US");
  const [concurrency, setConcurrency] = useState(2);
  const [maxAttempts, setMaxAttempts] = useState(1);
  const [normalizationProfile, setNormalizationProfile] = useState<"english-lexical" | "raw-text">("english-lexical");
  const [sampleRateHz, setSampleRateHz] = useState(24_000);
  const [channels, setChannels] = useState(1);
  const [encoding, setEncoding] = useState("linear16");
  const [chunkMs, setChunkMs] = useState(100);
  const [completionTimeoutMs, setCompletionTimeoutMs] = useState(30_000);
  const [endpointingMs, setEndpointingMs] = useState(500);
  const [pacingMultiplier, setPacingMultiplier] = useState(1);
  const [confirmed, setConfirmed] = useState(false);
  const [selections, setSelections] = useState<ProviderSelection[]>([]);
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(location.search).get("run_id"));
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [runTimes, setRunTimes] = useState<Pick<RunProgressDto, "created_at" | "started_at" | "finished_at"> | null>(null);
  const [runTrack, setRunTrack] = useState<Track | null>(null);
  const [progress, setProgress] = useState<ProgressDto | null>(null);
  const [leaderboard, setLeaderboard] = useState<LeaderboardDto[]>([]);
  const [sampleResults, setSampleResults] = useState<SampleResultDto[]>([]);
  const [sampleOffset, setSampleOffset] = useState(0);
  const [samplesTotal, setSamplesTotal] = useState(0);
  const [runError, setRunError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [exportFormat, setExportFormat] = useState<"csv" | "json">("csv");
  const createIdempotencyRef = useRef<{ payload: string; key: string } | null>(null);
  const retryIdempotencyRef = useRef(new Map<string, string>());

  useEffect(() => {
    const controller = new AbortController();
    setCatalogLoading(true);
    Promise.all([getDatasets({ signal: controller.signal }), getProviders({ signal: controller.signal })])
      .then(([datasetCatalog, providerCatalog]) => {
        setDatasets(datasetCatalog.items.map(toDatasetChoice));
        setProviders(providerCatalog.items.map(toProviderChoice));
        setCatalogError(null);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setCatalogError(errorMessage(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setCatalogLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let inFlight = false;
    const controller = new AbortController();

    const refresh = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        const [run, runProgress, samples] = await Promise.all([
          getRun(runId, { signal: controller.signal }),
          getRunProgress(runId, { signal: controller.signal }),
          getRunSamples(runId, SAMPLE_PAGE_SIZE, sampleOffset, { signal: controller.signal }),
        ]);
        if (cancelled) return;
        setRunStatus(runProgress.status);
        setRunTimes({
          created_at: runProgress.created_at,
          started_at: runProgress.started_at,
          finished_at: runProgress.finished_at,
        });
        setRunTrack(run.track);
        setProgress(runProgress.progress);
        setLeaderboard(run.leaderboard);
        setSampleResults(samples.items);
        setSamplesTotal(samples.total);
        setRunError(null);
      } catch (error) {
        if (!cancelled && !controller.signal.aborted) setRunError(errorMessage(error));
      } finally {
        inFlight = false;
      }
    };

    void refresh();
    const timer = runStatus && TERMINAL_STATES.includes(runStatus)
      ? null
      : window.setInterval(() => void refresh(), 2_000);
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) window.clearInterval(timer);
    };
  }, [runId, sampleOffset, runStatus]);

  const selectedDataset = datasets.find((dataset) => dataset.key === datasetKey);
  const visibleProviders = useMemo(
    () => providers.filter((provider) => provider.models.some((model) => model.tracks.includes(track))),
    [providers, track],
  );
  const selectedProviders = visibleProviders.filter(
    (provider) => provider.configured && selections.find((item) => item.providerKey === provider.key)?.selected,
  );
  const canStart = Boolean(
    !catalogLoading &&
      !catalogError &&
      selectedDataset &&
      sampleLimit > 0 &&
      sampleLimit <= selectedDataset.sampleCount &&
      concurrency > 0 &&
      maxAttempts > 0 &&
      selectedProviders.length >= 2 &&
      (track === "batch" || (
        sampleRateHz > 0 &&
        channels > 0 &&
        encoding.trim().length > 0 &&
        chunkMs > 0 &&
        completionTimeoutMs > 0 &&
        pacingMultiplier > 0
      )) &&
      confirmed,
  );

  const updateSelection = (provider: ProviderChoice, patch: Partial<ProviderSelection>) => {
    setSelections((current) => {
      const existing = current.find((selection) => selection.providerKey === provider.key);
      if (existing) {
        return current.map((selection) =>
          selection.providerKey === provider.key ? { ...selection, ...patch } : selection,
        );
      }
      const firstModel = provider.models.find((model) => model.tracks.includes(track));
      return [
        ...current,
        {
          providerKey: provider.key,
          modelKey: firstModel?.key ?? "",
          optionsJson: "{}",
          timestamps: false,
          selected: false,
          ...patch,
        },
      ];
    });
  };

  const selectionFor = (provider: ProviderChoice): ProviderSelection => {
    const existing = selections.find((selection) => selection.providerKey === provider.key);
    const model = provider.models.find((item) => item.key === existing?.modelKey && item.tracks.includes(track))
      ?? provider.models.find((item) => item.tracks.includes(track));
    return existing
      ? { ...existing, modelKey: model?.key ?? "" }
      : {
          providerKey: provider.key,
          modelKey: model?.key ?? "",
          optionsJson: "{}",
          timestamps: false,
          selected: false,
        };
  };

  const selectDataset = (key: string) => {
    setDatasetKey(key);
    const dataset = datasets.find((item) => item.key === key);
    if (dataset?.language) setLanguage(dataset.language);
    if (dataset?.locale) setLocale(dataset.locale);
    if (dataset?.sampleCount) setSampleLimit(Math.min(sampleLimit, dataset.sampleCount));
  };

  const submitRun = async () => {
    setActionError(null);
    setActionBusy(true);
    try {
      const providerRequests = selectedProviders.map((provider) => {
        const selection = selectionFor(provider);
        const options: unknown = JSON.parse(selection.optionsJson);
        if (typeof options !== "object" || options === null || Array.isArray(options)) {
          throw new Error(`Options for ${provider.title} must be a JSON object.`);
        }
        return {
          provider_id: provider.key,
          model: selection.modelKey,
          options: options as Record<string, unknown>,
          language: language.trim() || null,
          locale: locale.trim() || null,
          timestamps: selection.timestamps,
        };
      });
      if (!selectedDataset || providerRequests.length < 2) {
        throw new Error("Choose a dataset and at least two configured providers.");
      }
      const payload: CreateRunRequestDto = {
        dataset_manifest: selectedDataset.manifestRef,
        track,
        sample_limit: sampleLimit,
        concurrency,
        max_attempts: maxAttempts,
        providers: providerRequests,
        normalization_profile: normalizationProfile,
        streaming: track === "streaming" ? {
          sample_rate_hz: sampleRateHz,
          channels,
          encoding: encoding.trim(),
          chunk_duration_ms: chunkMs,
          completion_timeout_ms: completionTimeoutMs,
          endpointing_ms: endpointingMs > 0 ? endpointingMs : null,
          pacing_multiplier: pacingMultiplier,
        } : null,
      };
      const serializedPayload = JSON.stringify(payload);
      if (createIdempotencyRef.current?.payload !== serializedPayload) {
        createIdempotencyRef.current = { payload: serializedPayload, key: crypto.randomUUID() };
      }
      const accepted = await createRun(payload, createIdempotencyRef.current.key);
      createIdempotencyRef.current = null;
      setRunId(accepted.run_id);
      setRunStatus(accepted.status);
      setRunTrack(track);
      setSampleOffset(0);
      setLeaderboard([]);
      setSampleResults([]);
      setRunError(null);
      updateRunLocation(accepted.run_id);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setActionBusy(false);
    }
  };

  const handleCancel = async () => {
    if (!runId) return;
    setActionBusy(true);
    setActionError(null);
    try {
      await cancelRun(runId);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setActionBusy(false);
    }
  };

  const handleRetry = async () => {
    if (!runId) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const key = retryIdempotencyRef.current.get(runId) ?? crypto.randomUUID();
      retryIdempotencyRef.current.set(runId, key);
      const accepted = await retryFailed(runId, key);
      retryIdempotencyRef.current.delete(runId);
      setRunId(accepted.run_id);
      setRunStatus(accepted.status);
      setRunTrack(accepted.track);
      setSampleOffset(0);
      setLeaderboard([]);
      setSampleResults([]);
      setRunError(null);
      updateRunLocation(accepted.run_id);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setActionBusy(false);
    }
  };

  const handleExport = async () => {
    if (!runId) return;
    setActionBusy(true);
    setActionError(null);
    try {
      await downloadExport(runId, exportFormat);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setActionBusy(false);
    }
  };

  const batchRows = runTrack === "batch" ? leaderboard.map(toLeaderboardRow) : [];
  const streamRows = runTrack === "streaming" ? leaderboard.map(toLeaderboardRow) : [];
  const sampleRows = flattenSamples(sampleResults);
  const counts = progress;
  const isActive = runStatus === "queued" || runStatus === "running" || runStatus === "cancelling";
  const resolvedSamples = counts
    ? counts.succeeded_samples + counts.failed_samples + counts.cancelled_samples + counts.interrupted_samples
    : 0;
  const runElapsed = runTimes
    ? elapsedMs(runTimes.started_at ?? runTimes.created_at, runTimes.finished_at)
    : null;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="Voice Evaluation Engine home">
          <span className="brand-mark" aria-hidden="true">V</span>
          <span>Voice Evaluation Engine</span>
        </a>
        <div className="topbar-status"><span className={`status-dot ${catalogError ? "status-dot-error" : ""}`} /> Personal workspace</div>
      </header>

      <div className="page-content" id="top">
        <section className="hero">
          <div>
            <p className="eyebrow">SPEECH-TO-TEXT BENCHMARK</p>
            <h1>Find the right voice for your words.</h1>
            <p className="hero-copy">
              Run the same audio through multiple transcription models. Compare accuracy,
              response time, and cost with the full sample-level evidence.
            </p>
          </div>
          <div className="hero-badge"><span aria-hidden="true">◉</span> Same audio. Clear comparisons.</div>
        </section>

        {catalogError && (
          <div className="notice notice-warning" role="alert">
            <strong>Could not load evaluation options</strong>
            <span>{catalogError} Confirm that this page is served from the app and that `/api/v1/datasets` and `/api/v1/providers` are available.</span>
          </div>
        )}

        {actionError && <div className="notice notice-error" role="alert"><strong>Request failed</strong><span>{actionError}</span></div>}
        {runError && <div className="notice notice-error" role="alert"><strong>Run refresh failed</strong><span>{runError}</span></div>}

        <form className="workspace-grid" onSubmit={(event) => { event.preventDefault(); void submitRun(); }}>
          <section className="panel setup-panel" aria-labelledby="setup-title">
            <div className="panel-heading">
              <div><p className="eyebrow">CONFIGURATION</p><h2 id="setup-title">New evaluation</h2></div>
              <span className="step-badge">01</span>
            </div>

            <fieldset className="track-picker">
              <legend>Evaluation track</legend>
              <label className={`track-option ${track === "batch" ? "is-selected" : ""}`}>
                <input type="radio" name="track" value="batch" checked={track === "batch"} onChange={() => setTrack("batch")} />
                <span className="track-icon" aria-hidden="true">▣</span>
                <span><strong>Batch transcription</strong><small>Upload audio files and compare complete transcripts.</small></span>
              </label>
              <label className={`track-option ${track === "streaming" ? "is-selected" : ""}`}>
                <input type="radio" name="track" value="streaming" checked={track === "streaming"} onChange={() => setTrack("streaming")} />
                <span className="track-icon" aria-hidden="true">◉</span>
                <span><strong>Streaming replay</strong><small>Replay recorded audio at a controlled pace.</small></span>
              </label>
            </fieldset>

            <div className="form-grid">
              <label className="field field-wide">
                <span>Dataset</span>
                <select value={datasetKey} onChange={(event) => selectDataset(event.target.value)} disabled={catalogLoading || !datasets.length}>
                  <option value="">{catalogLoading ? "Loading datasets…" : catalogError ? "Dataset catalog unavailable" : datasets.length ? "Choose a dataset" : "No dataset available"}</option>
                  {datasets.map((dataset) => <option key={dataset.key} value={dataset.key}>{dataset.title}</option>)}
                </select>
                <small className="field-help">
                  {selectedDataset
                    ? `${selectedDataset.sampleCount.toLocaleString()} samples · ${selectedDataset.groundTruthCount.toLocaleString()} with references · ${selectedDataset.locale ?? selectedDataset.language ?? "language not set"} · ${selectedDataset.license ?? "license not specified"} · revision ${selectedDataset.revision ?? selectedDataset.declaredRevision ?? "computed at run creation"} · ${selectedDataset.revisionStatus === "computed_on_run" ? selectedDataset.revisionNote : selectedDataset.reproducible ? "reproducible" : "not pinned"}`
                    : "The manifest reference and resolved revision are snapshotted with the run."}
                </small>
              </label>
              <label className="field">
                <span>Sample limit</span>
                <input type="number" min={1} max={selectedDataset?.sampleCount ?? undefined} required value={sampleLimit} onChange={(event) => setSampleLimit(Number(event.target.value))} />
                <small className="field-help">{selectedDataset ? `${selectedDataset.groundTruthCount.toLocaleString()} samples have reference text` : "Use a small limit for a smoke run"}</small>
              </label>
              <label className="field">
                <span>Normalization profile</span>
                <select value={normalizationProfile} onChange={(event) => setNormalizationProfile(event.target.value as "english-lexical" | "raw-text")}>
                  <option value="english-lexical">English lexical · v1</option><option value="raw-text">Raw text · v1</option>
                </select>
              </label>
              <label className="field">
                <span>Language</span>
                <input value={language} onChange={(event) => setLanguage(event.target.value)} placeholder="en" />
                <small className="field-help">Passed to each provider configuration</small>
              </label>
              <label className="field">
                <span>Locale</span>
                <input value={locale} onChange={(event) => setLocale(event.target.value)} placeholder="en-US" />
              </label>
              <label className="field">
                <span>Concurrency</span>
                <input type="number" min={1} max={64} required value={concurrency} onChange={(event) => setConcurrency(Number(event.target.value))} />
              </label>
              <label className="field">
                <span>Maximum attempts</span>
                <input type="number" min={1} max={5} required value={maxAttempts} onChange={(event) => setMaxAttempts(Number(event.target.value))} />
                <small className="field-help">Includes the initial request.</small>
              </label>
              {track === "streaming" && <>
                <label className="field">
                  <span>Sample rate</span>
                  <div className="input-with-unit"><input type="number" min={8000} max={96000} step={1000} required value={sampleRateHz} onChange={(event) => setSampleRateHz(Number(event.target.value))} /><span>Hz</span></div>
                </label>
                <label className="field">
                  <span>Channels</span>
                  <input type="number" min={1} max={2} required value={channels} onChange={(event) => setChannels(Number(event.target.value))} />
                </label>
                <label className="field">
                  <span>Encoding</span>
                  <input value={encoding} required onChange={(event) => setEncoding(event.target.value)} />
                </label>
                <label className="field">
                  <span>Chunk size</span>
                  <div className="input-with-unit"><input type="number" min={20} max={1000} step={10} required value={chunkMs} onChange={(event) => setChunkMs(Number(event.target.value))} /><span>ms</span></div>
                </label>
                <label className="field">
                  <span>Replay pace</span>
                  <div className="input-with-unit"><input type="number" min={0.01} step="any" required value={pacingMultiplier} onChange={(event) => setPacingMultiplier(Number(event.target.value))} /><span>× real time</span></div>
                </label>
                <label className="field">
                  <span>Completion timeout</span>
                  <div className="input-with-unit"><input type="number" min={1000} max={120000} step={1000} required value={completionTimeoutMs} onChange={(event) => setCompletionTimeoutMs(Number(event.target.value))} /><span>ms</span></div>
                </label>
                <label className="field">
                  <span>Endpointing</span>
                  <div className="input-with-unit"><input type="number" min={0} max={5000} step={50} value={endpointingMs} onChange={(event) => setEndpointingMs(Number(event.target.value))} /><span>ms · 0 off</span></div>
                </label>
              </>}
            </div>

            <div className="section-divider" />
            <div className="subsection-heading">
              <div><h3>Providers &amp; models</h3><p>Choose at least two configured providers for a comparison.</p></div>
              <span className="selection-count">{selectedProviders.length} selected</span>
            </div>
            {visibleProviders.length === 0 ? (
              <div className="empty-state compact">
                <div className="empty-icon" aria-hidden="true">⌁</div>
                <strong>{catalogLoading ? "Loading provider catalog…" : catalogError ? "Provider catalog unavailable" : "No providers support this track"}</strong>
                <span>Track eligibility and default models come from <code>GET /api/v1/providers</code>.</span>
              </div>
            ) : (
              <div className="provider-list">
                {visibleProviders.map((provider) => {
                  const selection = selectionFor(provider);
                  const models = availableModels(provider, track);
                  return <article className={`provider-card ${selection.selected ? "provider-card-selected" : ""}`} key={provider.key}>
                    <div className="provider-card-head">
                      <label className="provider-title">
                        <input type="checkbox" checked={selection.selected} disabled={!provider.configured || !selection.modelKey} onChange={(event) => updateSelection(provider, { selected: event.target.checked })} />
                        <span className="provider-avatar">{provider.title.slice(0, 1).toUpperCase()}</span>
                        <strong>{provider.title}</strong>
                      </label>
                      <span className={`configured-pill ${provider.configured ? "configured" : "not-configured"}`}>{provider.configured ? "Configured" : "Credential missing"}</span>
                    </div>
                    <label className="field">
                      <span>Model</span>
                      <select value={selection.modelKey} disabled={!models.length} onChange={(event) => updateSelection(provider, { modelKey: event.target.value })}>
                        {models.map((model) => <option key={model.key} value={model.key}>{model.title}</option>)}
                      </select>
                    </label>
                    <label className="field options-field">
                      <span>Provider options <small>(JSON object)</small></span>
                      <textarea rows={2} value={selection.optionsJson} onChange={(event) => updateSelection(provider, { optionsJson: event.target.value })} spellCheck={false} aria-label={`${provider.title} provider options as JSON`} />
                    </label>
                    <label className="checkbox-line provider-timestamps"><input type="checkbox" checked={selection.timestamps} onChange={(event) => updateSelection(provider, { timestamps: event.target.checked })} /><span>Request timestamps when supported</span></label>
                  </article>;
                })}
              </div>
            )}

            <div className="section-divider" />
            <div className="consent-card">
              <label className="checkbox-line">
                <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
                <span>I have permission to send this audio to the selected providers and understand API usage may incur charges.</span>
              </label>
              <p>Provider requests are sent only after you select Start evaluation. This page does not capture microphone audio.</p>
            </div>
            <button className="button button-primary button-large" type="submit" disabled={!canStart || actionBusy}>
              <span aria-hidden="true">▶</span> {actionBusy ? "Submitting…" : "Start evaluation"}
            </button>
            {!catalogError && !catalogLoading && selectedProviders.length < 2 && <p className="form-hint">Select two configured providers to enable a comparison.</p>}
          </section>

          <div className="results-column">
            <section className="panel progress-panel" aria-labelledby="progress-title">
              <div className="panel-heading">
                <div><p className="eyebrow">RUN MONITOR</p><h2 id="progress-title">Progress</h2></div>
                {runStatus && <span className={`state-pill state-${runStatus}`}>{runStatus}</span>}
              </div>
              {runId ? <>
                <div className="run-id">Run <code>{runId}</code></div>
                <div className="progress-track" role="progressbar" aria-label="Resolved sample progress" aria-valuemin={0} aria-valuemax={counts?.total_samples ?? 0} aria-valuenow={resolvedSamples}><div className="progress-fill" style={{ width: `${counts?.total_samples ? (resolvedSamples / counts.total_samples) * 100 : 0}%` }} /></div>
                <div className="progress-numbers"><strong>{resolvedSamples.toLocaleString()}<span> / {counts?.total_samples.toLocaleString() ?? "—"}</span></strong><span>samples resolved</span></div>
                {runElapsed !== null && <p className="elapsed-time">Elapsed · {formatDuration(runElapsed)}</p>}
                <div className="count-grid">
                  <Count label="Succeeded" value={counts?.succeeded_samples ?? 0} tone="green" />
                  <Count label="Running" value={counts?.running_samples ?? 0} tone="blue" />
                  <Count label="Queued" value={counts?.queued_samples ?? 0} tone="muted" />
                  <Count label="Retrying" value={counts?.retrying_samples ?? 0} tone="amber" />
                  <Count label="Failed" value={counts?.failed_samples ?? 0} tone="red" />
                  <Count label="Cancelled / interrupted" value={(counts?.cancelled_samples ?? 0) + (counts?.interrupted_samples ?? 0)} tone="muted" />
                </div>
                <div className="run-actions">
                  <button type="button" className="button button-secondary" disabled={!isActive || actionBusy} onClick={() => void handleCancel()}>Cancel run</button>
                  {runStatus && TERMINAL_STATES.includes(runStatus) && (counts?.failed_samples ?? 0) > 0 && <button type="button" className="button button-secondary" disabled={actionBusy} onClick={() => void handleRetry()}>Retry failed</button>}
                </div>
              </> : <div className="empty-state">
                <div className="empty-icon" aria-hidden="true">⌁</div>
                <strong>Your run will appear here</strong>
                <span>Set up a comparison and start it when you're ready.</span>
              </div>}
            </section>

            <section className="panel leaderboard-panel" aria-labelledby="leaderboard-title">
              <div className="panel-heading">
                <div><p className="eyebrow">COMPARISON</p><h2 id="leaderboard-title">Leaderboard</h2></div>
                <div className="export-control">
                  <label className="sr-only" htmlFor="export-format">Export format</label>
                  <select id="export-format" value={exportFormat} onChange={(event) => setExportFormat(event.target.value as "csv" | "json")}>
                    <option value="csv">CSV</option><option value="json">JSON</option>
                  </select>
                  <button type="button" className="button button-secondary" disabled={!runId || actionBusy} onClick={() => void handleExport()}>Export</button>
                </div>
              </div>
              <div className="track-results">
                <Leaderboard title="Batch results" rows={batchRows} track="batch" activeTrack={runTrack} />
                <Leaderboard title="Streaming results" rows={streamRows} track="streaming" activeTrack={runTrack} />
              </div>
              <p className="table-note">Scores depend on the dataset revision, scoring profile, and sample coverage. Batch TTFT is unavailable for complete-response APIs; unknown costs stay unavailable.</p>
            </section>

            <section className="panel samples-panel" aria-labelledby="samples-title">
              <div className="panel-heading">
                <div><p className="eyebrow">INSPECT</p><h2 id="samples-title">Sample results</h2></div>
                <span className="selection-count">{samplesTotal.toLocaleString()} samples</span>
              </div>
              {sampleRows.length ? <>
                <SampleTable rows={sampleRows} />
                <div className="pagination">
                  <span>{sampleOffset + 1}–{Math.min(sampleOffset + sampleRows.length, samplesTotal)} of {samplesTotal}</span>
                  <div><button type="button" className="button button-secondary" disabled={sampleOffset === 0} onClick={() => setSampleOffset(Math.max(0, sampleOffset - SAMPLE_PAGE_SIZE))}>Previous</button><button type="button" className="button button-secondary" disabled={sampleOffset + SAMPLE_PAGE_SIZE >= samplesTotal} onClick={() => setSampleOffset(sampleOffset + SAMPLE_PAGE_SIZE)}>Next</button></div>
                </div>
              </> : <div className="empty-state compact">
                <div className="empty-icon" aria-hidden="true">⌕</div>
                <strong>No sample results yet</strong>
                <span>Transcript detail and ground-truth availability will appear after a run.</span>
              </div>}
            </section>
          </div>
        </form>
        <footer className="page-footer"><span>Voice Evaluation Engine</span><span>English dictation · private workspace</span></footer>
      </div>
    </main>
  );
}

function availableModels(provider: ProviderChoice, track: Track): ModelChoice[] {
  return provider.models.filter((model) => model.tracks.includes(track));
}

function toDatasetChoice(dataset: DatasetDto): DatasetChoice {
  return {
    key: dataset.manifest_ref,
    title: `${dataset.dataset_id} · ${dataset.manifest_ref}`,
    manifestRef: dataset.manifest_ref,
    revision: dataset.resolved_revision,
    declaredRevision: dataset.declared_revision,
    revisionStatus: dataset.revision_status,
    revisionNote: dataset.revision_note,
    sampleCount: dataset.sample_count,
    groundTruthCount: dataset.ground_truth_count,
    reproducible: dataset.reproducible,
    license: dataset.license,
    language: dataset.language,
    locale: dataset.locale,
  };
}

function toProviderChoice(provider: ProviderDto): ProviderChoice {
  const tracks: Track[] = ["batch", "streaming"];
  const models: ModelChoice[] = tracks
    .filter((track) => provider.capabilities.includes(track) && provider.default_models[track])
    .map((track) => ({ key: provider.default_models[track]!, title: provider.default_models[track]!, tracks: [track] }));
  return { key: provider.provider_id, title: provider.provider_id, configured: provider.configured, models };
}

function toLeaderboardRow(row: LeaderboardDto): LeaderboardRow {
  return {
    key: `${row.provider_id}/${row.model}`,
    provider: row.provider_id,
    model: row.model,
    wer: row.wer,
    cer: row.cer,
    latencyP50Ms: row.latency_p50_ms,
    latencyP95Ms: row.latency_p95_ms,
    ttftP50Ms: row.ttft_p50_ms,
    ttftP95Ms: row.ttft_p95_ms,
    finalizationP50Ms: row.post_audio_finalization_p50_ms,
    estimatedCostUsd: row.estimated_cost_usd,
    coverage: row.coverage,
    sampleCount: row.sample_count,
    successCount: row.success_count,
    failureCount: row.failure_count,
  };
}

function flattenSamples(samples: SampleResultDto[]): SampleRow[] {
  const rows: SampleRow[] = [];
  for (const sample of samples) {
    if (sample.providers.length === 0) {
      rows.push({
        sampleId: sample.sample_id,
        attemptNo: null,
        provider: "—",
        model: "—",
        status: sample.status,
        hasGroundTruth: sample.reference_text !== null,
        referenceText: sample.reference_text,
        hypothesisText: null,
        wer: null,
        cer: null,
        ttftMs: null,
        totalMs: null,
        finalizationMs: null,
        error: null,
      });
      continue;
    }
    for (const provider of sample.providers) {
      rows.push({
        sampleId: sample.sample_id,
        attemptNo: provider.attempt_no,
        provider: provider.provider_id,
        model: provider.model,
        status: provider.status,
        hasGroundTruth: sample.reference_text !== null,
        referenceText: sample.reference_text,
        hypothesisText: provider.transcript,
        wer: provider.score?.wer ?? null,
        cer: provider.score?.cer ?? null,
        ttftMs: provider.ttft_ms,
        totalMs: provider.latency_ms,
        finalizationMs: provider.post_audio_finalization_ms,
        error: provider.error ? `${provider.error.code}: ${provider.error.message}` : null,
      });
    }
  }
  return rows;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return `The run queue is at capacity. Try again when capacity is available. ${error.message}`;
    return error.message;
  }
  return error instanceof Error ? error.message : "An unexpected error occurred.";
}

function updateRunLocation(runId: string): void {
  const url = new URL(window.location.href);
  url.searchParams.set("run_id", runId);
  window.history.replaceState({}, "", url);
}

function elapsedMs(startedAt: string, finishedAt: string | null): number {
  const start = Date.parse(startedAt);
  const end = finishedAt === null ? Date.now() : Date.parse(finishedAt);
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : 0;
}

function formatDuration(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return minutes ? `${minutes}m ${remainingSeconds}s` : `${remainingSeconds}s`;
}

function Count({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className="count-item"><span className={`count-indicator ${tone}`} /><span>{label}</span><strong>{value}</strong></div>;
}

function Leaderboard({
  title,
  rows,
  track,
  activeTrack,
}: {
  title: string;
  rows: LeaderboardRow[];
  track: Track;
  activeTrack: Track | null;
}) {
  return <div className="leaderboard-track">
    <h3>{title}</h3>
    <div className="table-scroll"><table>
      <thead><tr><th>Provider / model</th><th>WER</th><th>CER</th><th>TTFT p50 / p95</th>{track === "streaming" && <th>Finalize p50</th>}<th>Total p50 / p95</th><th>Est. cost</th><th>Coverage</th><th>Failed</th></tr></thead>
      <tbody>{rows.length ? rows.map((row) => <tr key={row.key}>
        <td><strong>{row.provider}</strong><small>{row.model}</small></td>
        <td>{formatRate(row.wer)}</td><td>{formatRate(row.cer)}</td>
        <td>{formatPair(row.ttftP50Ms, row.ttftP95Ms)}</td>{track === "streaming" && <td>{formatMs(row.finalizationP50Ms)}</td>}
        <td>{formatPair(row.latencyP50Ms, row.latencyP95Ms)}</td>
        <td>{formatMoney(row.estimatedCostUsd)}</td>
        <td>{formatRate(row.coverage)}<small>{row.successCount} / {row.sampleCount} samples</small></td><td>{row.failureCount}</td>
      </tr>) : <tr><td colSpan={track === "streaming" ? 9 : 8} className="empty-cell">{activeTrack && activeTrack !== track ? `This run uses ${activeTrack}; no ${track} results.` : "Waiting for evaluation results"}</td></tr>}</tbody>
    </table></div>
  </div>;
}

function SampleTable({ rows }: { rows: SampleRow[] }) {
  return <div className="table-scroll sample-scroll"><table>
    <thead><tr><th>Sample</th><th>Provider / model</th><th>Attempt</th><th>Status</th><th>Reference / transcript</th><th>WER</th><th>CER</th><th>TTFT</th><th>Finalize</th><th>Total</th></tr></thead>
    <tbody>{rows.map((row) => <tr key={`${row.sampleId}-${row.provider}-${row.attemptNo ?? "none"}`}>
      <td><code>{row.sampleId}</code></td><td><strong>{row.provider}</strong><small>{row.model}</small></td><td>{row.attemptNo === null ? "—" : `Attempt ${row.attemptNo}`}</td><td><span className="sample-status">{row.status}</span></td>
      <td className="transcript-cell">
        {row.hasGroundTruth ? <><span className="transcript-label">Reference</span><p>{row.referenceText ?? ""}</p></> : <span className="no-ground-truth">Ground truth unavailable</span>}
        {row.hypothesisText !== null && <><span className="transcript-label">Transcript</span><p>{row.hypothesisText}</p></>}
        {row.error && <span className="sample-error">{row.error}</span>}
      </td><td>{formatRate(row.wer)}</td><td>{formatRate(row.cer)}</td><td>{formatMs(row.ttftMs)}</td><td>{formatMs(row.finalizationMs)}</td><td>{formatMs(row.totalMs)}</td>
    </tr>)}</tbody>
  </table></div>;
}

function formatRate(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatMs(value: number | null): string {
  return value === null ? "—" : `${Math.round(value).toLocaleString()} ms`;
}

function formatPair(p50: number | null, p95: number | null): string {
  return p50 === null && p95 === null ? "—" : `${formatMs(p50)} / ${formatMs(p95)}`;
}

function formatMoney(value: number | null): string {
  return value === null ? "Unavailable" : `$${value.toFixed(4)}`;
}

export default App;

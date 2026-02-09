import React, { useMemo, useRef, useState, useEffect } from "react";

const API_BASE = "http://127.0.0.1:8000";

type ElementItem = {
  id: number;
  type: string;
  content: string;
  center: [number, number];
  bbox: [number, number, number, number];
};

type StepResponse = {
  action: string;
  target_id?: number | null;
  target_point?: [number, number] | null;
  action_tool?: string | null;
  action_text?: string | null;
  action_key?: string | null;
  action_ms?: number | null;
  action_url?: string | null;
  action_scroll?: number | null;
  reason?: string;
  annotated_image_base64?: string;
  elements?: ElementItem[];
  current_url?: string | null;
  planner_debug?: Record<string, unknown> | null;
  finish_debug?: Record<string, unknown> | null;
  // v2.2: VLM 对话详情
  vlm_conversation?: {
    request: {
      task: string;
      elements_count: number;
      elements: Array<{
        id: number;
        type: string;
        content: string;
      }>;
      image_size: [number, number];
      annotated_image: string;
    };
    response: Record<string, unknown>;
    response_raw: string;
  } | null;
};

type PlanStepsResponse = {
  steps: string[];
  debug?: Record<string, unknown> | null;
};

type TaskSpecResponse = {
  data: Record<string, unknown>;
  debug?: Record<string, unknown> | null;
};

type FileListResponse = {
  files: string[];
};

type RunExtractionResponse = {
  status: string;
  items_extracted: number;
  target_count: number;
  file_path: string | null;
  progress: Array<Record<string, unknown>>;
  errors: string[];
  items: Array<Record<string, unknown>>;
};

function App() {
  const [task, setTask] = useState("在搜索框中输入bilibili并回车，点击进入bilibili这个网站");
  const [elements, setElements] = useState<ElementItem[]>([]);
  const [annotated, setAnnotated] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [currentUrl, setCurrentUrl] = useState<string>("");
  const [hoverId, setHoverId] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [planSteps, setPlanSteps] = useState<string[]>([]);
  const [currentStepIndex, setCurrentStepIndex] = useState<number>(-1);
  const [planDebug, setPlanDebug] = useState<Record<string, unknown> | null>(null);
  const [stepDebug, setStepDebug] = useState<Record<string, unknown> | null>(null);
  const [finishDebug, setFinishDebug] = useState<Record<string, unknown> | null>(null);
  const [taskSpec, setTaskSpec] = useState<Record<string, unknown> | null>(null);
  const [taskSpecDebug, setTaskSpecDebug] = useState<Record<string, unknown> | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [lastTargetPoint, setLastTargetPoint] = useState<[number, number] | null>(null);
  const [extractionResult, setExtractionResult] = useState<RunExtractionResponse | null>(null);
  const [extractionLoading, setExtractionLoading] = useState(false);
  const [maxItems, setMaxItems] = useState<number>(10);
  const [useOmniparser, setUseOmniparser] = useState<boolean>(true);
  const [listOnly, setListOnly] = useState<boolean>(false);
  const [extractionProgress, setExtractionProgress] = useState<Array<Record<string, unknown>>>([]);
  const [vlmConversation, setVlmConversation] = useState<StepResponse['vlm_conversation']>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const progressIntervalRef = useRef<number | null>(null);

  const imgSrc = useMemo(() => {
    if (!annotated) return null;
    return `data:image/png;base64,${annotated}`;
  }, [annotated]);

  const handleRun = async () => {
    setLoading(true);
    setStatus("planning...");
    setSelectedId(null);
    setPlanSteps([]);
    setCurrentStepIndex(-1);
    setPlanDebug(null);
    setStepDebug(null);
    setFinishDebug(null);
    try {
      const resp = await fetch(`${API_BASE}/plan_steps`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task, max_steps: 6 }),
      });
      const data: PlanStepsResponse = await resp.json();
      setPlanSteps(data.steps || []);
      setPlanDebug(data.debug || null);
      setStatus(`planned ${data.steps?.length || 0} steps`);
    } catch (err) {
      setStatus(`error: ${String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleNextStep = async () => {
    if (loading) return;
    const nextIndex = currentStepIndex + 1;
    if (nextIndex >= planSteps.length) return;

    setLoading(true);
    setStatus(`running step ${nextIndex + 1}/${planSteps.length}...`);
    setSelectedId(null);
    setStepDebug(null);
    setFinishDebug(null);
    setCurrentStepIndex(nextIndex);
    try {
      const stepTask = planSteps[nextIndex];
      const planContext = planSteps.map((s, idx) => `${idx + 1}. ${s}`).join("\n");
      const taskWithContext = `Current step (${nextIndex + 1}/${planSteps.length}): ${stepTask}`;
      const resp = await fetch(`${API_BASE}/step_once`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: taskWithContext, plan_context: planContext }),
      });
      const data: StepResponse = await resp.json();
      setAnnotated(data.annotated_image_base64 || null);
      setElements(data.elements || []);
      setCurrentUrl(data.current_url || "");
      setLastTargetPoint(data.target_point || null);
      setStepDebug({
        ...(data.planner_debug || {}),
        target_point: data.target_point || null,
        action_tool: data.action_tool || null,
        action_text: data.action_text || null,
        action_key: data.action_key || null,
        action_ms: data.action_ms || null,
        action_url: data.action_url || null,
        action_scroll: data.action_scroll || null,
      });
      setFinishDebug(data.finish_debug || null);
      setVlmConversation(data.vlm_conversation || null);
      setStatus(`done (${data.reason || data.action || ""})`);
    } catch (err) {
      setStatus(`error: ${String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleManualClick = async (ev: React.MouseEvent<HTMLDivElement>) => {
    if (!imgRef.current || !annotated) return;
    const rect = imgRef.current.getBoundingClientRect();
    const x = Math.round(((ev.clientX - rect.left) / rect.width) * imgRef.current.naturalWidth);
    const y = Math.round(((ev.clientY - rect.top) / rect.height) * imgRef.current.naturalHeight);
    const closest = findClosestElement([x, y], elements);
    setSelectedId(closest?.id ?? null);

    setLoading(true);
    setStatus("manual click...");
    try {
      const resp = await fetch(`${API_BASE}/step`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task, override_point: [x, y] }),
      });
      const data: StepResponse = await resp.json();
      setAnnotated(data.annotated_image_base64 || null);
      setElements(data.elements || []);
      setCurrentUrl(data.current_url || "");
      setStatus(`done (${data.reason || ""})`);
    } catch (err) {
      setStatus(`error: ${String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleTaskSpec = async () => {
    setLoading(true);
    setStatus("task spec...");
    setTaskSpec(null);
    setTaskSpecDebug(null);
    try {
      const resp = await fetch(`${API_BASE}/task_spec`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task }),
      });
      const data: TaskSpecResponse = await resp.json();
      setTaskSpec(data.data || null);
      setTaskSpecDebug(data.debug || null);
      setStatus("task spec ready");
    } catch (err) {
      setStatus(`error: ${String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleRefreshFiles = async () => {
    try {
      const resp = await fetch(`${API_BASE}/files`);
      const data: FileListResponse = await resp.json();
      setFiles(data.files || []);
    } catch (err) {
      setStatus(`error: ${String(err)}`);
    }
  };

  const handleRunExtraction = async () => {
    setExtractionLoading(true);
    setStatus("running extraction...");
    setExtractionResult(null);
    setExtractionProgress([]);

    // 启动进度轮询
    progressIntervalRef.current = window.setInterval(async () => {
      try {
        const resp = await fetch(`${API_BASE}/extraction_progress`);
        const data = await resp.json();
        if (data.progress && data.progress.length > 0) {
          setExtractionProgress(data.progress);

          // 更新状态显示
          const lastProgress = data.progress[data.progress.length - 1];
          const stage = lastProgress.stage || "unknown";
          const currentAction = lastProgress.current_action || "";
          const processed = lastProgress.processed || 0;
          const total = lastProgress.total || 0;

          if (currentAction) {
            setStatus(`${stage}: ${currentAction} (${processed}/${total})`);
          } else {
            setStatus(`${stage} (${processed}/${total})`);
          }
        }
      } catch (err) {
        console.error("Failed to fetch progress:", err);
      }
    }, 500); // 每500ms轮询一次

    try {
      const resp = await fetch(`${API_BASE}/run_extraction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task,
          max_items: maxItems,
          strategy: { list_only: listOnly },
          use_omniparser: useOmniparser,
        }),
      });
      const data: RunExtractionResponse = await resp.json();
      setExtractionResult(data);
      setStatus(`extraction ${data.status}: ${data.items_extracted}/${data.target_count} items`);
      // 自动刷新文件列表
      await handleRefreshFiles();
    } catch (err) {
      setStatus(`error: ${String(err)}`);
    } finally {
      // 停止进度轮询
      if (progressIntervalRef.current !== null) {
        clearInterval(progressIntervalRef.current);
        progressIntervalRef.current = null;
      }
      setExtractionLoading(false);
    }
  };

  const findClosestElement = (point: [number, number], list: ElementItem[]) => {
    if (!list.length) return null;
    let best = list[0];
    let bestDist = distance(point, best.center);
    for (const elem of list.slice(1)) {
      const d = distance(point, elem.center);
      if (d < bestDist) {
        best = elem;
        bestDist = d;
      }
    }
    return best;
  };

  const distance = (a: [number, number], b: [number, number]) => {
    const dx = a[0] - b[0];
    const dy = a[1] - b[1];
    return Math.sqrt(dx * dx + dy * dy);
  };

  const handleMouseMove = (ev: React.MouseEvent<HTMLDivElement>) => {
    if (!imgRef.current || !annotated) return;
    const rect = imgRef.current.getBoundingClientRect();
    const x = Math.round(((ev.clientX - rect.left) / rect.width) * imgRef.current.naturalWidth);
    const y = Math.round(((ev.clientY - rect.top) / rect.height) * imgRef.current.naturalHeight);
    const closest = findClosestElement([x, y], elements);
    setHoverId(closest?.id ?? null);
  };

  // 清理函数：组件卸载时停止轮询
  useEffect(() => {
    return () => {
      if (progressIntervalRef.current !== null) {
        clearInterval(progressIntervalRef.current);
      }
    };
  }, []);

  return (
    <div style={{
      minHeight: "100vh",
      background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
      padding: "32px 24px"
    }}>
      <div style={{
        maxWidth: 1400,
        margin: "0 auto",
        display: "grid",
        gap: 24
      }}>
        {/* Header */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: "24px 32px",
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          animation: "fadeIn 0.5s ease-out"
        }}>
          <h1 style={{
            fontSize: 32,
            fontWeight: 700,
            background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
            marginBottom: 8
          }}>
            GUIAgent Local
          </h1>
          <p style={{ color: "#666", fontSize: 14 }}>Intelligent GUI</p>
        </div>

        {/* Task Input Card */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          animation: "fadeIn 0.5s ease-out 0.1s backwards"
        }}>
          <label style={{
            display: "block",
            fontWeight: 600,
            marginBottom: 12,
            fontSize: 15,
            color: "#333"
          }}>
            📝 Task Description
          </label>
          <textarea
            rows={3}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            style={{
              width: "100%",
              padding: 12,
              border: "2px solid #e0e0e0",
              borderRadius: 8,
              fontSize: 14,
              fontFamily: "inherit",
              resize: "vertical",
              transition: "all 0.2s",
              outline: "none"
            }}
            onFocus={(e) => e.target.style.borderColor = "#667eea"}
            onBlur={(e) => e.target.style.borderColor = "#e0e0e0"}
            placeholder="Enter your automation task here..."
          />

          {/* Action Buttons */}
          <div style={{
            display: "flex",
            gap: 12,
            alignItems: "center",
            flexWrap: "wrap",
            marginTop: 16
          }}>
            <button
              onClick={handleRun}
              disabled={loading}
              style={{
                padding: "10px 20px",
                background: loading ? "#ccc" : "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
                color: "white",
                border: "none",
                borderRadius: 8,
                fontWeight: 600,
                fontSize: 14,
                cursor: loading ? "not-allowed" : "pointer",
                transition: "all 0.2s",
                boxShadow: loading ? "none" : "0 4px 12px rgba(102, 126, 234, 0.3)",
                transform: loading ? "none" : "translateY(0)",
              }}
              onMouseEnter={(e) => !loading && (e.currentTarget.style.transform = "translateY(-2px)")}
              onMouseLeave={(e) => !loading && (e.currentTarget.style.transform = "translateY(0)")}
            >
              {loading ? "⏳ Running..." : "▶️ Run"}
            </button>

            <button
              onClick={handleTaskSpec}
              disabled={loading}
              style={{
                padding: "10px 20px",
                background: "white",
                color: "#667eea",
                border: "2px solid #667eea",
                borderRadius: 8,
                fontWeight: 600,
                fontSize: 14,
                cursor: loading ? "not-allowed" : "pointer",
                transition: "all 0.2s",
                opacity: loading ? 0.5 : 1
              }}
              onMouseEnter={(e) => !loading && (e.currentTarget.style.background = "#f5f7ff")}
              onMouseLeave={(e) => !loading && (e.currentTarget.style.background = "white")}
            >
              📋 Task Spec
            </button>

            <button
              onClick={handleNextStep}
              disabled={loading || planSteps.length === 0 || currentStepIndex >= planSteps.length - 1}
              style={{
                padding: "10px 20px",
                background: (loading || planSteps.length === 0 || currentStepIndex >= planSteps.length - 1) ? "#e0e0e0" : "#4caf50",
                color: "white",
                border: "none",
                borderRadius: 8,
                fontWeight: 600,
                fontSize: 14,
                cursor: (loading || planSteps.length === 0 || currentStepIndex >= planSteps.length - 1) ? "not-allowed" : "pointer",
                transition: "all 0.2s",
                opacity: (loading || planSteps.length === 0 || currentStepIndex >= planSteps.length - 1) ? 0.5 : 1
              }}
            >
              ⏭️ Next Step
            </button>

            <button
              onClick={handleRunExtraction}
              disabled={extractionLoading || loading}
              style={{
                padding: "10px 20px",
                background: (extractionLoading || loading) ? "#e0e0e0" : "#ff6b00",
                color: "white",
                border: "none",
                borderRadius: 8,
                fontWeight: 600,
                fontSize: 14,
                cursor: (extractionLoading || loading) ? "not-allowed" : "pointer",
                transition: "all 0.2s",
                opacity: (extractionLoading || loading) ? 0.5 : 1
              }}
            >
              🚀 Extract Data
            </button>

            <input
              type="number"
              value={maxItems}
              onChange={(e) => setMaxItems(Math.max(1, Math.min(100, parseInt(e.target.value) || 10)))}
              style={{
                width: 70,
                padding: "8px 12px",
                border: "2px solid #e0e0e0",
                borderRadius: 8,
                fontSize: 14,
                textAlign: "center"
              }}
              min={1}
              max={100}
              disabled={extractionLoading}
            />
            <span style={{ fontSize: 13, color: "#666", fontWeight: 500 }}>items</span>

            <label style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 13,
              cursor: "pointer",
              padding: "6px 12px",
              background: "#f5f5f5",
              borderRadius: 6,
              transition: "all 0.2s"
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = "#ebebeb"}
            onMouseLeave={(e) => e.currentTarget.style.background = "#f5f5f5"}
            >
              <input
                type="checkbox"
                checked={useOmniparser}
                onChange={(e) => setUseOmniparser(e.target.checked)}
                disabled={extractionLoading}
                style={{ cursor: "pointer" }}
              />
              <span>Use Annotation</span>
            </label>

            <label style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 13,
              cursor: "pointer",
              padding: "6px 12px",
              background: "#f5f5f5",
              borderRadius: 6,
              transition: "all 0.2s"
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = "#ebebeb"}
            onMouseLeave={(e) => e.currentTarget.style.background = "#f5f5f5"}
            >
              <input
                type="checkbox"
                checked={listOnly}
                onChange={(e) => setListOnly(e.target.checked)}
                disabled={extractionLoading}
                style={{ cursor: "pointer" }}
              />
              <span>List Only (Fast)</span>
            </label>

            <button
              onClick={handleRefreshFiles}
              disabled={loading}
              style={{
                padding: "10px 16px",
                background: "white",
                color: "#666",
                border: "2px solid #e0e0e0",
                borderRadius: 8,
                fontWeight: 600,
                fontSize: 14,
                cursor: loading ? "not-allowed" : "pointer",
                transition: "all 0.2s"
              }}
            >
              🔄 Refresh
            </button>
          </div>

          {/* Status Bar */}
          <div style={{
            marginTop: 16,
            padding: 12,
            background: status.includes("error") ? "#ffebee" : "#e8f5e9",
            borderRadius: 8,
            borderLeft: `4px solid ${status.includes("error") ? "#f44336" : "#4caf50"}`,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            <span style={{ fontSize: 13, fontWeight: 500, color: "#333" }}>
              {status || "Ready to start"}
            </span>
          </div>

          {currentUrl && (
            <div style={{
              marginTop: 12,
              padding: 10,
              background: "#f5f7ff",
              borderRadius: 8,
              fontSize: 12,
              color: "#666",
              display: "flex",
              alignItems: "center",
              gap: 8
            }}>
              <span style={{ fontWeight: 600 }}>🌐 URL:</span>
              <span style={{
                flex: 1,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap"
              }}>
                {currentUrl}
              </span>
            </div>
          )}
        </div>

        {/* Steps Card */}
        {planSteps.length > 0 && (
          <div style={{
            background: "rgba(255, 255, 255, 0.95)",
            backdropFilter: "blur(10px)",
            borderRadius: 16,
            padding: 24,
            boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
            animation: "fadeIn 0.5s ease-out 0.2s backwards"
          }}>
            <div style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 16
            }}>
              <span style={{ fontSize: 18, fontWeight: 700, color: "#333" }}>📋 Execution Steps</span>
              <span style={{
                padding: "4px 12px",
                background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
                color: "white",
                borderRadius: 12,
                fontSize: 12,
                fontWeight: 600
              }}>
                {currentStepIndex + 1} / {planSteps.length}
              </span>
            </div>
            <div style={{ display: "grid", gap: 8 }}>
              {planSteps.map((step, idx) => {
                const isActive = idx === currentStepIndex;
                const isDone = idx < currentStepIndex;
                return (
                  <div
                    key={`${idx}-${step}`}
                    style={{
                      padding: "14px 16px",
                      borderRadius: 10,
                      border: `2px solid ${isActive ? "#667eea" : isDone ? "#4caf50" : "#e0e0e0"}`,
                      background: isActive ? "linear-gradient(135deg, rgba(102, 126, 234, 0.1) 0%, rgba(118, 75, 162, 0.1) 100%)" : isDone ? "rgba(76, 175, 80, 0.05)" : "white",
                      fontSize: 14,
                      transition: "all 0.3s",
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      animation: `slideIn 0.3s ease-out ${idx * 0.05}s backwards`
                    }}
                  >
                    <div style={{
                      width: 28,
                      height: 28,
                      borderRadius: "50%",
                      background: isActive ? "linear-gradient(135deg, #667eea 0%, #764ba2 100%)" : isDone ? "#4caf50" : "#e0e0e0",
                      color: "white",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontWeight: 700,
                      fontSize: 13,
                      flexShrink: 0
                    }}>
                      {isDone ? "✓" : idx + 1}
                    </div>
                    <span style={{
                      color: isActive ? "#667eea" : isDone ? "#2e7d32" : "#666",
                      fontWeight: isActive ? 600 : 500,
                      flex: 1
                    }}>
                      {step}
                    </span>
                    {isActive && (
                      <div style={{
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        background: "#667eea",
                        animation: "pulse 2s ease-in-out infinite"
                      }} />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Debug Information */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          animation: "fadeIn 0.5s ease-out 0.3s backwards"
        }}>
          <div style={{
            fontSize: 18,
            fontWeight: 700,
            color: "#333",
            marginBottom: 16,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            🔍 Debug Information
          </div>
          <div style={{
            display: "grid",
            gap: 16,
            gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))"
          }}>
            {/* Task Spec */}
            <div style={{
              border: "2px solid #e0e0e0",
              borderRadius: 12,
              padding: 16,
              background: "white",
              transition: "all 0.2s"
            }}>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 12,
                color: "#667eea",
                display: "flex",
                alignItems: "center",
                gap: 6
              }}>
                📄 Task Spec
              </div>
              <pre style={{
                margin: 0,
                whiteSpace: "pre-wrap",
                fontSize: 11,
                color: "#666",
                maxHeight: 200,
                overflowY: "auto",
                padding: 8,
                background: "#f8f9fa",
                borderRadius: 6
              }}>
                {taskSpec ? JSON.stringify(taskSpec, null, 2) : "(empty)"}
              </pre>
              {taskSpecDebug && (
                <>
                  <div style={{
                    fontWeight: 600,
                    fontSize: 13,
                    margin: "12px 0 8px",
                    color: "#666"
                  }}>
                    Debug Info
                  </div>
                  <pre style={{
                    margin: 0,
                    whiteSpace: "pre-wrap",
                    fontSize: 11,
                    color: "#666",
                    maxHeight: 150,
                    overflowY: "auto",
                    padding: 8,
                    background: "#f8f9fa",
                    borderRadius: 6
                  }}>
                    {JSON.stringify(taskSpecDebug, null, 2)}
                  </pre>
                </>
              )}
            </div>

            {/* Plan Steps Debug */}
            <div style={{
              border: "2px solid #e0e0e0",
              borderRadius: 12,
              padding: 16,
              background: "white",
              transition: "all 0.2s"
            }}>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 12,
                color: "#667eea",
                display: "flex",
                alignItems: "center",
                gap: 6
              }}>
                🗺️ Plan Steps
              </div>
              <pre style={{
                margin: 0,
                whiteSpace: "pre-wrap",
                fontSize: 11,
                color: "#666",
                maxHeight: 200,
                overflowY: "auto",
                padding: 8,
                background: "#f8f9fa",
                borderRadius: 6
              }}>
                {planDebug ? JSON.stringify(planDebug, null, 2) : "(empty)"}
              </pre>
            </div>

            {/* Step Decision */}
            <div style={{
              border: "2px solid #e0e0e0",
              borderRadius: 12,
              padding: 16,
              background: "white",
              transition: "all 0.2s"
            }}>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 12,
                color: "#667eea",
                display: "flex",
                alignItems: "center",
                gap: 6
              }}>
                🎯 Step Decision
              </div>
              <pre style={{
                margin: 0,
                whiteSpace: "pre-wrap",
                fontSize: 11,
                color: "#666",
                maxHeight: 200,
                overflowY: "auto",
                padding: 8,
                background: "#f8f9fa",
                borderRadius: 6
              }}>
                {stepDebug ? JSON.stringify(stepDebug, null, 2) : "(empty)"}
              </pre>
              {stepDebug && "target_point" in stepDebug && (
                <div style={{
                  marginTop: 8,
                  fontSize: 11,
                  color: "#666",
                  padding: 8,
                  background: "#fff3e0",
                  borderRadius: 6,
                  borderLeft: "3px solid #ff9800"
                }}>
                  <strong>Target Point:</strong> {JSON.stringify((stepDebug as any).target_point)}
                </div>
              )}
            </div>

            {/* Finish Check */}
            <div style={{
              border: "2px solid #e0e0e0",
              borderRadius: 12,
              padding: 16,
              background: "white",
              transition: "all 0.2s"
            }}>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 12,
                color: "#667eea",
                display: "flex",
                alignItems: "center",
                gap: 6
              }}>
                ✅ Finish Check
              </div>
              <pre style={{
                margin: 0,
                whiteSpace: "pre-wrap",
                fontSize: 11,
                color: "#666",
                maxHeight: 200,
                overflowY: "auto",
                padding: 8,
                background: "#f8f9fa",
                borderRadius: 6
              }}>
                {finishDebug ? JSON.stringify(finishDebug, null, 2) : "(empty)"}
              </pre>
            </div>
          </div>
        </div>

      {/* VLM Conversation Details */}
      {vlmConversation && (
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          border: "3px solid #667eea",
          animation: "fadeIn 0.5s ease-out"
        }}>
          <div style={{
            fontSize: 20,
            fontWeight: 700,
            background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
            marginBottom: 20,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            🔍 VLM Conversation Details
          </div>

          {/* Annotated Image */}
          <div style={{ marginBottom: 20 }}>
            <div style={{
              fontWeight: 600,
              fontSize: 15,
              marginBottom: 12,
              color: "#333"
            }}>
              📸 Annotated Image (VLM View)
            </div>
            <div style={{
              border: "2px solid #e0e0e0",
              borderRadius: 12,
              overflow: "hidden",
              background: "white",
              boxShadow: "0 4px 12px rgba(0, 0, 0, 0.08)"
            }}>
              <img
                src={`data:image/png;base64,${vlmConversation.request.annotated_image}`}
                alt="VLM Annotated View"
                style={{ width: "100%", maxWidth: 900, display: "block" }}
              />
            </div>
          </div>

          {/* Request Information */}
          <div style={{
            border: "2px solid #e0e0e0",
            borderRadius: 12,
            padding: 20,
            background: "white",
            marginBottom: 16
          }}>
            <div style={{
              fontWeight: 600,
              fontSize: 15,
              marginBottom: 12,
              color: "#667eea",
              display: "flex",
              alignItems: "center",
              gap: 6
            }}>
              📤 VLM Request
            </div>
            <div style={{ display: "grid", gap: 10, fontSize: 14 }}>
              <div style={{
                padding: 10,
                background: "#f8f9fa",
                borderRadius: 8,
                borderLeft: "3px solid #667eea"
              }}>
                <strong>Task:</strong> {vlmConversation.request.task}
              </div>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                <div style={{
                  padding: "8px 16px",
                  background: "#e3f2fd",
                  borderRadius: 8,
                  fontSize: 13
                }}>
                  <strong>Elements:</strong> {vlmConversation.request.elements_count}
                </div>
                <div style={{
                  padding: "8px 16px",
                  background: "#f3e5f5",
                  borderRadius: 8,
                  fontSize: 13
                }}>
                  <strong>Image Size:</strong> {vlmConversation.request.image_size[0]} × {vlmConversation.request.image_size[1]}
                </div>
              </div>
            </div>

            <div style={{ marginTop: 16 }}>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 10,
                color: "#333"
              }}>
                Element List (First 20):
              </div>
              <div style={{
                maxHeight: 250,
                overflowY: "auto",
                border: "2px solid #e0e0e0",
                borderRadius: 8,
                background: "white"
              }}>
                <table style={{
                  width: "100%",
                  borderCollapse: "collapse",
                  fontSize: 13
                }}>
                  <thead style={{
                    position: "sticky",
                    top: 0,
                    background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
                    color: "white"
                  }}>
                    <tr>
                      <th style={{
                        padding: "10px 12px",
                        textAlign: "left",
                        fontWeight: 600
                      }}>ID</th>
                      <th style={{
                        padding: "10px 12px",
                        textAlign: "left",
                        fontWeight: 600
                      }}>Type</th>
                      <th style={{
                        padding: "10px 12px",
                        textAlign: "left",
                        fontWeight: 600
                      }}>Content</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vlmConversation.request.elements.map((elem, idx) => (
                      <tr
                        key={elem.id}
                        style={{
                          borderBottom: "1px solid #e0e0e0",
                          background: idx % 2 === 0 ? "#fafafa" : "white"
                        }}
                      >
                        <td style={{
                          padding: "10px 12px",
                          fontWeight: 600,
                          color: "#667eea"
                        }}>{elem.id}</td>
                        <td style={{
                          padding: "10px 12px",
                          color: "#666"
                        }}>{elem.type}</td>
                        <td style={{
                          padding: "10px 12px",
                          maxWidth: 500,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap"
                        }}>
                          {elem.content}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {/* Response Information */}
          <div style={{
            border: "2px solid #e0e0e0",
            borderRadius: 12,
            padding: 20,
            background: "white"
          }}>
            <div style={{
              fontWeight: 600,
              fontSize: 15,
              marginBottom: 12,
              color: "#667eea",
              display: "flex",
              alignItems: "center",
              gap: 6
            }}>
              📥 VLM Response
            </div>
            <div>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 8,
                color: "#333"
              }}>
                Parsed JSON:
              </div>
              <pre style={{
                margin: 0,
                padding: 16,
                background: "#f8f9fa",
                borderRadius: 8,
                fontSize: 12,
                overflowX: "auto",
                border: "2px solid #e0e0e0",
                maxHeight: 300
              }}>
                {JSON.stringify(vlmConversation.response, null, 2)}
              </pre>
            </div>

            <div style={{ marginTop: 16 }}>
              <div style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 8,
                color: "#333"
              }}>
                Raw Response:
              </div>
              <pre style={{
                margin: 0,
                padding: 16,
                background: "#f8f9fa",
                borderRadius: 8,
                fontSize: 12,
                overflowX: "auto",
                border: "2px solid #e0e0e0",
                whiteSpace: "pre-wrap",
                maxHeight: 300
              }}>
                {vlmConversation.response_raw}
              </pre>
            </div>
          </div>
        </div>
      )}

        {/* Output Files */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          animation: "fadeIn 0.5s ease-out 0.4s backwards"
        }}>
          <div style={{
            fontSize: 18,
            fontWeight: 700,
            color: "#333",
            marginBottom: 16,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            📁 Output Files
            {files.length > 0 && (
              <span style={{
                padding: "4px 12px",
                background: "#4caf50",
                color: "white",
                borderRadius: 12,
                fontSize: 12,
                fontWeight: 600
              }}>
                {files.length}
              </span>
            )}
          </div>
          {files.length === 0 ? (
            <div style={{
              padding: 32,
              textAlign: "center",
              color: "#999",
              fontSize: 14,
              background: "#f8f9fa",
              borderRadius: 12,
              border: "2px dashed #e0e0e0"
            }}>
              No output files yet. Run extraction to generate files.
            </div>
          ) : (
            <div style={{ display: "grid", gap: 8 }}>
              {files.map((file, idx) => (
                <a
                  key={file}
                  href={`${API_BASE}/files/${encodeURIComponent(file)}`}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    padding: "12px 16px",
                    background: "white",
                    border: "2px solid #e0e0e0",
                    borderRadius: 10,
                    textDecoration: "none",
                    color: "#667eea",
                    fontWeight: 500,
                    fontSize: 14,
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    transition: "all 0.2s",
                    animation: `slideIn 0.3s ease-out ${idx * 0.05}s backwards`
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = "#667eea";
                    e.currentTarget.style.background = "#f5f7ff";
                    e.currentTarget.style.transform = "translateX(4px)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = "#e0e0e0";
                    e.currentTarget.style.background = "white";
                    e.currentTarget.style.transform = "translateX(0)";
                  }}
                >
                  <span style={{ fontSize: 18 }}>📄</span>
                  <span style={{ flex: 1 }}>{file}</span>
                  <span style={{ fontSize: 12, color: "#999" }}>↗</span>
                </a>
              ))}
            </div>
          )}
        </div>

      {/* Real-time Progress */}
      {extractionLoading && extractionProgress.length > 0 && (
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          border: "3px solid #2196f3",
          animation: "fadeIn 0.5s ease-out"
        }}>
          <div style={{
            fontSize: 18,
            fontWeight: 700,
            color: "#2196f3",
            marginBottom: 16,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            <div style={{
              width: 20,
              height: 20,
              border: "3px solid #2196f3",
              borderTopColor: "transparent",
              borderRadius: "50%",
              animation: "spin 1s linear infinite"
            }} />
            Real-time Progress
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            {extractionProgress.map((prog, idx) => (
              <div
                key={idx}
                style={{
                  padding: "14px 16px",
                  background: "white",
                  border: "2px solid #90caf9",
                  borderRadius: 10,
                  fontSize: 13,
                  animation: `slideIn 0.3s ease-out ${idx * 0.05}s backwards`
                }}
              >
                <div style={{
                  fontWeight: 600,
                  marginBottom: 6,
                  color: "#1976d2",
                  fontSize: 14
                }}>
                  {String(prog.stage)} - {String(prog.status)}
                </div>
                {prog.current_action && (
                  <div style={{
                    color: "#1565c0",
                    fontSize: 12,
                    marginBottom: 4,
                    padding: "6px 10px",
                    background: "#e3f2fd",
                    borderRadius: 6,
                    borderLeft: "3px solid #2196f3"
                  }}>
                    {String(prog.current_action)}
                  </div>
                )}
                {prog.processed !== undefined && prog.total !== undefined && (
                  <div style={{
                    fontSize: 12,
                    color: "#666",
                    marginTop: 8,
                    display: "flex",
                    alignItems: "center",
                    gap: 8
                  }}>
                    <span style={{ fontWeight: 600 }}>Progress:</span>
                    <div style={{
                      flex: 1,
                      height: 8,
                      background: "#e0e0e0",
                      borderRadius: 4,
                      overflow: "hidden"
                    }}>
                      <div style={{
                        height: "100%",
                        background: "linear-gradient(90deg, #2196f3 0%, #21cbf3 100%)",
                        width: `${(Number(prog.processed) / Number(prog.total)) * 100}%`,
                        transition: "width 0.3s"
                      }} />
                    </div>
                    <span style={{ fontWeight: 600, minWidth: 60, textAlign: "right" }}>
                      {String(prog.processed)} / {String(prog.total)}
                    </span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Extraction Results */}
      {extractionResult && (
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          border: `3px solid ${extractionResult.status === 'success' ? '#4caf50' : extractionResult.status === 'partial' ? '#ff9800' : '#f44336'}`,
          animation: "fadeIn 0.5s ease-out"
        }}>
          <div style={{
            fontSize: 18,
            fontWeight: 700,
            color: extractionResult.status === 'success' ? '#4caf50' : extractionResult.status === 'partial' ? '#ff9800' : '#f44336',
            marginBottom: 16,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            {extractionResult.status === 'success' ? '✅' : extractionResult.status === 'partial' ? '⚠️' : '❌'}
            Extraction Results
          </div>

          <div style={{
            display: "grid",
            gap: 12,
            marginBottom: 20
          }}>
            <div style={{
              display: "flex",
              gap: 16,
              flexWrap: "wrap"
            }}>
              <div style={{
                padding: "12px 20px",
                background: extractionResult.status === 'success' ? '#e8f5e9' : extractionResult.status === 'partial' ? '#fff3e0' : '#ffebee',
                borderRadius: 10,
                borderLeft: `4px solid ${extractionResult.status === 'success' ? '#4caf50' : extractionResult.status === 'partial' ? '#ff9800' : '#f44336'}`
              }}>
                <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>Status</div>
                <div style={{
                  fontSize: 16,
                  fontWeight: 700,
                  color: extractionResult.status === 'success' ? '#2e7d32' : extractionResult.status === 'partial' ? '#f57c00' : '#d32f2f',
                  textTransform: "uppercase"
                }}>
                  {extractionResult.status}
                </div>
              </div>

              <div style={{
                padding: "12px 20px",
                background: "#e3f2fd",
                borderRadius: 10,
                borderLeft: "4px solid #2196f3"
              }}>
                <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>Items Extracted</div>
                <div style={{ fontSize: 16, fontWeight: 700, color: "#1976d2" }}>
                  {extractionResult.items_extracted} / {extractionResult.target_count}
                </div>
              </div>

              {extractionResult.file_path && (
                <div style={{
                  padding: "12px 20px",
                  background: "#f3e5f5",
                  borderRadius: 10,
                  borderLeft: "4px solid #9c27b0",
                  flex: 1,
                  minWidth: 200
                }}>
                  <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>Output File</div>
                  <a
                    href={`${API_BASE}/files/${encodeURIComponent(extractionResult.file_path)}`}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      color: "#7b1fa2",
                      fontWeight: 600,
                      fontSize: 14,
                      textDecoration: "none",
                      display: "flex",
                      alignItems: "center",
                      gap: 6
                    }}
                  >
                    {extractionResult.file_path}
                    <span style={{ fontSize: 12 }}>↗</span>
                  </a>
                </div>
              )}
            </div>

            {extractionResult.errors.length > 0 && (
              <div style={{
                padding: "12px 16px",
                background: "#ffebee",
                borderRadius: 10,
                borderLeft: "4px solid #f44336",
                color: "#c62828",
                fontSize: 13
              }}>
                <div style={{ fontWeight: 600, marginBottom: 6 }}>⚠️ Errors:</div>
                {extractionResult.errors.join("; ")}
              </div>
            )}
          </div>

          {/* Data Preview */}
          {extractionResult.items.length > 0 && (
            <div>
              <div style={{
                fontWeight: 600,
                fontSize: 15,
                marginBottom: 12,
                color: "#333"
              }}>
                📊 Data Preview
              </div>
              <div style={{
                overflowX: "auto",
                maxHeight: 450,
                border: "2px solid #e0e0e0",
                borderRadius: 12,
                background: "white"
              }}>
                <table style={{
                  width: "100%",
                  borderCollapse: "collapse",
                  fontSize: 13
                }}>
                  <thead style={{
                    position: "sticky",
                    top: 0,
                    background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
                    color: "white",
                    zIndex: 1
                  }}>
                    <tr>
                      {Object.keys(extractionResult.items[0]).map((key) => (
                        <th
                          key={key}
                          style={{
                            padding: "12px 16px",
                            textAlign: "left",
                            fontWeight: 600,
                            whiteSpace: "nowrap"
                          }}
                        >
                          {key}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {extractionResult.items.map((item, idx) => (
                      <tr
                        key={idx}
                        style={{
                          borderBottom: "1px solid #e0e0e0",
                          background: idx % 2 === 0 ? "#fafafa" : "white",
                          transition: "background 0.2s"
                        }}
                        onMouseEnter={(e) => e.currentTarget.style.background = "#f5f7ff"}
                        onMouseLeave={(e) => e.currentTarget.style.background = idx % 2 === 0 ? "#fafafa" : "white"}
                      >
                        {Object.values(item).map((value, vidx) => (
                          <td
                            key={vidx}
                            style={{
                              padding: "12px 16px",
                              maxWidth: 350,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap"
                            }}
                          >
                            {String(value)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Execution Progress */}
          {extractionResult.progress.length > 0 && (
            <div style={{ marginTop: 20 }}>
              <div style={{
                fontWeight: 600,
                fontSize: 15,
                marginBottom: 12,
                color: "#333"
              }}>
                ⏱️ Execution Timeline
              </div>
              <div style={{ display: "grid", gap: 8 }}>
                {extractionResult.progress.map((prog, idx) => (
                  <div
                    key={idx}
                    style={{
                      padding: "12px 16px",
                      background: "white",
                      border: "2px solid #e0e0e0",
                      borderRadius: 10,
                      fontSize: 13,
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      animation: `slideIn 0.3s ease-out ${idx * 0.05}s backwards`
                    }}
                  >
                    <div style={{
                      width: 8,
                      height: 8,
                      borderRadius: "50%",
                      background: "#4caf50",
                      flexShrink: 0
                    }} />
                    <span style={{ fontWeight: 600, color: "#333" }}>{String(prog.stage)}:</span>
                    <span style={{ color: "#666" }}>{String(prog.status)}</span>
                    {prog.items !== undefined && (
                      <span style={{
                        marginLeft: "auto",
                        padding: "4px 10px",
                        background: "#e8f5e9",
                        borderRadius: 6,
                        fontSize: 12,
                        fontWeight: 600,
                        color: "#2e7d32"
                      }}>
                        {String(prog.items)} items
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

        {/* Screenshot Viewer */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          animation: "fadeIn 0.5s ease-out 0.5s backwards"
        }}>
          <div style={{
            fontSize: 18,
            fontWeight: 700,
            color: "#333",
            marginBottom: 16,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            🖼️ Screenshot Viewer
          </div>
          <div
            style={{
              position: "relative",
              border: "3px solid #e0e0e0",
              borderRadius: 12,
              overflow: "hidden",
              cursor: imgSrc ? "crosshair" : "default",
              background: imgSrc ? "white" : "#f8f9fa",
              transition: "all 0.2s"
            }}
            onClick={handleManualClick}
            onMouseMove={handleMouseMove}
          >
            {imgSrc ? (
              <img
                ref={imgRef}
                src={imgSrc}
                alt="annotated"
                style={{
                  width: "100%",
                  display: "block"
                }}
              />
            ) : (
              <div style={{
                padding: 64,
                textAlign: "center",
                color: "#999",
                fontSize: 15
              }}>
                <div style={{ fontSize: 48, marginBottom: 16 }}>📸</div>
                <div>No screenshot yet. Click Run to start.</div>
              </div>
            )}

            {imgRef.current && elements.map((elem) => {
              const [x, y, w, h] = elem.bbox;
              const scaleX = imgRef.current!.width / imgRef.current!.naturalWidth;
              const scaleY = imgRef.current!.height / imgRef.current!.naturalHeight;
              const left = x * scaleX;
              const top = y * scaleY;
              const width = w * scaleX;
              const height = h * scaleY;
              const isHover = hoverId === elem.id;
              const isSelected = selectedId === elem.id;
              return (
                <div
                  key={elem.id}
                  style={{
                    position: "absolute",
                    left,
                    top,
                    width,
                    height,
                    border: isSelected ? "3px solid #ff6b00" : isHover ? "3px solid #667eea" : "2px solid #4caf50",
                    pointerEvents: "none",
                    boxSizing: "border-box",
                    background: isSelected ? "rgba(255,107,0,0.2)" : isHover ? "rgba(102,126,234,0.15)" : "transparent",
                    transition: "all 0.2s",
                    borderRadius: 4
                  }}
                  title={`${elem.id}: ${elem.content}`}
                />
              );
            })}
            {imgRef.current && lastTargetPoint && (() => {
              const [x, y] = lastTargetPoint;
              const scaleX = imgRef.current!.width / imgRef.current!.naturalWidth;
              const scaleY = imgRef.current!.height / imgRef.current!.naturalHeight;
              const left = x * scaleX;
              const top = y * scaleY;
              return (
                <div
                  key="target-point"
                  style={{
                    position: "absolute",
                    left: left - 6,
                    top: top - 6,
                    width: 12,
                    height: 12,
                    borderRadius: "50%",
                    background: "#ff3b30",
                    border: "2px solid #fff",
                    boxShadow: "0 0 8px rgba(255,59,48,0.6), 0 0 0 4px rgba(255,59,48,0.2)",
                    pointerEvents: "none",
                    animation: "pulse 2s ease-in-out infinite"
                  }}
                  title={`target_point: ${x}, ${y}`}
                />
              );
            })()}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;


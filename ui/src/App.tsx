import React, { useMemo, useRef, useState } from "react";

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
  reason?: string;
  annotated_image_base64?: string;
  elements?: ElementItem[];
  current_url?: string | null;
  planner_debug?: Record<string, unknown> | null;
  finish_debug?: Record<string, unknown> | null;
};

type PlanStepsResponse = {
  steps: string[];
  debug?: Record<string, unknown> | null;
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
  const [lastTargetPoint, setLastTargetPoint] = useState<[number, number] | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

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
      });
      setFinishDebug(data.finish_debug || null);
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

  return (
    <div style={{ fontFamily: "sans-serif", padding: 16, display: "grid", gap: 12 }}>
      <h2>GUIAgent Local</h2>
      <div style={{ display: "grid", gap: 8 }}>
        <label>Task</label>
        <textarea
          rows={3}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          style={{ width: "100%" }}
        />
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button onClick={handleRun} disabled={loading}>
            Run
          </button>
          <button
            onClick={handleNextStep}
            disabled={loading || planSteps.length === 0 || currentStepIndex >= planSteps.length - 1}
          >
            下一步
          </button>
          <span>{status}</span>
        </div>
        <div style={{ fontSize: 12, color: "#555" }}>
          URL: {currentUrl || "(unknown)"}
        </div>
      </div>

      <div style={{ display: "grid", gap: 6, maxWidth: 1200 }}>
        <div style={{ fontWeight: 600 }}>Steps</div>
        {planSteps.length === 0 ? (
          <div style={{ fontSize: 12, color: "#666" }}>No steps yet. Click Run.</div>
        ) : (
          <div style={{ display: "grid", gap: 4 }}>
            {planSteps.map((step, idx) => {
              const isActive = idx === currentStepIndex;
              const isDone = idx < currentStepIndex;
              const color = isActive ? "#1e90ff" : isDone ? "#2e7d32" : "#666";
              return (
                <div
                  key={`${idx}-${step}`}
                  style={{
                    padding: "6px 8px",
                    borderRadius: 6,
                    border: `1px solid ${isActive ? "#1e90ff" : "#ddd"}`,
                    background: isActive ? "rgba(30,144,255,0.08)" : isDone ? "rgba(46,125,50,0.06)" : "#fafafa",
                    color,
                    fontSize: 13,
                  }}
                >
                  {idx + 1}. {step}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div style={{ display: "grid", gap: 12, maxWidth: 1200 }}>
        <div style={{ fontWeight: 600 }}>Debug</div>
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr 1fr" }}>
          <div style={{ border: "1px solid #ddd", borderRadius: 6, padding: 8, background: "#fafafa" }}>
            <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 6 }}>Plan Steps</div>
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 11 }}>
              {planDebug ? JSON.stringify(planDebug, null, 2) : "(empty)"}
            </pre>
          </div>
          <div style={{ border: "1px solid #ddd", borderRadius: 6, padding: 8, background: "#fafafa" }}>
            <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 6 }}>Step Decide</div>
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 11 }}>
              {stepDebug ? JSON.stringify(stepDebug, null, 2) : "(empty)"}
            </pre>
            <div style={{ marginTop: 6, fontSize: 11, color: "#555" }}>
              Point: {"target_point" in (stepDebug || {}) ? JSON.stringify((stepDebug as any).target_point) : "(none)"}
            </div>
          </div>
          <div style={{ border: "1px solid #ddd", borderRadius: 6, padding: 8, background: "#fafafa" }}>
            <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 6 }}>Finish Check</div>
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 11 }}>
              {finishDebug ? JSON.stringify(finishDebug, null, 2) : "(empty)"}
            </pre>
          </div>
        </div>
      </div>

      <div
        style={{
          position: "relative",
          border: "1px solid #ccc",
          width: "100%",
          maxWidth: 1200,
          cursor: "crosshair",
        }}
        onClick={handleManualClick}
        onMouseMove={handleMouseMove}
      >
        {imgSrc ? (
          <img ref={imgRef} src={imgSrc} alt="annotated" style={{ width: "100%" }} />
        ) : (
          <div style={{ padding: 24 }}>No image yet. Click Run.</div>
        )}

        {imgRef.current && elements.map((elem) => {
          const [x, y, w, h] = elem.bbox;
          const scaleX = imgRef.current.width / imgRef.current.naturalWidth;
          const scaleY = imgRef.current.height / imgRef.current.naturalHeight;
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
                border: isSelected ? "2px solid #ff6b00" : isHover ? "2px solid #1e90ff" : "1px solid #00c853",
                pointerEvents: "none",
                boxSizing: "border-box",
                background: isSelected ? "rgba(255,107,0,0.15)" : "transparent",
              }}
              title={`${elem.id}: ${elem.content}`}
            />
          );
        })}
        {imgRef.current && lastTargetPoint && (() => {
          const [x, y] = lastTargetPoint;
          const scaleX = imgRef.current.width / imgRef.current.naturalWidth;
          const scaleY = imgRef.current.height / imgRef.current.naturalHeight;
          const left = x * scaleX;
          const top = y * scaleY;
          return (
            <div
              style={{
                position: "absolute",
                left: left - 4,
                top: top - 4,
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "#ff3b30",
                border: "1px solid #fff",
                boxShadow: "0 0 4px rgba(0,0,0,0.4)",
                pointerEvents: "none",
              }}
              title={`target_point: ${x}, ${y}`}
            />
          );
        })()}
      </div>
    </div>
  );
}

export default App;


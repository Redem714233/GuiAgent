import React, { useEffect, useMemo, useRef, useState } from "react";

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
  reason?: string;
  annotated_image_base64?: string;
  elements?: ElementItem[];
};

function App() {
  const [task, setTask] = useState("打开百度并点击搜索框");
  const [elements, setElements] = useState<ElementItem[]>([]);
  const [annotated, setAnnotated] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [hoverId, setHoverId] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  const imgSrc = useMemo(() => {
    if (!annotated) return null;
    return `data:image/png;base64,${annotated}`;
  }, [annotated]);

  const handleStep = async () => {
    setLoading(true);
    setStatus("processing...");
    setSelectedId(null);
    try {
      const resp = await fetch(`${API_BASE}/step`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task }),
      });
      const data: StepResponse = await resp.json();
      setAnnotated(data.annotated_image_base64 || null);
      setElements(data.elements || []);
      setStatus(`done (${data.reason || ""})`);
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
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={handleStep} disabled={loading}>
            Run Step (LLM)
          </button>
          <span>{status}</span>
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
          <div style={{ padding: 24 }}>No image yet. Click Run Step.</div>
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
      </div>
    </div>
  );
}

export default App;


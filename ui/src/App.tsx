import React, { useMemo, useRef, useState } from "react";

const API_BASE = "http://127.0.0.1:8000";

type ElementItem = {
  id: number;
  type: string;
  content: string;
  center: [number, number];
  bbox: [number, number, number, number];
};

type RunTaskResponse = {
  status: string;
  steps: Array<{
    step_index: number;
    retry_index: number;
    description: string;
    action: string;
    verification: Record<string, unknown>;
    status: string;
    termination_reason?: string;
    user_message?: string;
  }>;
  extracted_items: Array<Record<string, unknown>>;
  excel_file: string | null;
  termination_reason: string | null;
  user_message: string | null;
  final_url: string;
  reasoning: string;
  plan?: string[];
};

type StreamStepItem = {
  step_index: number;
  retry_index: number;
  description: string;
  action: string;
  verification: Record<string, unknown>;
  status: string;
  termination_reason?: string;
  user_message?: string;
};

const STEEL_STAGE_TITLE: Record<string, string> = {
  auth: "加载登录态",
  navigation: "进入历史记录页",
  date: "设置日期范围",
  filter: "筛选异常状态",
  wait_ready: "等待筛选结果",
  download_excel: "下载异常Excel",
  download_zip: "下载原始图片ZIP",
  download_zip_retry: "重试图片下载",
  recover: "恢复后重试",
  unzip: "解压图片文件",
  embed: "生成带图Excel",
  done: "钢铁任务完成",
  failed: "钢铁任务失败",
};

const toSteelStep = (
  stage: string,
  message: string,
  index: number,
): StreamStepItem => {
  const normalizedStage = (stage || "stage").trim();
  const normalizedMessage = (message || "").trim();
  const isFailed = normalizedStage === "failed";
  const isDone = normalizedStage === "done";

  return {
    step_index: index,
    retry_index: normalizedStage.includes("retry") ? 1 : 0,
    description: STEEL_STAGE_TITLE[normalizedStage] || normalizedMessage || normalizedStage,
    action: normalizedStage,
    verification: normalizedMessage ? { reasoning: normalizedMessage } : {},
    status: isFailed ? "failed" : (isDone ? "success" : "success"),
    termination_reason: isFailed ? normalizedMessage : undefined,
    user_message: isFailed ? normalizedMessage : undefined,
  };
};

type FileListResponse = {
  files: string[];
};

function App() {
  const [task, setTask] = useState("打开百度，搜索Python，提取前5个结果");
  const [listOnly, setListOnly] = useState(false);
  const [elements, setElements] = useState<ElementItem[]>([]);
  const [annotated, setAnnotated] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [currentUrl, setCurrentUrl] = useState<string>("");
  const [hoverId, setHoverId] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [taskResult, setTaskResult] = useState<RunTaskResponse | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const steelStepKeysRef = useRef<Set<string>>(new Set());

  const imgSrc = useMemo(() => {
    if (!annotated) return null;
    return `data:image/png;base64,${annotated}`;
  }, [annotated]);

  const parseMaxItemsFromTask = (text: string): number => {
    const lowered = text.toLowerCase();
    const patterns = [
      /前\s*(\d+)\s*(?:条|个|本|篇|项|条数据|results?|items?)/i,
      /(\d+)\s*(?:条|个|本|篇|项)\s*(?:数据|结果)?/i,
      /(?:extract|collect|scrape)\s*(\d+)\s*(?:results?|items?)/i,
    ];

    for (const pattern of patterns) {
      const match = lowered.match(pattern);
      if (match?.[1]) {
        const value = Number(match[1]);
        if (Number.isFinite(value) && value > 0) {
          return Math.min(value, 50);
        }
      }
    }

    return 50;
  };

  const handleRunTask = async () => {
    setLoading(true);
    setStatus("执行任务中...");
    setSelectedId(null);
    setTaskResult(null);
    steelStepKeysRef.current = new Set();

    try {
      const maxItems = parseMaxItemsFromTask(task);
      const params = new URLSearchParams({
        task,
        max_steps: "20",
        max_retries_per_step: "3",
        max_items: maxItems.toString(),
        max_pages: "5",
        list_only: listOnly.toString()
      });

      const eventSource = new EventSource(`${API_BASE}/run_task_stream?${params}`);
      let streamCompleted = false;

      eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === "steel_stage") {
          const stage = data.stage ? ` [${data.stage}]` : "";
          const message = data.message || "钢铁流程执行中";
          setStatus(`🎮${stage} ${message}`);

          const stageName = String(data.stage || "").trim();
          const stepKey = `${stageName}::${message}`;
          if (!steelStepKeysRef.current.has(stepKey)) {
            steelStepKeysRef.current.add(stepKey);
            setTaskResult(prev => {
              if (!prev) return prev;
              const nextIndex = prev.steps.length;
              return {
                ...prev,
                steps: [...prev.steps, toSteelStep(stageName, message, nextIndex)],
              };
            });
          }
          return;
        }

        if (data.type === "start") {
          setStatus("🚀 开始执行任务...");
          setTaskResult({
            status: "running",
            steps: [],
            extracted_items: [],
            excel_file: null,
            termination_reason: null,
            user_message: null,
            final_url: "",
            reasoning: "",
            plan: []
          });
        } else if (data.type === "plan") {
          setTaskResult(prev => prev ? {...prev, plan: data.steps} : null);
          setStatus(`📋 已规划 ${data.steps.length} 个步骤`);
        } else if (data.type === "step_start") {
          setStatus(`⏳ 步骤 ${data.index + 1}: ${data.description}`);
        } else if (data.type === "step_complete") {
          setTaskResult(prev => {
            if (!prev) return null;
            return {
              ...prev,
              steps: [...prev.steps, data.result]
            };
          });

          const statusIcon = data.result.status === "success" ? "✓" :
                           data.result.status === "terminated" ? "⚠" : "✗";
          setStatus(`${statusIcon} 步骤 ${data.result.step_index + 1} ${data.result.status}`);
        } else if (data.type === "extract_start") {
          setStatus("📊 开始提取数据...");
        } else if (data.type === "extract_progress") {
          setStatus(`📊 已提取 ${data.count} 条数据`);
        } else if (data.type === "extract_done") {
          setStatus(`✅ 提取完成！共 ${data.count} 条数据`);
          if (data.file) {
            handleRefreshFiles();
          }
        } else if (data.type === "done") {
          streamCompleted = true;
          const isSteelRun = Boolean(data.steel_result);
          setTaskResult(prev => {
            if (!prev) return null;
            return {
              ...prev,
              status: data.status || "success",
              final_url: data.final_url,
              extracted_items: data.extracted_items || [],
              excel_file: data.excel_file || null
            };
          });
          if (data.excel_file) {
            void handleRefreshFiles();
          }
          if (data.status === "terminated") {
            setStatus(`⚠️ 任务终止: ${data.user_message || data.reasoning || "已终止"}`);
          } else if (data.status === "failed") {
            setStatus(`❌ 任务失败: ${data.reasoning || "未知错误"}`);
          } else {
            setStatus("✅ 任务完成！");
          }
          setCurrentUrl(data.final_url);
          eventSource.close();
          setLoading(false);
          if (!isSteelRun) {
            handleMark();
          }
        } else if (data.type === "error") {
          streamCompleted = true;
          setStatus(`❌ 错误: ${data.message}`);
          setTaskResult(prev => prev ? { ...prev, status: "failed", reasoning: data.message || prev.reasoning } : prev);
          eventSource.close();
          setLoading(false);
        }
      };

      eventSource.onerror = () => {
        if (streamCompleted) {
          return;
        }
        setStatus("❌ 连接中断");
        setTaskResult(prev => prev ? { ...prev, status: prev.status === "running" ? "failed" : prev.status } : prev);
        eventSource.close();
        setLoading(false);
      };

    } catch (err) {
      setStatus(`❌ 错误: ${String(err)}`);
      setLoading(false);
    }
  };

  const handleRunTaskOld = async () => {
    setLoading(true);
    setStatus("执行任务中...");
    setSelectedId(null);
    setTaskResult(null);

    try {
      const resp = await fetch(`${API_BASE}/run_task`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task,
          max_steps: 20,
          max_retries_per_step: 3,
          max_items: 50,
          max_pages: 5,
          list_only: listOnly,
        }),
      });
      const data: RunTaskResponse = await resp.json();
      setTaskResult(data);

      // 更新状态
      if (data.status === "terminated") {
        setStatus(`⚠️ 任务终止: ${data.user_message || data.termination_reason}`);
      } else if (data.status === "success") {
        if (data.extracted_items && data.extracted_items.length > 0) {
          setStatus(`✅ 任务完成！提取了 ${data.extracted_items.length} 条数据`);
        } else {
          setStatus(`✅ 任务完成！`);
        }
      } else {
        setStatus(`❌ 任务失败: ${data.reasoning}`);
      }

      // 更新当前 URL
      setCurrentUrl(data.final_url);

      // 刷新文件列表
      if (data.excel_file) {
        await handleRefreshFiles();
      }

      // 获取最终截图
      await handleMark();

    } catch (err) {
      setStatus(`❌ 错误: ${String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleMark = async () => {
    try {
      const resp = await fetch(`${API_BASE}/mark_elements`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setElements(data.elements || []);
      setAnnotated(data.annotated_image_base64 || null);
      setCurrentUrl(data.current_url || "");
    } catch (err) {
      console.error("Mark elements failed:", err);
    }
  };

  const handleRefreshFiles = async () => {
    try {
      const resp = await fetch(`${API_BASE}/files`);
      const data: FileListResponse = await resp.json();
      setFiles(data.files || []);
    } catch (err) {
      console.error("Refresh files failed:", err);
    }
  };

  const handleImageClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!imgRef.current) return;
    const rect = imgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const scaleX = imgRef.current.naturalWidth / rect.width;
    const scaleY = imgRef.current.naturalHeight / rect.height;
    const realX = Math.round(x * scaleX);
    const realY = Math.round(y * scaleY);

    // 查找点击的元素
    for (const elem of elements) {
      const [bx, by, bw, bh] = elem.bbox;
      if (realX >= bx && realX <= bx + bw && realY >= by && realY <= by + bh) {
        setSelectedId(elem.id);
        return;
      }
    }
    setSelectedId(null);
  };

  const handleImageMouseMove = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!imgRef.current) return;
    const rect = imgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const scaleX = imgRef.current.naturalWidth / rect.width;
    const scaleY = imgRef.current.naturalHeight / rect.height;
    const realX = Math.round(x * scaleX);
    const realY = Math.round(y * scaleY);

    for (const elem of elements) {
      const [bx, by, bw, bh] = elem.bbox;
      if (realX >= bx && realX <= bx + bw && realY >= by && realY <= by + bh) {
        setHoverId(elem.id);
        return;
      }
    }
    setHoverId(null);
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
      padding: "40px 20px",
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    }}>
      <div style={{ maxWidth: 1400, margin: "0 auto" }}>
        {/* Header */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: "24px 32px",
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          marginBottom: 24
        }}>
          <h1 style={{
            fontSize: 32,
            fontWeight: 700,
            background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
            marginBottom: 8
          }}>
            GUIAgent v4.0
          </h1>
          <p style={{ color: "#666", fontSize: 14 }}>统一任务执行 · 智能反思验证 · 自动数据提取</p>
        </div>

        {/* Task Input Card */}
        <div style={{
          background: "rgba(255, 255, 255, 0.95)",
          backdropFilter: "blur(10px)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)",
          marginBottom: 24
        }}>
          <label style={{
            display: "block",
            fontWeight: 600,
            marginBottom: 12,
            fontSize: 15,
            color: "#333"
          }}>
            📝 任务描述
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
            placeholder="例如：打开百度，搜索Python，提取前5个结果"
          />

          {/* List Only Checkbox */}
          <div style={{
            marginTop: 12,
            display: "flex",
            alignItems: "center",
            gap: 8
          }}>
            <input
              type="checkbox"
              id="listOnly"
              checked={listOnly}
              onChange={(e) => setListOnly(e.target.checked)}
              style={{
                width: 18,
                height: 18,
                cursor: "pointer"
              }}
            />
            <label
              htmlFor="listOnly"
              style={{
                fontSize: 14,
                color: "#666",
                cursor: "pointer",
                userSelect: "none"
              }}
            >
              仅提取列表（不进入详情页）
            </label>
          </div>

          {/* Action Buttons */}
          <div style={{
            display: "flex",
            gap: 12,
            alignItems: "center",
            flexWrap: "wrap",
            marginTop: 16
          }}>
            <button
              onClick={handleRunTask}
              disabled={loading}
              style={{
                padding: "12px 24px",
                fontSize: 16,
                fontWeight: 600,
                cursor: loading ? "not-allowed" : "pointer",
                border: "none",
                borderRadius: 8,
                background: loading ? "#ccc" : "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
                color: "white",
                boxShadow: loading ? "none" : "0 4px 15px rgba(102, 126, 234, 0.4)",
                transition: "all 0.3s ease",
                transform: loading ? "none" : "translateY(0)"
              }}
              onMouseEnter={(e) => !loading && (e.currentTarget.style.transform = "translateY(-2px)")}
              onMouseLeave={(e) => !loading && (e.currentTarget.style.transform = "translateY(0)")}
            >
              {loading ? "⏳ 执行中..." : "🚀 执行任务"}
            </button>

            <button
              onClick={handleRefreshFiles}
              disabled={loading}
              style={{
                padding: "10px 20px",
                fontSize: 14,
                cursor: loading ? "not-allowed" : "pointer",
                border: "2px solid #e0e0e0",
                borderRadius: 6,
                background: "white",
                color: "#333"
              }}
            >
              🔄 刷新文件
            </button>
          </div>

          {/* Status Bar */}
          {status && (
            <div style={{
              marginTop: 16,
              padding: 12,
              background: "#f5f5f5",
              borderRadius: 8,
              fontSize: 14,
              color: "#333"
            }}>
              {status}
            </div>
          )}

          {/* Current URL */}
          {currentUrl && (
            <div style={{
              marginTop: 12,
              padding: 8,
              background: "#f0f0f0",
              borderRadius: 6,
              fontSize: 12,
              color: "#666",
              wordBreak: "break-all"
            }}>
              🌐 {currentUrl}
            </div>
          )}
        </div>

        {/* Main Content Grid */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "1fr 400px",
          gap: 24
        }}>
          {/* Left: Screenshot */}
          <div style={{
            background: "rgba(255, 255, 255, 0.95)",
            backdropFilter: "blur(10px)",
            borderRadius: 16,
            padding: 24,
            boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)"
          }}>
            <h2 style={{
              fontSize: 18,
              fontWeight: 600,
              marginBottom: 16,
              color: "#333"
            }}>
              📸 页面截图
            </h2>
            {imgSrc ? (
              <img
                ref={imgRef}
                src={imgSrc}
                alt="annotated"
                onClick={handleImageClick}
                onMouseMove={handleImageMouseMove}
                onMouseLeave={() => setHoverId(null)}
                style={{
                  width: "100%",
                  borderRadius: 8,
                  cursor: "crosshair",
                  border: "2px solid #e0e0e0"
                }}
              />
            ) : (
              <div style={{
                padding: 60,
                textAlign: "center",
                color: "#999",
                background: "#f5f5f5",
                borderRadius: 12,
                border: "2px dashed #e0e0e0"
              }}>
                <div style={{ fontSize: 48, marginBottom: 16 }}>📸</div>
                <div>点击"执行任务"开始</div>
              </div>
            )}
          </div>

          {/* Right: Results & Files */}
          <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
            {/* Task Result */}
            {taskResult && (
              <div style={{
                background: "rgba(255, 255, 255, 0.95)",
                backdropFilter: "blur(10px)",
                borderRadius: 16,
                padding: 24,
                boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)"
              }}>
                <h2 style={{
                  fontSize: 18,
                  fontWeight: 600,
                  marginBottom: 16,
                  color: "#333"
                }}>
                  📊 执行结果
                </h2>

                {/* Status */}
                <div style={{
                  padding: 12,
                  background: taskResult.status === "success" ? "#e8f5e9" :
                             taskResult.status === "terminated" ? "#fff3e0" :
                             taskResult.status === "running" ? "#e3f2fd" : "#ffebee",
                  borderRadius: 8,
                  marginBottom: 16,
                  fontSize: 14,
                  fontWeight: 600,
                  color: taskResult.status === "success" ? "#2e7d32" :
                         taskResult.status === "terminated" ? "#f57c00" :
                         taskResult.status === "running" ? "#1565c0" : "#c62828"
                }}>
                  {taskResult.status === "success" ? "✅ 成功" :
                   taskResult.status === "terminated" ? "⚠️ 终止" :
                   taskResult.status === "running" ? "⏳ 进行中" : "❌ 失败"}
                </div>

                {/* Plan */}
                {taskResult.plan && taskResult.plan.length > 0 && (
                  <div style={{ marginBottom: 16 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, color: "#666" }}>
                      📋 规划步骤 ({taskResult.plan.length})
                    </div>
                    <div style={{
                      padding: 12,
                      background: "#f0f7ff",
                      borderRadius: 8,
                      fontSize: 12,
                      maxHeight: 150,
                      overflowY: "auto"
                    }}>
                      {taskResult.plan.map((step, idx) => (
                        <div key={idx} style={{
                          marginBottom: 6,
                          paddingLeft: 8,
                          borderLeft: "3px solid #2196f3"
                        }}>
                          {idx + 1}. {step}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Steps - 流水线化显示 */}
                {taskResult.steps && taskResult.steps.length > 0 && (
                  <div style={{ marginBottom: 16 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, color: "#666" }}>
                      🔄 执行步骤 ({taskResult.steps.length})
                    </div>
                    <div style={{ maxHeight: 300, overflowY: "auto" }}>
                      {taskResult.steps.map((step, idx) => {
                        const isSuccess = step.status === "success";
                        const isFailed = step.status === "failed";
                        const isTerminated = step.status === "terminated";
                        const hasRetry = step.retry_index > 0;

                        return (
                          <div key={idx} style={{
                            marginBottom: 12,
                            position: "relative"
                          }}>
                            {/* 连接线 */}
                            {idx < taskResult.steps.length - 1 && (
                              <div style={{
                                position: "absolute",
                                left: 12,
                                top: 40,
                                width: 2,
                                height: "calc(100% + 12px)",
                                background: "#e0e0e0"
                              }} />
                            )}

                            <div style={{
                              padding: 12,
                              background: isSuccess ? "#f1f8f4" :
                                         isTerminated ? "#fff3e0" : "#fff5f5",
                              borderRadius: 8,
                              border: `2px solid ${isSuccess ? "#4caf50" :
                                                   isTerminated ? "#ff9800" : "#f44336"}`,
                              position: "relative"
                            }}>
                              {/* 步骤标题 */}
                              <div style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                marginBottom: 8
                              }}>
                                <div style={{
                                  width: 24,
                                  height: 24,
                                  borderRadius: "50%",
                                  background: isSuccess ? "#4caf50" :
                                             isTerminated ? "#ff9800" : "#f44336",
                                  color: "white",
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  fontSize: 12,
                                  fontWeight: 600,
                                  flexShrink: 0
                                }}>
                                  {step.step_index + 1}
                                </div>
                                <div style={{
                                  fontWeight: 600,
                                  fontSize: 13,
                                  flex: 1
                                }}>
                                  {step.description}
                                </div>
                                <div style={{
                                  fontSize: 11,
                                  padding: "2px 8px",
                                  borderRadius: 4,
                                  background: isSuccess ? "#4caf50" :
                                             isTerminated ? "#ff9800" : "#f44336",
                                  color: "white"
                                }}>
                                  {isSuccess ? "✓ 成功" :
                                   isTerminated ? "⚠ 终止" : "✗ 失败"}
                                </div>
                              </div>

                              {/* 动作 */}
                              <div style={{
                                fontSize: 11,
                                color: "#666",
                                marginBottom: 6,
                                paddingLeft: 32
                              }}>
                                🎯 动作: {step.action}
                              </div>

                              {/* 重试标记 */}
                              {hasRetry && (
                                <div style={{
                                  fontSize: 11,
                                  color: "#ff9800",
                                  marginBottom: 6,
                                  paddingLeft: 32,
                                  display: "flex",
                                  alignItems: "center",
                                  gap: 4
                                }}>
                                  ↻ 重试 {step.retry_index} 次
                                </div>
                              )}

                              {/* 反思结果 */}
                              {step.verification && (
                                <div style={{
                                  fontSize: 11,
                                  color: "#666",
                                  paddingLeft: 32,
                                  marginTop: 6,
                                  paddingTop: 6,
                                  borderTop: "1px solid #e0e0e0"
                                }}>
                                  💭 反思: {step.verification.reasoning || "无"}
                                </div>
                              )}

                              {/* 终止原因 */}
                              {step.user_message && (
                                <div style={{
                                  fontSize: 11,
                                  color: "#f57c00",
                                  paddingLeft: 32,
                                  marginTop: 6,
                                  fontWeight: 600
                                }}>
                                  ⚠️ {step.user_message}
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* Extracted Items */}
                {taskResult.extracted_items && taskResult.extracted_items.length > 0 && (
                  <div style={{ marginBottom: 16 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, color: "#666" }}>
                      提取数据 ({taskResult.extracted_items.length} 条)
                    </div>
                    <div style={{
                      padding: 12,
                      background: "#f5f5f5",
                      borderRadius: 8,
                      fontSize: 12,
                      maxHeight: 150,
                      overflowY: "auto"
                    }}>
                      {taskResult.extracted_items.slice(0, 3).map((item, idx) => (
                        <div key={idx} style={{ marginBottom: 8 }}>
                          {JSON.stringify(item, null, 2)}
                        </div>
                      ))}
                      {taskResult.extracted_items.length > 3 && (
                        <div style={{ color: "#999" }}>
                          ... 还有 {taskResult.extracted_items.length - 3} 条
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* Excel File */}
                {taskResult.excel_file && (
                  <div style={{
                    padding: 12,
                    background: "#e3f2fd",
                    borderRadius: 8,
                    fontSize: 14
                  }}>
                    📄 Excel: {taskResult.excel_file}
                  </div>
                )}
              </div>
            )}

            {/* Files */}
            <div style={{
              background: "rgba(255, 255, 255, 0.95)",
              backdropFilter: "blur(10px)",
              borderRadius: 16,
              padding: 24,
              boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)"
            }}>
              <h2 style={{
                fontSize: 18,
                fontWeight: 600,
                marginBottom: 16,
                color: "#333"
              }}>
                📁 输出文件
              </h2>
              {files.length === 0 ? (
                <div style={{
                  padding: 40,
                  textAlign: "center",
                  color: "#999",
                  background: "#f5f5f5",
                  borderRadius: 12,
                  border: "2px dashed #e0e0e0"
                }}>
                  暂无文件
                </div>
              ) : (
                <div style={{ display: "grid", gap: 8 }}>
                  {files.map((file) => (
                    <a
                      key={file}
                      href={`${API_BASE}/files/${file}`}
                      download
                      style={{
                        padding: 12,
                        background: "#f5f5f5",
                        borderRadius: 8,
                        textDecoration: "none",
                        color: "#333",
                        fontSize: 14,
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        transition: "all 0.2s"
                      }}
                      onMouseEnter={(e) => e.currentTarget.style.background = "#e0e0e0"}
                      onMouseLeave={(e) => e.currentTarget.style.background = "#f5f5f5"}
                    >
                      📄 {file}
                    </a>
                  ))}
                </div>
              )}
            </div>

            {/* Elements */}
            {elements.length > 0 && (
              <div style={{
                background: "rgba(255, 255, 255, 0.95)",
                backdropFilter: "blur(10px)",
                borderRadius: 16,
                padding: 24,
                boxShadow: "0 8px 32px rgba(0, 0, 0, 0.1)"
              }}>
                <h2 style={{
                  fontSize: 18,
                  fontWeight: 600,
                  marginBottom: 16,
                  color: "#333"
                }}>
                  🎯 页面元素 ({elements.length})
                </h2>
                <div style={{ maxHeight: 300, overflowY: "auto" }}>
                  {elements.map((elem) => (
                    <div
                      key={elem.id}
                      style={{
                        padding: 8,
                        background: elem.id === selectedId ? "#e3f2fd" :
                                   elem.id === hoverId ? "#f5f5f5" : "white",
                        borderRadius: 6,
                        marginBottom: 8,
                        fontSize: 12,
                        cursor: "pointer",
                        border: elem.id === selectedId ? "2px solid #2196f3" : "1px solid #e0e0e0"
                      }}
                      onClick={() => setSelectedId(elem.id)}
                    >
                      <div style={{ fontWeight: 600, marginBottom: 4 }}>
                        #{elem.id} - {elem.type}
                      </div>
                      <div style={{ color: "#666" }}>{elem.content}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;

(() => {
  "use strict";
  const moduleId = "builtin.desktop-mirror";
  const $ = (id) => document.getElementById(id);
  let state = null;
  let previewUrl = null;
  let previewTimer = null;
  let updateTimer = null;
  let moduleVisible = true;
  let previewLastFrameId = null;
  let previewFpsStarted = performance.now();
  let previewFrames = 0;
  let previewMeasuredFps = 0;
  const pendingControls = new Set();

  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `HTTP ${response.status}`);
    }
    return response.json();
  }

  function payload() {
    return {
      monitor_id: $("monitor").value,
      target_fps: Number($("target-fps").value),
      transport_mode: $("transport-mode").value,
      jpeg_quality: Number($("jpeg-quality").value),
      jpeg_subsampling: $("subsampling").value,
      indexed_bpp: Number($("indexed-bpp").value),
      cursor: $("cursor-toggle").getAttribute("aria-checked") === "true",
    };
  }

  function syncControl(id, value) {
    const control = $(id);
    if (document.activeElement === control || pendingControls.has(id)) return;
    control.value = String(value);
  }

  async function invoke(action, data = {}) {
    const envelope = await api(`/api/modules/${moduleId}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({ payload: data }),
    });
    state = envelope.result;
    render();
  }

  async function startMirror() {
    await invoke("start", payload());
    await api(`/api/modules/${moduleId}/activate`, {
      method: "POST",
      body: JSON.stringify({ target: "esp32" }),
    });
  }

  function render() {
    if (!state) return;
    const labels = { stopped: "已停止", ready: "待命", paused: "已暂停", starting: "启动中", capturing: "正在镜像", error: "捕获失败", unavailable: "不可用" };
    $("status").textContent = labels[state.status] || state.status;
    $("capture-toggle").textContent = state.capture_active || state.status === "starting" ? "停止镜像" : "开始镜像";
    $("capture-toggle").classList.toggle("primary", !state.capture_active && state.status !== "starting");
    $("backend").textContent = `CAPTURE · ${String(state.backend || "--").toUpperCase()}`;
    $("frame-count").textContent = `FRAME ${state.frames_captured || 0}`;
    $("capture-fps").textContent = Number(state.capture_fps || 0).toFixed(1);
    $("captured").textContent = state.frames_captured || 0;
    syncControl("target-fps", state.target_fps);
    syncControl("transport-mode", state.transport_mode || "mjpeg");
    syncControl("jpeg-quality", state.jpeg_quality);
    syncControl("subsampling", state.jpeg_subsampling);
    syncControl("indexed-bpp", state.indexed_bpp || 8);
    const indexed = state.transport_mode === "indexed";
    $("mjpeg-settings").hidden = indexed;
    $("indexed-settings").hidden = !indexed;
    $("transport-label").textContent = indexed ? `I${state.indexed_bpp || 8} LZ4` : "MJPEG";
    $("cursor-toggle").setAttribute("aria-checked", state.cursor ? "true" : "false");
    const monitor = $("monitor");
    const signature = JSON.stringify(state.monitors || []);
    if (monitor.dataset.signature !== signature) {
      monitor.replaceChildren(...(state.monitors || []).map((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.name}${item.primary ? " · 主显示器" : ""}`;
        return option;
      }));
      monitor.dataset.signature = signature;
    }
    monitor.value = state.monitor_id;
    $("error").hidden = !state.error;
    $("error").textContent = state.error || "";
  }

  async function refresh() {
    try {
      const envelope = await api(`/api/modules/${moduleId}/state`);
      state = envelope.state;
      render();
    } catch (error) {
      $("status").textContent = "Host 离线";
      $("error").hidden = false;
      $("error").textContent = error.message;
    }
  }

  async function refreshPreview() {
    if (!moduleVisible) return;
    try {
      const indexed = state?.transport_mode === "indexed";
      const endpoint = indexed ? "/api/preview.png" : "/api/display/preview.jpg";
      const response = await fetch(`${endpoint}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const frameId = response.headers.get("X-Frame-Id");
      const url = URL.createObjectURL(await response.blob());
      $("preview").src = url;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = url;
      if (frameId !== null && frameId !== previewLastFrameId) {
        previewLastFrameId = frameId;
        previewFrames += 1;
      }
      const now = performance.now();
      const fpsElapsed = now - previewFpsStarted;
      if (fpsElapsed >= 1000) {
        previewMeasuredFps = previewFrames * 1000 / fpsElapsed;
        previewFrames = 0;
        previewFpsStarted = now;
      }
      $("preview-fps").textContent = `${previewMeasuredFps.toFixed(1)} FPS`;
    } catch (_) {
      $("preview-fps").textContent = "WAITING";
    } finally {
      if (moduleVisible) {
        const target = Number(state?.target_fps || 0);
        const active = state?.capture_active || state?.status === "starting";
        const delay = active ? (target > 0 ? Math.max(0, 1000 / target) : 16) : 250;
        previewTimer = window.setTimeout(refreshPreview, delay);
      }
    }
  }

  function queueConfigure(event) {
    const control = event?.currentTarget;
    if (control && !control.checkValidity()) return;
    if (control?.id) pendingControls.add(control.id);
    clearTimeout(updateTimer);
    updateTimer = window.setTimeout(async () => {
      try {
        await invoke("configure", payload());
      } catch (error) {
        $("error").hidden = false;
        $("error").textContent = error.message;
      } finally {
        pendingControls.clear();
        render();
      }
    }, 250);
  }

  $("capture-toggle").addEventListener("click", () => {
    const active = state?.capture_active || state?.status === "starting";
    const operation = active ? invoke("stop") : startMirror();
    operation.catch((error) => {
      $("error").hidden = false;
      $("error").textContent = error.message;
    });
  });
  $("monitor").addEventListener("change", queueConfigure);
  $("target-fps").addEventListener("change", queueConfigure);
  $("transport-mode").addEventListener("change", queueConfigure);
  $("jpeg-quality").addEventListener("change", queueConfigure);
  $("subsampling").addEventListener("change", queueConfigure);
  $("indexed-bpp").addEventListener("change", queueConfigure);
  $("cursor-toggle").addEventListener("click", () => {
    const enabled = $("cursor-toggle").getAttribute("aria-checked") !== "true";
    $("cursor-toggle").setAttribute("aria-checked", enabled ? "true" : "false");
    queueConfigure();
  });
  $("preview").addEventListener("load", () => {
    const landscape = $("preview").naturalWidth > $("preview").naturalHeight;
    $("preview").classList.toggle("is-landscape", landscape);
    $("output-size").textContent = landscape ? "320 × 240" : "240 × 320";
  });
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.type !== "espagent:module-visibility") return;
    moduleVisible = Boolean(event.data.active);
    clearTimeout(previewTimer);
    if (moduleVisible) {
      refreshPreview();
    } else if (state?.capture_active || state?.status === "starting") {
      invoke("stop").catch(() => {});
    }
  });
  refresh();
  refreshPreview();
  window.setInterval(refresh, 750);
  window.addEventListener("beforeunload", () => {
    clearTimeout(previewTimer);
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  });
})();

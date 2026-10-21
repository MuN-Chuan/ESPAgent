(() => {
  "use strict";
  const registry = window.ESPAgentThemes = window.ESPAgentThemes || {};
  const audioLabels = { ready: "就绪", pending: "连接中", generating: "生成中", playing: "播放中", interrupted: "已打断", failed: "不可用", disabled: "静音" };
  const hermesLabels = { thinking: "在线 · 正在思考", replying: "在线 · 正在回复", error: "在线 · 需要留意", idle: "在线 · 待命" };
  const pages = { hermes: "AI ASSIST", lights: "LIVING SPACE", link: "DISPLAY LINK", codex: "CODEX USAGE" };
  let previousReply = null;
  let previousBrightness = null;

  function quotaWindowLabel(value) {
    const minutes = Number(value?.window_duration_minutes);
    if (!minutes) return "--";
    if (minutes % 1440 === 0) return `${minutes / 1440} 天`;
    if (minutes % 60 === 0) return `${minutes / 60} 小时`;
    return `${minutes} 分钟`;
  }

  function flash(node) {
    if (!node || node.closest("#display")?.dataset.motion === "off") return;
    node.classList.remove("f2-change");
    void node.offsetWidth;
    node.classList.add("f2-change");
  }

  function renderImeCandidates(node, hermes) {
    if (!node) return;
    const candidates = Array.isArray(hermes.input_candidates) ? hermes.input_candidates : [];
    const selected = candidates.length
      ? ((Number(hermes.input_candidate_index) || 0) % candidates.length + candidates.length) % candidates.length
      : 0;
    const signature = JSON.stringify(candidates);
    if (node.dataset.candidateSignature !== signature) {
      node.replaceChildren(...candidates.map((candidate, index) => {
        const item = document.createElement("span");
        item.className = "f2-ime-candidate";
        item.textContent = `${index + 1} ${candidate}`;
        return item;
      }));
      node.dataset.candidateSignature = signature;
      node.scrollLeft = 0;
    }
    const items = Array.from(node.children);
    items.forEach((item, index) => item.classList.toggle("is-selected", index === selected));
    const selectedItem = items[selected];
    if (!selectedItem) return;
    const itemLeft = selectedItem.offsetLeft;
    const itemRight = itemLeft + selectedItem.offsetWidth;
    if (itemLeft < node.scrollLeft) node.scrollLeft = itemLeft;
    else if (itemRight > node.scrollLeft + node.clientWidth) node.scrollLeft = itemRight - node.clientWidth;
  }

  registry.second = {
    label: "第二套 · Editorial",
    markup: `
      <header class="f2-topline"><span class="f2-product-mark">ESP—A</span><span id="page-title">AI ASSIST</span></header>
      <section class="f2-scene is-active" data-scene="hermes">
        <header class="f2-scene-head"><div><span class="f2-micro-label">STATUS</span><strong id="hermes-status">在线 · 待命</strong></div><span class="f2-folio">01</span></header>
        <div class="f2-reply" id="hermes-reply">
          <div class="f2-chat f2-chat-user" id="hermes-user" hidden></div>
          <div class="f2-chat f2-chat-assistant" id="hermes-assistant">等待 Hermes 回复</div>
        </div>
        <div class="f2-ime" id="hermes-ime" hidden>
          <div class="f2-ime-entry"><span id="hermes-input-before"></span><span class="f2-ime-preedit" id="hermes-input-preedit"></span><i class="f2-ime-caret" aria-hidden="true"></i><span id="hermes-input-after"></span></div>
          <div class="f2-ime-candidates" id="hermes-input-candidates"></div>
        </div>
        <div class="f2-system-strip"><span><i>VOICE</i><b id="audio-status">静音</b></span><span><i>DISPLAY</i><b id="screen-status">离线</b></span></div>
      </section>
      <section class="f2-scene" data-scene="lights">
        <header class="f2-scene-head"><div><span class="f2-micro-label">DEVICE</span><strong id="light-name">客厅主灯</strong></div><span class="f2-folio">02</span></header>
        <div class="f2-light-mode"><span class="f2-section-label">POWER</span><strong id="light-state">已关闭</strong></div>
        <div class="f2-brightness-reading"><strong id="brightness">00</strong><span>%</span></div>
        <div class="f2-level-row"><span>亮度</span><div class="f2-segments" id="brightness-segments" aria-label="亮度"></div></div>
        <div class="f2-system-strip f2-single"><span><i>HOME ASSISTANT</i><b id="light-footer">离线</b></span></div>
      </section>
      <section class="f2-scene" data-scene="link">
        <header class="f2-scene-head"><div><span class="f2-micro-label">CONNECTION</span><strong id="link-status">等待设备</strong></div><span class="f2-folio">03</span></header>
        <div class="f2-fps-reading"><strong id="display-fps">0.0</strong><span>FPS<br><small>LCD REFRESH</small></span></div>
        <div class="f2-metrics">
          <div class="f2-metric"><span>发送帧</span><strong id="frame-id">0</strong></div><div class="f2-metric"><span>脏块</span><strong id="dirty-tiles">0</strong></div>
          <div class="f2-metric f2-wide"><span>接收 / 通过</span><strong id="packet-count">0/0</strong></div><div class="f2-metric"><span>异常</span><strong id="error-count">0</strong></div>
        </div>
        <div class="f2-link-signature" id="link-footer">MJPEG · --</div>
      </section>
      <section class="f2-scene" data-scene="codex">
        <header class="f2-scene-head"><div><span class="f2-micro-label">ACCOUNT</span></div><span class="f2-folio">04</span></header>
        <div class="f2-fps-reading"><strong id="codex-primary">--</strong><span class="f2-quota-unit">%</span><span class="f2-chatgpt-watermark" aria-hidden="true"></span></div>
        <div class="f2-metrics">
          <div class="f2-metric"><span>额度周期</span><strong id="codex-window">--</strong></div><div class="f2-metric"><span>额度组</span><strong id="codex-limit">--</strong></div>
          <div class="f2-metric f2-wide"><span>计划</span><strong id="codex-detail">--</strong></div>
        </div>
        <div class="f2-link-signature">CHATGPT · CODEX</div>
      </section>
      <nav class="f2-scene-nav" aria-label="功能页"><span data-nav="hermes">AI</span><span data-nav="lights">HOME</span><span data-nav="link">LINK</span><span data-nav="codex">CODEX</span></nav>`,
    mount(root, ui) {
      previousReply = null;
      previousBrightness = null;
      ui.segments("brightness-segments", 0, "f2-segment");
    },
    render(state, ui) {
      const scene = pages[state.active_scene] ? state.active_scene : "hermes";
      const hermes = state.hermes || {};
      const light = state.light || {};
      const link = state.link || {};
      const codex = state.modules?.["builtin.codex-usage"] || {};
      const reply = hermes.last_reply?.trim() || hermes.interim_message?.trim() || "等待 Hermes 回复";
      const replyChanged = previousReply !== reply;
      const replyContinues = previousReply !== null && reply.length >= previousReply.length && reply.startsWith(previousReply);
      const inputActive = Boolean(hermes.input_active);
      const draft = String(hermes.input_draft || "");
      const caret = Math.max(0, Math.min(draft.length, Number(hermes.input_caret) || 0));
      const preedit = String(hermes.input_preedit || "");
      const brightness = Math.max(0, Math.min(100, Number(light.brightness) || 0));

      ui.setActiveScene(scene, "f2");
      document.querySelectorAll("[data-nav]").forEach((node) => node.classList.toggle("is-active", node.dataset.nav === scene));
      ui.text("page-title", pages[scene]);
      ui.text("hermes-status", hermes.online ? (hermes.activity ? `在线 · ${hermes.activity}` : (hermesLabels[hermes.status] || hermesLabels.idle)) : "离线 · 等待连接");
      ui.text("hermes-assistant", reply);
      const userNode = ui.byId("hermes-user");
      const userMessage = String(hermes.last_user_message || "").trim();
      userNode.hidden = !userMessage;
      if (userMessage) ui.text("hermes-user", userMessage);
      ui.byId("hermes-ime").hidden = !inputActive;
      ui.byId("hermes-reply").classList.toggle("has-ime", inputActive);
      const placeholder = inputActive && !draft && !preedit;
      const afterNode = ui.byId("hermes-input-after");
      afterNode.classList.toggle("is-placeholder", placeholder);
      ui.text("hermes-input-before", placeholder ? "" : draft.slice(0, caret));
      ui.text("hermes-input-preedit", preedit);
      ui.text("hermes-input-after", placeholder ? "请输入" : draft.slice(caret));
      renderImeCandidates(ui.byId("hermes-input-candidates"), hermes);
      ui.text("audio-status", audioLabels[hermes.audio_status] || hermes.audio_status || "静音");
      ui.text("screen-status", link.esp32_online ? "在线" : "离线");
      ui.fitText("hermes-assistant", document.getElementById("display").dataset.fontChinese === "pixel" ? [18, 16, 14] : [20, 18, 16, 14]);
      const replyNode = ui.byId("hermes-reply");
      if (replyChanged && !replyContinues) replyNode.scrollTop = 0;
      const remoteScrollDelta = ui.remoteScrollDelta(state, replyNode);
      if (scene === "hermes" && remoteScrollDelta !== null) {
        const maximum = Math.max(0, replyNode.scrollHeight - replyNode.clientHeight);
        replyNode.scrollTop = Math.max(0, Math.min(maximum, replyNode.scrollTop + remoteScrollDelta));
      }
      ui.text("light-name", light.name || "灯具");
      ui.text("light-state", light.on ? "已开启" : "已关闭");
      ui.text("brightness", String(Math.round(brightness)).padStart(2, "0"));
      ui.segments("brightness-segments", brightness, "f2-segment");
      ui.text("light-footer", light.error || (light.online ? "已同步" : "离线"));
      ui.text("link-status", link.esp32_online ? "链路在线" : "等待设备");
      ui.text("display-fps", Number(link.display_fps || 0).toFixed(1));
      ui.text("frame-id", link.frame_id || 0);
      ui.text("dirty-tiles", link.dirty_tiles || 0);
      ui.text("packet-count", `${link.rx_packets || 0}/${link.accepted_packets || 0}`);
      ui.text("error-count", Number(link.rejected_packets || 0) + Number(link.draw_failed_packets || 0));
      ui.text("link-footer", `${String(link.transport_mode || "mjpeg").toUpperCase()}  ·  ${link.esp32_ip || "--"}`);
      const quota = codex.quota || codex.primary || codex.secondary || {};
      ui.text("codex-primary", quota.remaining_percent ?? "--");
      ui.text("codex-window", quotaWindowLabel(quota));
      ui.text("codex-limit", codex.limit_name || codex.limit_id || "--");
      ui.text("codex-detail", String(codex.plan_type || "--").toUpperCase());

      if (previousReply !== null && previousReply !== reply) flash(ui.byId("hermes-reply"));
      if (previousBrightness !== null && previousBrightness !== brightness) flash(ui.byId("brightness"));
      previousReply = reply;
      previousBrightness = brightness;
    }
  };
})();

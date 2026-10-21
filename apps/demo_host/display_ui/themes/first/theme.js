(() => {
  "use strict";
  const registry = window.ESPAgentThemes = window.ESPAgentThemes || {};
  const audioLabels = { ready: "就绪", pending: "连接中", generating: "生成中", playing: "播放中", interrupted: "已打断", failed: "不可用", disabled: "静音" };
  const hermesLabels = { thinking: "在线 · 正在思考", replying: "在线 · 正在回复", error: "在线 · 需要留意", idle: "在线 · 待命" };
  const pages = {
    hermes: { title: "HERMES", index: "01 / 04" },
    lights: { title: "HOME", index: "02 / 04" },
    link: { title: "DISPLAY", index: "03 / 04" },
    codex: { title: "CODEX", index: "04 / 04" }
  };
  const sceneOrder = ["hermes", "lights", "link", "codex"];
  let activeScene = "hermes";
  let runningAnimations = [];
  let previousReply = null;
  let previousBrightness = null;
  let replyScrollMeasureFrame = null;
  let replyScrollFrame = null;
  let replyScrollLastTime = 0;
  let replyScrollFinishing = false;
  let manualReplyScroll = false;

  function quotaWindowLabel(value) {
    const minutes = Number(value?.window_duration_minutes);
    if (!minutes) return "--";
    if (minutes % 1440 === 0) return `${minutes / 1440} 天`;
    if (minutes % 60 === 0) return `${minutes / 60} 小时`;
    return `${minutes} 分钟`;
  }

  function quotaResetLabel(value) {
    if (!value) return "--";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? "--" : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  }

  function motionAllowed(root) {
    return root.dataset.motion !== "off" && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function track(animation) {
    runningAnimations.push(animation);
    animation.finished.catch(() => {}).finally(() => {
      runningAnimations = runningAnimations.filter((item) => item !== animation);
    });
    return animation;
  }

  function cancelAnimations(root) {
    runningAnimations.forEach((animation) => animation.cancel());
    runningAnimations = [];
    root.querySelectorAll(".f1-is-leaving, .f1-is-transitioning").forEach((node) => {
      node.classList.remove("f1-is-leaving", "f1-is-transitioning");
    });
  }

  function transitionScene(scene, root) {
    if (scene === activeScene) return;
    const next = root.querySelector(`.f1-scene[data-scene="${scene}"]`);
    if (!next) return;

    cancelAnimations(root);
    const direction = sceneOrder.indexOf(scene) > sceneOrder.indexOf(activeScene) ? 1 : -1;
    root.querySelectorAll(".f1-scene").forEach((node) => node.classList.remove("is-active"));
    next.classList.add("is-active", "f1-is-transitioning");
    activeScene = scene;

    if (!motionAllowed(root)) {
      next.classList.remove("f1-is-transitioning");
      return;
    }

    const animations = [];
    const content = next.querySelectorAll(".f1-motion-item");
    content.forEach((node, index) => {
      animations.push(track(node.animate(
        [
          { transform: `translate3d(${direction * 7}px,5px,0)` },
          { transform: "translate3d(0,0,0)" }
        ],
        {
          duration: 280,
          delay: 45 + index * 32,
          easing: "cubic-bezier(.22,1,.36,1)",
          fill: "both"
        }
      )));
    });

    const rule = root.querySelector(".f1-motion-rule");
    if (rule) {
      const ruleDistance = rule.offsetWidth;
      animations.push(track(rule.animate(
        [
          { transform: `translate3d(${direction > 0 ? -ruleDistance : ruleDistance}px,0,0) scaleX(.2)` },
          { transform: "translate3d(0,0,0) scaleX(1)" }
        ],
        { duration: 360, easing: "cubic-bezier(.22,1,.36,1)", fill: "both" }
      )));
    }

    Promise.all(animations.map((animation) => animation.finished.catch(() => {}))).then(() => {
      next.classList.remove("f1-is-transitioning");
      animations.forEach((animation) => animation.cancel());
    });
  }

  function emphasize(node, kind) {
    const root = node?.closest("#display");
    if (!node || !root || !motionAllowed(root)) return;
    const frames = kind === "number"
      ? [{ transform: "scale3d(.94,.94,1) translate3d(0,4px,0)" }, { transform: "scale3d(1,1,1) translate3d(0,0,0)" }]
      : [{ transform: "translate3d(0,6px,0)" }, { transform: "translate3d(0,0,0)" }];
    track(node.animate(frames, { duration: 260, easing: "cubic-bezier(.22,1,.36,1)" }));
  }

  function stopReplyAutoScroll(node, reset = false) {
    if (replyScrollMeasureFrame !== null) cancelAnimationFrame(replyScrollMeasureFrame);
    if (replyScrollFrame !== null) cancelAnimationFrame(replyScrollFrame);
    replyScrollMeasureFrame = null;
    replyScrollFrame = null;
    replyScrollLastTime = 0;
    replyScrollFinishing = false;
    if (reset && node) node.scrollTop = 0;
  }

  function advanceReplyAutoScroll(node, timestamp) {
    if (!node?.isConnected) {
      stopReplyAutoScroll();
      return;
    }
    const target = Math.max(0, node.scrollHeight - node.clientHeight);
    const distance = target - node.scrollTop;
    if (distance <= 0.5) {
      node.scrollTop = target;
      stopReplyAutoScroll();
      return;
    }

    const elapsed = replyScrollLastTime ? Math.min(64, timestamp - replyScrollLastTime) : 16;
    replyScrollLastTime = timestamp;
    const speed = replyScrollFinishing
      ? Math.max(150, Math.min(360, distance * 3.5))
      : Math.max(38, Math.min(90, distance * 1.25));
    node.scrollTop = Math.min(target, node.scrollTop + Math.max(0.75, speed * elapsed / 1000));
    replyScrollFrame = requestAnimationFrame((nextTimestamp) => advanceReplyAutoScroll(node, nextTimestamp));
  }

  function scheduleReplyAutoScroll(node, { reset = false, finishing = false } = {}) {
    if (!node) return;
    if (reset) stopReplyAutoScroll(node, true);
    replyScrollFinishing = replyScrollFinishing || finishing;
    if (replyScrollMeasureFrame !== null) cancelAnimationFrame(replyScrollMeasureFrame);
    replyScrollMeasureFrame = requestAnimationFrame(() => {
      replyScrollMeasureFrame = null;
      if (!node.isConnected) return;
      const target = Math.max(0, node.scrollHeight - node.clientHeight);
      if (target <= 0) {
        stopReplyAutoScroll(node, true);
        return;
      }
      const root = node.closest("#display");
      if (!root || !motionAllowed(root)) {
        stopReplyAutoScroll();
        node.scrollTop = target;
        return;
      }
      if (replyScrollFrame === null) {
        replyScrollLastTime = 0;
        replyScrollFrame = requestAnimationFrame((timestamp) => advanceReplyAutoScroll(node, timestamp));
      }
    });
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
        item.className = "f1-ime-candidate";
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

  registry.first = {
    label: "第一套 · Paper",
    markup: `
      <header class="f1-masthead">
        <div class="f1-brand"><span class="f1-brand-mark"></span><span id="page-title">HERMES</span></div>
        <span id="page-index">01 / 03</span>
      </header>
      <span class="f1-motion-rule" aria-hidden="true"></span>
      <section class="f1-scene is-active" data-scene="hermes">
        <p class="f1-status f1-motion-item" id="hermes-status">在线 · 待命</p>
        <div class="f1-reply f1-motion-item" id="hermes-reply">
          <div class="f1-chat f1-chat-user" id="hermes-user" hidden></div>
          <div class="f1-chat f1-chat-assistant" id="hermes-assistant">等待 Hermes 回复</div>
        </div>
        <div class="f1-ime f1-motion-item" id="hermes-ime" hidden>
          <div class="f1-ime-entry">
            <span class="f1-ime-text" id="hermes-input-text"><span id="hermes-input-before"></span><span class="f1-ime-preedit" id="hermes-input-preedit"></span><i class="f1-ime-caret" aria-hidden="true"></i><span id="hermes-input-after"></span></span>
            <span class="f1-ime-voice" id="hermes-input-voice" hidden><span id="hermes-input-voice-label">语音输入中</span><i class="f1-voice-wave" aria-hidden="true"><b></b><b></b><b></b><b></b></i></span>
          </div>
          <div class="f1-ime-candidates" id="hermes-input-candidates"></div>
        </div>
        <footer class="f1-footer f1-footer-split f1-motion-item"><span id="audio-status">语音&nbsp;&nbsp;静音</span><span id="screen-status">屏幕&nbsp;&nbsp;离线</span></footer>
      </section>
      <section class="f1-scene" data-scene="lights">
        <p class="f1-status f1-motion-item" id="light-name">客厅主灯</p>
        <h1 class="f1-light-state f1-motion-item" id="light-state">已关闭</h1>
        <div class="f1-brightness f1-motion-item" id="brightness">00%</div>
        <p class="f1-eyebrow f1-brightness-label f1-motion-item">亮度</p>
        <div class="f1-segments f1-motion-item" id="brightness-segments" aria-label="亮度"></div>
        <p class="f1-eyebrow f1-color-temperature-label f1-motion-item">色温</p>
        <div class="f1-segments f1-motion-item" id="color-temperature-segments" aria-label="色温"></div>
        <footer class="f1-footer f1-motion-item" id="light-footer">HOME ASSISTANT&nbsp;&nbsp;离线</footer>
      </section>
      <section class="f1-scene" data-scene="link">
        <p class="f1-status f1-motion-item" id="link-status">等待设备</p>
        <div class="f1-fps-row f1-motion-item"><strong id="display-fps">0.0</strong><span>LCD FPS</span></div>
        <div class="f1-metrics f1-motion-item">
          <div class="f1-metric"><span>发送帧</span><strong id="frame-id">0</strong></div>
          <div class="f1-metric"><span>脏块</span><strong id="dirty-tiles">0</strong></div>
          <div class="f1-metric"><span>接收 / 通过</span><strong id="packet-count">0/0</strong></div>
          <div class="f1-metric"><span>异常</span><strong id="error-count">0</strong></div>
        </div>
        <footer class="f1-footer f1-motion-item" id="link-footer">MJPEG&nbsp;&nbsp;·&nbsp;&nbsp;--</footer>
      </section>
      <section class="f1-scene" data-scene="codex">
        <div class="f1-quota-reading f1-motion-item"><strong id="codex-primary">--</strong><span class="f1-quota-unit">%</span><span class="f1-quota-watermark" aria-hidden="true"></span></div>
        <div class="f1-metrics f1-motion-item">
          <div class="f1-metric"><span>额度周期</span><strong id="codex-window">--</strong></div>
          <div class="f1-metric"><span>额度组</span><strong id="codex-limit">--</strong></div>
          <div class="f1-metric"><span>计划</span><strong id="codex-plan">--</strong></div>
          <div class="f1-metric"><span>额度重置</span><strong id="codex-reset">--</strong></div>
        </div>
        <footer class="f1-footer f1-motion-item">CHATGPT&nbsp;&nbsp;·&nbsp;&nbsp;CODEX</footer>
      </section>`,
    mount(root, ui) {
      cancelAnimations(root);
      activeScene = root.querySelector(".f1-scene.is-active")?.dataset.scene || "hermes";
      previousReply = null;
      previousBrightness = null;
      manualReplyScroll = false;
      stopReplyAutoScroll(ui.byId("hermes-reply"), true);
      ui.segments("brightness-segments", 0, "f1-segment");
      ui.selectionSegments("color-temperature-segments", null, 2000, 6500, "f1-segment");
    },
    unmount(root) {
      cancelAnimations(root);
      stopReplyAutoScroll(root.querySelector("#hermes-reply"));
    },
    render(state, ui) {
      const scene = pages[state.active_scene] ? state.active_scene : "hermes";
      const hermes = state.hermes || {};
      const light = state.light || {};
      const link = state.link || {};
      const codex = state.modules?.["builtin.codex-usage"] || {};
      const online = Boolean(hermes.online);
      const reply = hermes.last_reply?.trim() || hermes.interim_message?.trim() || "等待 Hermes 回复";
      const replyChanged = previousReply !== reply;
      const replyContinues = previousReply !== null && reply.length >= previousReply.length && reply.startsWith(previousReply);
      const voiceInputActive = Boolean(hermes.input_voice_active);
      const inputActive = Boolean(hermes.input_active) || voiceInputActive;
      const draft = String(hermes.input_draft || "");
      const caret = Math.max(0, Math.min(draft.length, Number(hermes.input_caret) || 0));
      const preedit = String(hermes.input_preedit || "");
      const brightness = Math.max(0, Math.min(100, Number(light.brightness) || 0));
      const colorTemperature = light.color_temp_kelvin;
      const minimumColorTemperature = Number(light.min_color_temp_kelvin) || 2000;
      const maximumColorTemperature = Number(light.max_color_temp_kelvin) || 6500;

      transitionScene(scene, document.getElementById("display"));
      ui.text("page-title", pages[scene].title);
      ui.text("page-index", pages[scene].index);
      ui.text("hermes-status", online ? (hermes.activity ? `在线 · ${hermes.activity}` : (hermesLabels[hermes.status] || hermesLabels.idle)) : "离线 · 等待连接");
      if (replyChanged) ui.text("hermes-assistant", reply);
      const userNode = ui.byId("hermes-user");
      const userMessage = String(hermes.last_user_message || "").trim();
      userNode.hidden = !userMessage;
      if (userMessage) ui.text("hermes-user", userMessage);
      ui.byId("hermes-ime").hidden = !inputActive;
      ui.byId("hermes-reply").classList.toggle("has-ime", inputActive);
      ui.byId("hermes-input-text").hidden = voiceInputActive;
      ui.byId("hermes-input-voice").hidden = !voiceInputActive;
      ui.text(
        "hermes-input-voice-label",
        hermes.voice_status === "transcribing" ? "正在识别" : "语音输入中",
      );
      const placeholder = inputActive && !draft && !preedit;
      const afterNode = ui.byId("hermes-input-after");
      afterNode.classList.toggle("is-placeholder", placeholder);
      ui.text("hermes-input-before", placeholder ? "" : draft.slice(0, caret));
      ui.text("hermes-input-preedit", preedit);
      ui.text("hermes-input-after", placeholder ? "请输入" : draft.slice(caret));
      renderImeCandidates(ui.byId("hermes-input-candidates"), hermes);
      ui.text("audio-status", `语音  ${audioLabels[hermes.audio_status] || hermes.audio_status || "静音"}`);
      ui.text("screen-status", `屏幕  ${link.esp32_online ? "在线" : "离线"}`);
      ui.fitText("hermes-assistant", document.getElementById("display").dataset.fontChinese === "pixel" ? [18, 16, 14] : [20, 18, 16, 14]);
      const replyNode = ui.byId("hermes-reply");
      if (replyChanged && !replyContinues) manualReplyScroll = false;
      const remoteScrollDelta = ui.remoteScrollDelta(state, replyNode);
      if (scene === "hermes" && remoteScrollDelta !== null) {
        stopReplyAutoScroll(replyNode);
        const maximum = Math.max(0, replyNode.scrollHeight - replyNode.clientHeight);
        replyNode.scrollTop = Math.max(0, Math.min(maximum, replyNode.scrollTop + remoteScrollDelta));
        manualReplyScroll = true;
      }
      if (scene === "hermes") {
        if (manualReplyScroll) stopReplyAutoScroll(replyNode);
        else {
          scheduleReplyAutoScroll(replyNode, {
            reset: replyChanged && !replyContinues,
            finishing: hermes.streaming === false
          });
        }
      } else {
        manualReplyScroll = false;
        stopReplyAutoScroll(replyNode);
      }
      ui.text("light-name", light.name || "灯具");
      ui.text("light-state", light.on ? "已开启" : "已关闭");
      ui.text("brightness", `${String(Math.round(brightness)).padStart(2, "0")}%`);
      ui.segments("brightness-segments", brightness, "f1-segment");
      ui.selectionSegments(
        "color-temperature-segments",
        colorTemperature,
        minimumColorTemperature,
        maximumColorTemperature,
        "f1-segment"
      );
      ui.text("light-footer", light.error || `HOME ASSISTANT  ${light.online ? "已同步" : "离线"}`);
      ui.text("link-status", link.esp32_online ? "链路在线" : "等待设备");
      ui.text("display-fps", Number(link.display_fps || 0).toFixed(1));
      ui.text("frame-id", link.frame_id || 0);
      ui.text("dirty-tiles", link.dirty_tiles || 0);
      ui.text("packet-count", `${link.rx_packets || 0}/${link.accepted_packets || 0}`);
      ui.text("error-count", Number(link.rejected_packets || 0) + Number(link.draw_failed_packets || 0));
      ui.text("link-footer", `${String(link.transport_mode || "mjpeg").toUpperCase()}  ·  ${link.esp32_ip || "--"}`);
      const quota = codex.quota || codex.primary || codex.secondary || {};
      const quotaRemaining = quota.remaining_percent ?? "--";
      ui.text("codex-primary", quotaRemaining);
      ui.text("codex-window", quotaWindowLabel(quota));
      ui.text("codex-limit", codex.limit_name || codex.limit_id || "--");
      ui.text("codex-plan", String(codex.plan_type || "--").toUpperCase());
      ui.text("codex-reset", quotaResetLabel(quota.resets_at));

      if (previousReply !== null && replyChanged) emphasize(replyNode, "text");
      if (previousBrightness !== null && previousBrightness !== brightness) emphasize(ui.byId("brightness"), "number");
      previousReply = reply;
      previousBrightness = brightness;
    }
  };
})();

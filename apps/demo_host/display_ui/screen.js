(() => {
  "use strict";

  const root = document.getElementById("display");
  const themes = window.ESPAgentThemes || {};
  const query = new URLSearchParams(location.search);
  const previewTheme = query.get("theme");
  const previewFont = query.get("font");
  const previewFontChinese = query.get("font_chinese") || query.get("font_zh");
  const previewFontLatin = query.get("font_latin") || query.get("font_en");
  const previewFontNumeric = query.get("font_numeric") || query.get("font_num");
  const previewScale = query.get("scale");
  const previewMotion = query.get("motion");
  const previewHinting = query.get("hinting");
  const previewGrayscaleAa = query.get("grayscale_aa") || query.get("aa");
  let activeTheme = null;
  const fontRoleSupport = {
    first: {
      chinese: new Set(["auto", "modern", "ui", "editorial", "humanist", "pixel"]),
      latin: new Set(["auto", "modern", "ui", "editorial", "industrial", "pixel"]),
      numeric: new Set(["auto", "modern", "editorial", "industrial", "mono", "pixel"])
    },
    second: {
      chinese: new Set(["auto", "modern", "ui", "editorial", "pixel"]),
      latin: new Set(["auto", "modern", "ui", "editorial", "industrial", "pixel"]),
      numeric: new Set(["auto", "modern", "editorial", "industrial", "mono", "pixel"])
    }
  };
  const fontPresets = {
    auto: { chinese: "auto", latin: "auto", numeric: "auto" },
    modern: { chinese: "modern", latin: "modern", numeric: "modern" },
    ui: { chinese: "ui", latin: "ui", numeric: "modern" },
    editorial: { chinese: "editorial", latin: "editorial", numeric: "editorial" },
    humanist: { chinese: "humanist", latin: "editorial", numeric: "modern" },
    industrial: { chinese: "modern", latin: "industrial", numeric: "industrial" },
    pixel: { chinese: "pixel", latin: "pixel", numeric: "pixel" }
  };
  window.ESPAgentFontRegistry = {
    chinese: ["auto", "modern", "ui", "editorial", "humanist", "pixel"],
    latin: ["auto", "modern", "ui", "editorial", "industrial", "pixel"],
    numeric: ["auto", "modern", "editorial", "industrial", "mono", "pixel"]
  };

  function characterRole(char, previousRole) {
    if (/\p{Script=Han}/u.test(char) || /[\u3000-\u303f\uff00-\uffef]/u.test(char)) return "chinese";
    if (/[0-9]/.test(char)) return "numeric";
    if (/[A-Za-z]/.test(char)) return "latin";
    if (previousRole === "numeric" && /[%‰°.\/:+\-]/.test(char)) return "numeric";
    return previousRole || "chinese";
  }

  function writeScriptAwareText(node, value) {
    const source = String(value);
    const runs = [];
    let role = null;
    let run = "";
    const flush = () => {
      if (!run) return;
      runs.push({ role, text: run });
      run = "";
    };
    for (const char of source) {
      const nextRole = characterRole(char, role);
      if (role !== null && nextRole !== role) flush();
      role = nextRole;
      run += char;
    }
    flush();
    const children = Array.from(node.children);
    const reusable = children.length === runs.length && children.every((child, index) => (
      child.className === `ui-script-${runs[index].role}`
    ));
    if (reusable) {
      children.forEach((child, index) => {
        if (child.textContent !== runs[index].text) child.textContent = runs[index].text;
      });
      return;
    }
    const spans = runs.map(({ role: runRole, text }) => {
      const span = document.createElement("span");
      span.className = `ui-script-${runRole}`;
      span.textContent = text;
      return span;
    });
    node.replaceChildren(...spans);
  }

  function segmentNodes(container, count, className) {
    let nodes = Array.from(container.children);
    const needsRebuild = nodes.length !== count || nodes.some((node) => !node.classList.contains(className));
    if (!needsRebuild) return nodes;

    container.classList.remove("is-motion-ready");
    nodes = Array.from({ length: count }, () => {
      const segment = document.createElement("span");
      segment.className = className;
      return segment;
    });
    container.replaceChildren(...nodes);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (container.isConnected) container.classList.add("is-motion-ready");
    }));
    return nodes;
  }

  const utils = {
    byId(id) { return document.getElementById(id); },
    text(id, value) {
      const node = document.getElementById(id);
      if (!node) return;
      const text = String(value);
      if (node.dataset.uiText === text) return;
      writeScriptAwareText(node, text);
      node.dataset.uiText = text;
    },
    setActiveScene(scene, prefix) {
      root.querySelectorAll(`.${prefix}-scene`).forEach((node) => {
        node.classList.toggle("is-active", node.dataset.scene === scene);
      });
    },
    fitText(id, sizes) {
      const node = document.getElementById(id);
      if (!node) return;
      for (const size of sizes) {
        node.style.fontSize = `${size}px`;
        if (node.scrollHeight <= node.clientHeight && node.scrollWidth <= node.clientWidth) return;
      }
    },
    remoteScrollDelta(state, node) {
      if (!node) return null;
      const revision = Math.max(0, Number(state.display_ui?.scroll_revision) || 0);
      const previous = node.dataset.remoteScrollRevision;
      node.dataset.remoteScrollRevision = String(revision);
      if (previous === undefined || Number(previous) === revision) return null;
      const delta = Number(state.display_ui?.scroll_delta);
      return Number.isFinite(delta) && delta !== 0 ? delta : null;
    },
    segments(id, brightness, className) {
      const container = document.getElementById(id);
      if (!container) return;
      const filled = Math.round(brightness / 10);
      const nodes = segmentNodes(container, 10, className);
      const changed = nodes.filter((node, index) => node.classList.contains("is-filled") !== (index < filled));
      const ordered = filled < Number(container.dataset.filledCount || 0) ? changed.reverse() : changed;
      nodes.forEach((node) => node.style.setProperty("--segment-delay", "0ms"));
      ordered.forEach((node, index) => node.style.setProperty("--segment-delay", `${index * 22}ms`));
      nodes.forEach((node, index) => node.classList.toggle("is-filled", index < filled));
      container.dataset.filledCount = String(filled);
    },
    selectionSegments(id, value, minimum, maximum, className) {
      const container = document.getElementById(id);
      if (!container) return;
      const count = 10;
      const numericValue = Number(value);
      const numericMinimum = Number(minimum);
      const numericMaximum = Number(maximum);
      const hasSelection = value !== null && value !== undefined && value !== ""
        && Number.isFinite(numericValue)
        && Number.isFinite(numericMinimum)
        && Number.isFinite(numericMaximum)
        && numericMaximum > numericMinimum;
      const selected = hasSelection
        ? Math.round((Math.max(numericMinimum, Math.min(numericMaximum, numericValue)) - numericMinimum)
          / (numericMaximum - numericMinimum) * (count - 1))
        : -1;
      const nodes = segmentNodes(container, count, className);
      const previous = nodes.findIndex((node) => node.classList.contains("is-selected"));
      nodes.forEach((node) => node.style.setProperty("--segment-delay", "0ms"));
      if (selected !== previous && selected >= 0) {
        nodes[selected].style.setProperty("--segment-delay", previous >= 0 ? "55ms" : "0ms");
      }
      nodes.forEach((node, index) => node.classList.toggle("is-selected", index === selected));
    }
  };

  function selectTheme(state) {
    const requested = previewTheme || state.display_ui?.theme || "first";
    return themes[requested] ? requested : "first";
  }

  function selectFonts(state, themeId) {
    const selected = {
      chinese: state.display_ui?.font_chinese || "auto",
      latin: state.display_ui?.font_latin || "auto",
      numeric: state.display_ui?.font_numeric || "auto"
    };
    if (previewFont && fontPresets[previewFont]) Object.assign(selected, fontPresets[previewFont]);
    if (previewFontChinese) selected.chinese = previewFontChinese;
    if (previewFontLatin) selected.latin = previewFontLatin;
    if (previewFontNumeric) selected.numeric = previewFontNumeric;
    for (const role of Object.keys(selected)) {
      if (!fontRoleSupport[themeId]?.[role].has(selected[role])) selected[role] = "auto";
    }
    return selected;
  }

  function selectScale(state) {
    const requested = Number(previewScale || state.display_ui?.scale_percent || 100);
    return Math.max(90, Math.min(110, Number.isFinite(requested) ? requested : 100));
  }

  function selectOrientation(state) {
    return state.display_ui?.orientation === "landscape" ? "landscape" : "portrait";
  }

  function applyGeometry(orientation) {
    const width = orientation === "landscape" ? 320 : 240;
    const height = orientation === "landscape" ? 240 : 320;
    if (root.dataset.orientation === orientation) return;
    root.dataset.orientation = orientation;
    root.style.width = `${width}px`;
    root.style.height = `${height}px`;
    document.documentElement.style.width = `${width}px`;
    document.documentElement.style.height = `${height}px`;
    document.body.style.width = `${width}px`;
    document.body.style.height = `${height}px`;
    delete root.dataset.scale;
  }

  function applyScale(scalePercent) {
    const stage = root.querySelector(".ui-scale-stage");
    if (!stage) return;
    if (root.dataset.scale === String(scalePercent)) return;
    const scale = scalePercent / 100;
    const width = root.dataset.orientation === "landscape" ? 320 : 240;
    const height = root.dataset.orientation === "landscape" ? 240 : 320;
    stage.style.width = `${width / scale}px`;
    stage.style.height = `${height / scale}px`;
    stage.style.transform = `scale(${scale})`;
    root.dataset.scale = String(scalePercent);
  }

  function ensureTheme(themeId, fonts, motionEnabled, hinting, grayscaleAntialiasing) {
    if (activeTheme !== themeId) {
      if (activeTheme) themes[activeTheme].unmount?.(root, utils);
      root.replaceChildren();
      root.innerHTML = `<div class="ui-scale-stage">${themes[themeId].markup}</div>`;
      root.dataset.theme = themeId;
      activeTheme = themeId;
      themes[themeId].mount?.(root, utils);
    }
    root.dataset.fontChinese = fonts.chinese;
    root.dataset.fontLatin = fonts.latin;
    root.dataset.fontNumeric = fonts.numeric;
    const motion = previewMotion === null ? motionEnabled : previewMotion !== "0";
    root.dataset.motion = motion ? "on" : "off";
    const requestedHinting = previewHinting || hinting || "auto";
    root.dataset.hinting = ["auto", "crisp", "smooth"].includes(requestedHinting) ? requestedHinting : "auto";
    const grayscaleAa = previewGrayscaleAa === null ? grayscaleAntialiasing : previewGrayscaleAa !== "0";
    root.dataset.grayscaleAa = grayscaleAa ? "on" : "off";
  }

  window.renderDisplay = (state) => {
    const themeId = selectTheme(state);
    const fonts = selectFonts(state, themeId);
    const scalePercent = selectScale(state);
    applyGeometry(selectOrientation(state));
    ensureTheme(
      themeId,
      fonts,
      state.display_ui?.motion_enabled !== false,
      state.display_ui?.font_hinting,
      state.display_ui?.grayscale_antialiasing === true
    );
    applyScale(scalePercent);
    themes[themeId].render(state, utils);
    document.documentElement.dataset.ready = "true";
    return true;
  };

  async function poll() {
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (response.ok) window.renderDisplay(await response.json());
    } finally {
      setTimeout(poll, 250);
    }
  }

  const emptyState = {
    active_scene: "hermes",
    display_ui: { theme: "first", orientation: "portrait", font_chinese: "auto", font_latin: "auto", font_numeric: "auto", scale_percent: 100, motion_enabled: true, font_hinting: "auto", grayscale_antialiasing: false, scroll_delta: 0, scroll_revision: 0, keyboard_control_enabled: false, global_hotkeys_available: false },
    hermes: {},
    light: {},
    link: {},
    modules: {}
  };
  window.renderDisplay(emptyState);
  if (location.protocol === "http:" || location.protocol === "https:") poll();
})();

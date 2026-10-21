(() => {
  "use strict";
  const moduleId = "builtin.codex-usage";
  let espControlEnabled = false;
  let shortcutSettings = {
    toggle_modifier: "alt",
    toggle_key: "backquote",
    navigation_modifier: "none",
    scroll_modifier: "alt",
    navigation_mode: "none",
    navigation_prefix: "chord:vk:18:Alt",
    scroll_mode: "custom",
    scroll_prefix: "chord:vk:18:Alt",
    voice_modifier: "ctrl_alt",
    voice_key: "v",
    exit_mode: "same",
    exit_modifier: "alt",
    exit_key: "backquote",
  };
  const pressedShortcutCodes = new Set();
  const shortcutSpecialCodes = {
    backquote: "Backquote", minus: "Minus", equal: "Equal", bracket_left: "BracketLeft",
    bracket_right: "BracketRight", backslash: "Backslash", semicolon: "Semicolon",
    quote: "Quote", comma: "Comma", period: "Period", slash: "Slash", space: "Space",
    tab: "Tab", enter: "Enter", escape: "Escape", backspace: "Backspace", delete: "Delete",
    insert: "Insert", home: "Home", end: "End", page_up: "PageUp", page_down: "PageDown",
    arrow_left: "ArrowLeft", arrow_up: "ArrowUp", arrow_right: "ArrowRight",
    arrow_down: "ArrowDown", caps_lock: "CapsLock", num_lock: "NumLock",
    scroll_lock: "ScrollLock", pause: "Pause", print_screen: "PrintScreen",
    numpad_add: "NumpadAdd", numpad_subtract: "NumpadSubtract",
    numpad_multiply: "NumpadMultiply", numpad_divide: "NumpadDivide",
    numpad_decimal: "NumpadDecimal",
  };

  function shortcutKeyCode(key) {
    if (key.startsWith("chord:")) return "";
    const encoded = /^vk:\d{1,3}:([A-Za-z0-9]+)$/.exec(key);
    if (encoded) return encoded[1];
    if (/^[a-z]$/.test(key)) return `Key${key.toUpperCase()}`;
    if (/^[0-9]$/.test(key)) return `Digit${key}`;
    if (/^f([1-9]|1[0-9]|2[0-4])$/.test(key)) return key.toUpperCase();
    if (/^numpad[0-9]$/.test(key)) return `Numpad${key.at(-1)}`;
    return shortcutSpecialCodes[key] || "";
  }

  function shortcutChordCodes(key) {
    if (!key.startsWith("chord:")) return [];
    return key.slice("chord:".length)
      .split(",")
      .map((entry) => shortcutKeyCode(entry))
      .filter(Boolean);
  }

  function shortcutBindingMatches(event, key, modifier) {
    if (!key.startsWith("chord:")) {
      return event.code === shortcutKeyCode(key) && modifiersMatch(event, modifier);
    }
    const codes = shortcutChordCodes(key);
    return codes.length > 0 && codes.every(shortcutCodePressed);
  }

  function shortcutCodePressed(code) {
    const aliases = {
      Control: ["ControlLeft", "ControlRight"],
      Shift: ["ShiftLeft", "ShiftRight"],
      Alt: ["AltLeft", "AltRight"],
      Meta: ["MetaLeft", "MetaRight"],
    };
    return pressedShortcutCodes.has(code)
      || (aliases[code] || []).some((alias) => pressedShortcutCodes.has(alias));
  }

  function directionBindingMatches(event, mode, prefix, directions) {
    if (!directions.includes(event.code) || mode === "disabled") return false;
    if (mode === "none") return true;
    const codes = shortcutChordCodes(prefix);
    return mode === "custom" && codes.length > 0 && codes.every(shortcutCodePressed);
  }

  function modifiersMatch(event, modifier) {
    const parts = modifier === "none" ? [] : modifier.split("_");
    return Boolean(event.altKey) === parts.includes("alt")
      && Boolean(event.ctrlKey) === parts.includes("ctrl")
      && Boolean(event.shiftKey) === parts.includes("shift")
      && Boolean(event.metaKey) === parts.includes("win");
  }

  window.addEventListener("message", (event) => {
    if (event.origin === location.origin && event.data?.type === "espagent:control-state") {
      espControlEnabled = Boolean(event.data.enabled);
      shortcutSettings = { ...shortcutSettings, ...(event.data.shortcuts || {}) };
    }
  });
  window.addEventListener("keydown", (event) => {
    pressedShortcutCodes.add(event.code);
    const isToggle = shortcutBindingMatches(event, shortcutSettings.toggle_key, shortcutSettings.toggle_modifier);
    const isVoice = shortcutBindingMatches(event, shortcutSettings.voice_key, shortcutSettings.voice_modifier);
    const isExit = espControlEnabled
      && shortcutSettings.exit_mode === "custom"
      && shortcutBindingMatches(event, shortcutSettings.exit_key, shortcutSettings.exit_modifier);
    const isHorizontal = directionBindingMatches(
      event,
      shortcutSettings.navigation_mode,
      shortcutSettings.navigation_prefix,
      ["ArrowLeft", "ArrowRight"],
    );
    const isVertical = directionBindingMatches(
      event,
      shortcutSettings.scroll_mode,
      shortcutSettings.scroll_prefix,
      ["ArrowUp", "ArrowDown"],
    );
    const isDirection = isHorizontal || isVertical;
    const chordNeedsForwarding = shortcutSettings.toggle_key.startsWith("chord:")
      || shortcutSettings.voice_key.startsWith("chord:")
      || (shortcutSettings.exit_mode === "custom" && shortcutSettings.exit_key.startsWith("chord:"))
      || shortcutSettings.navigation_mode === "custom"
      || shortcutSettings.scroll_mode === "custom";
    if (!chordNeedsForwarding && !isToggle && !isVoice && !isExit && !(espControlEnabled && isDirection)) return;
    if (isToggle || isVoice || isExit || (espControlEnabled && isDirection)) {
      event.preventDefault();
      event.stopPropagation();
    }
    window.parent.postMessage({
      type: "espagent:control-key",
      code: event.code,
      keyCode: event.keyCode,
      altKey: event.altKey,
      ctrlKey: event.ctrlKey,
      shiftKey: event.shiftKey,
      metaKey: event.metaKey,
      repeat: event.repeat,
      phase: "down",
    }, location.origin);
  }, true);
  window.addEventListener("keyup", (event) => {
    pressedShortcutCodes.delete(event.code);
    window.parent.postMessage({
      type: "espagent:control-key",
      code: event.code,
      keyCode: event.keyCode,
      altKey: event.altKey,
      ctrlKey: event.ctrlKey,
      shiftKey: event.shiftKey,
      metaKey: event.metaKey,
      repeat: false,
      phase: "up",
    }, location.origin);
  }, true);
  const $ = (id) => document.getElementById(id);

  function windowName(window) {
    const minutes = Number(window?.window_duration_minutes);
    if (!minutes) return "额度周期";
    if (minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
    if (minutes % 60 === 0) return `${minutes / 60} 小时额度`;
    return `${minutes} 分钟额度`;
  }

  function resetLabel(value) {
    if (!value) return "--";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString("zh-CN", { hour12: false });
  }

  function tone(remaining) {
    if (remaining <= 5) return "error";
    if (remaining <= 20) return "warn";
    return "ok";
  }

  function setMeter(id, remaining) {
    const normalized = Number.isFinite(Number(remaining)) ? Math.max(0, Math.min(100, Number(remaining))) : 0;
    $(id).style.width = `${normalized}%`;
    $(id).dataset.tone = tone(normalized);
  }

  function setQuota(value) {
    const remaining = Number(value?.remaining_percent);
    const available = Number.isFinite(remaining);
    const normalized = available ? Math.max(0, Math.min(100, remaining)) : 0;
    $("quota-label").textContent = windowName(value);
    $("quota-remaining").textContent = available ? normalized : "--";
    $("quota-window").textContent = windowName(value);
    $("quota-value").textContent = available ? `${normalized}% 剩余` : "暂不可用";
    $("quota-reset").textContent = resetLabel(value?.resets_at);
    setMeter("quota-meter", available ? normalized : 0);
  }

  function setNotice(title, detail, noticeTone = "") {
    const notice = $("notice");
    const heading = document.createElement("strong");
    const body = document.createElement("span");
    heading.textContent = title;
    body.textContent = detail;
    notice.hidden = false;
    notice.dataset.tone = noticeTone;
    notice.replaceChildren(heading, body);
  }

  function render(envelope) {
    const state = envelope.state || {};
    const statusLabels = {
      ready: ["在线", "ok"], connecting: ["连接中", ""], auth_required: ["需要登录", "warn"],
      codex_not_found: ["未找到 Codex", "error"], error: ["读取失败", "error"], stopped: ["已停止", ""],
    };
    const [label, statusTone] = statusLabels[state.status] || [state.status || envelope.lifecycle, ""];
    $("status").textContent = label;
    $("status").dataset.tone = statusTone;
    $("plan").textContent = String(state.plan_type || "--").toUpperCase();
    $("limit-name").textContent = state.limit_name || state.limit_id || "--";
    const credits = state.credits || {};
    $("credits").textContent = credits.unlimited ? "UNLIMITED" : (credits.balance ?? "--");
    $("updated-at").textContent = state.updated_at
      ? new Date(state.updated_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })
      : "--";
    setQuota(state.quota || state.primary || state.secondary);

    const notice = $("notice");
    if (state.status === "auth_required") {
      setNotice("Codex CLI 尚未登录", "请在终端运行 codex login，完成后点击刷新。", "warn");
    } else if (state.error) {
      setNotice("额度读取失败", state.error, "error");
    } else if (state.status === "connecting") {
      setNotice("正在连接 Codex", "账户额度将在连接后更新。");
    } else {
      notice.hidden = true;
    }
  }

  async function refresh() {
    try {
      const response = await fetch(`/api/modules/${moduleId}/state`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      $("status").textContent = "Host 离线";
      $("status").dataset.tone = "error";
      setNotice("无法连接模块运行时", error.message, "error");
    }
  }

  $("refresh").addEventListener("click", async () => {
    await fetch(`/api/modules/${moduleId}/actions/refresh`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    window.setTimeout(refresh, 250);
  });
  refresh();
  window.setInterval(refresh, 10000);
})();

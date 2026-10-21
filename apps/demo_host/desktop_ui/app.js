(() => {
  "use strict";

  const viewTitles = {
    overview: "总览",
    modules: "模块",
    assistant: "助手",
    home: "家居",
    display: "显示",
    firmware: "固件",
    settings: "设置",
    diagnostics: "诊断",
  };
  const tonePresets = {
    off: { enabled: false, preset: "off", contrast_percent: 100, brightness_percent: 100, color_percent: 100, sharpness_percent: 100, binary_threshold: 0 },
    "ips-contrast": { enabled: true, preset: "ips-contrast", contrast_percent: 135, brightness_percent: 105, color_percent: 110, sharpness_percent: 125, binary_threshold: 0 },
    "high-contrast": { enabled: true, preset: "high-contrast", contrast_percent: 165, brightness_percent: 108, color_percent: 125, sharpness_percent: 150, binary_threshold: 0 },
    "binary-text": { enabled: true, preset: "binary-text", contrast_percent: 100, brightness_percent: 100, color_percent: 0, sharpness_percent: 100, binary_threshold: 128 },
  };
  const espControlScenes = [
    { id: "hermes", label: "Hermes" },
    { id: "lights", label: "Home" },
    { id: "link", label: "Display" },
    { id: "codex", label: "Codex", moduleId: "builtin.codex-usage" },
  ];
  const shortcutDefaults = {
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
  const shortcutSpecialCodes = {
    backquote: "Backquote", minus: "Minus", equal: "Equal",
    bracket_left: "BracketLeft", bracket_right: "BracketRight", backslash: "Backslash",
    semicolon: "Semicolon", quote: "Quote", comma: "Comma", period: "Period", slash: "Slash",
    space: "Space", tab: "Tab", enter: "Enter", escape: "Escape", backspace: "Backspace",
    delete: "Delete", insert: "Insert", home: "Home", end: "End", page_up: "PageUp",
    page_down: "PageDown", arrow_left: "ArrowLeft", arrow_up: "ArrowUp",
    arrow_right: "ArrowRight", arrow_down: "ArrowDown", caps_lock: "CapsLock",
    num_lock: "NumLock", scroll_lock: "ScrollLock", pause: "Pause",
    print_screen: "PrintScreen", numpad_add: "NumpadAdd", numpad_subtract: "NumpadSubtract",
    numpad_multiply: "NumpadMultiply", numpad_divide: "NumpadDivide",
    numpad_decimal: "NumpadDecimal",
  };
  const shortcutSpecialLabels = {
    backquote: "`", minus: "-", equal: "=", bracket_left: "[", bracket_right: "]",
    backslash: "\\", semicolon: ";", quote: "'", comma: ",", period: ".", slash: "/",
    space: "Space", tab: "Tab", enter: "Enter", escape: "Esc", backspace: "Backspace",
    delete: "Delete", insert: "Insert", home: "Home", end: "End", page_up: "Page Up",
    page_down: "Page Down", arrow_left: "←", arrow_up: "↑", arrow_right: "→",
    arrow_down: "↓", caps_lock: "Caps Lock", num_lock: "Num Lock",
    scroll_lock: "Scroll Lock", pause: "Pause", print_screen: "Print Screen",
    numpad_add: "Num +", numpad_subtract: "Num -", numpad_multiply: "Num ×",
    numpad_divide: "Num ÷", numpad_decimal: "Num .",
  };
  const shortcutVirtualKeysByCode = {
    ShiftLeft: 0xa0, ShiftRight: 0xa1, ControlLeft: 0xa2, ControlRight: 0xa3,
    AltLeft: 0xa4, AltRight: 0xa5, MetaLeft: 0x5b, MetaRight: 0x5c,
    BrowserBack: 0xa6, BrowserForward: 0xa7, BrowserRefresh: 0xa8, BrowserStop: 0xa9,
    BrowserSearch: 0xaa, BrowserFavorites: 0xab, BrowserHome: 0xac,
    AudioVolumeMute: 0xad, AudioVolumeDown: 0xae, AudioVolumeUp: 0xaf,
    MediaTrackNext: 0xb0, MediaTrackPrevious: 0xb1, MediaStop: 0xb2,
    MediaPlayPause: 0xb3, LaunchMail: 0xb4, MediaSelect: 0xb5,
    LaunchApp1: 0xb6, LaunchApp2: 0xb7,
  };
  const displayGpioKeys = ["scl", "sda", "cs", "dc", "rst", "bl"];
  const displayGpioDefaults = { scl: 12, sda: 11, cs: 10, dc: 8, rst: 7, bl: 6 };

  let currentView = "overview";
  let currentState = null;
  let hermesStreaming = false;
  let lastPreviewFrame = null;
  let previewBlobUrl = null;
  let previewRunning = true;
  let toastTimer = null;
  let pollBusy = false;
  let installedModules = [];
  let activeModuleId = null;
  let espControlEnabled = false;
  let espControlAvailable = false;
  let globalHotkeysAvailable = false;
  let espControlUpdatePending = false;
  let espControlCommandPending = false;
  let espControlLastCommandAt = 0;
  let firmwareState = null;
  let firmwarePollBusy = false;
  let firmwareLogSignature = "";
  let displayGpioDirty = false;
  let shortcutSettings = { ...shortcutDefaults };
  let shortcutCaptureTarget = null;
  let shortcutCaptureKeys = new Map();
  const pressedShortcutCodes = new Set();
  const shortcutChordDown = { toggle: false, voice: false, exit: false };
  let voiceShortcutHeld = false;
  let voiceShortcutPending = false;
  let voiceShortcutActive = false;

  const $ = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));
  const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const fixed = (value, digits = 1) => number(value).toFixed(digits);

  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (_) {
        // The HTTP status is enough when the body is not JSON.
      }
      throw new Error(detail);
    }
    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json") ? response.json() : response;
  }

  async function runAction(action, successMessage) {
    try {
      const result = await action();
      if (successMessage) showToast(successMessage);
      await refreshState();
      return result;
    } catch (error) {
      showToast(error.message || String(error), true);
      return null;
    }
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.toggle("is-error", isError);
    toast.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2800);
  }

  function syncEspControlIndicator() {
    const button = $("esp-control-toggle");
    const toggleLabel = shortcutLabel(shortcutSettings.toggle_modifier, shortcutKeyLabel(shortcutSettings.toggle_key));
    button.setAttribute("aria-pressed", espControlEnabled ? "true" : "false");
    button.disabled = !espControlAvailable;
    button.title = espControlAvailable
      ? `ESP32 ${globalHotkeysAvailable ? "全局" : "窗口内"}屏幕控制${espControlEnabled ? "已开启" : "已关闭"} · ${toggleLabel}`
      : "ESP32 离线，屏幕控制不可用";
    button.querySelector("kbd").textContent = toggleLabel;
    const frame = $("module-frame");
    frame.contentWindow?.postMessage({ type: "espagent:control-state", enabled: espControlEnabled, shortcuts: shortcutSettings }, location.origin);
  }

  function shortcutSettingsFromUi(ui = {}) {
    return {
      toggle_modifier: ui.shortcut_toggle_modifier || shortcutDefaults.toggle_modifier,
      toggle_key: ui.shortcut_toggle_key || shortcutDefaults.toggle_key,
      navigation_modifier: ui.shortcut_navigation_modifier || shortcutDefaults.navigation_modifier,
      scroll_modifier: ui.shortcut_scroll_modifier || shortcutDefaults.scroll_modifier,
      navigation_mode: ui.shortcut_navigation_mode || shortcutDefaults.navigation_mode,
      navigation_prefix: ui.shortcut_navigation_prefix || shortcutDefaults.navigation_prefix,
      scroll_mode: ui.shortcut_scroll_mode || shortcutDefaults.scroll_mode,
      scroll_prefix: ui.shortcut_scroll_prefix || shortcutDefaults.scroll_prefix,
      voice_modifier: ui.shortcut_voice_modifier || shortcutDefaults.voice_modifier,
      voice_key: ui.shortcut_voice_key || shortcutDefaults.voice_key,
      exit_mode: ui.shortcut_exit_mode || shortcutDefaults.exit_mode,
      exit_modifier: ui.shortcut_exit_modifier || shortcutDefaults.exit_modifier,
      exit_key: ui.shortcut_exit_key || shortcutDefaults.exit_key,
    };
  }

  function shortcutLabel(modifier, keyLabel) {
    const labels = { ctrl: "Ctrl", shift: "Shift", alt: "Alt", win: "Win" };
    const modifierLabel = modifier === "none"
      ? ""
      : modifier.split("_").map((part) => labels[part] || part).join(" + ");
    return [modifierLabel, keyLabel].filter(Boolean).join(" + ");
  }

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

  function shortcutKeyLabel(key) {
    if (key.startsWith("chord:")) {
      return key.slice("chord:".length).split(",").map(shortcutKeyLabel).join(" + ");
    }
    const encoded = /^vk:\d{1,3}:([A-Za-z0-9]+)$/.exec(key);
    if (encoded) return shortcutCodeLabel(encoded[1]);
    if (/^[a-z]$/.test(key)) return key.toUpperCase();
    if (/^[0-9]$/.test(key)) return key;
    if (/^f([1-9]|1[0-9]|2[0-4])$/.test(key)) return key.toUpperCase();
    if (/^numpad[0-9]$/.test(key)) return `Num ${key.at(-1)}`;
    return shortcutSpecialLabels[key] || key;
  }

  function shortcutCodeLabel(code) {
    if (/^Key[A-Z]$/.test(code)) return code.slice(3);
    if (/^Digit[0-9]$/.test(code)) return code.slice(5);
    if (/^Numpad[0-9]$/.test(code)) return `Num ${code.at(-1)}`;
    const legacy = Object.keys(shortcutSpecialCodes).find((key) => shortcutSpecialCodes[key] === code);
    if (legacy) return shortcutSpecialLabels[legacy] || legacy;
    return code
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/^Audio /, "")
      .replace(/^Key /, "")
      .trim();
  }

  function encodedShortcutKey(event) {
    const virtualKey = Number(shortcutVirtualKeysByCode[event.code] || event.keyCode || event.which);
    if (!event.code || !Number.isInteger(virtualKey) || virtualKey < 1 || virtualKey > 0xff) return null;
    return `vk:${virtualKey}:${event.code}`;
  }

  function shortcutKeyMatches(event, key) {
    return event.code === shortcutKeyCode(key);
  }

  function shortcutChordCodes(key) {
    if (!key.startsWith("chord:")) return [];
    return key.slice("chord:".length)
      .split(",")
      .map((entry) => shortcutKeyCode(entry))
      .filter(Boolean);
  }

  function shortcutChordHeld(key) {
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

  function shortcutBindingMatches(event, key, modifier) {
    return key.startsWith("chord:")
      ? shortcutChordHeld(key)
      : shortcutKeyMatches(event, key) && shortcutModifiersMatch(event, modifier);
  }

  function directionBindingMatches(event, mode, prefix, directions) {
    if (!directions.includes(event.code) || mode === "disabled") return false;
    return mode === "none" || (mode === "custom" && shortcutChordHeld(prefix));
  }

  function shortcutModifiersMatch(event, modifier) {
    const parts = modifier === "none" ? [] : modifier.split("_");
    return Boolean(event.altKey) === parts.includes("alt")
      && Boolean(event.ctrlKey) === parts.includes("ctrl")
      && Boolean(event.shiftKey) === parts.includes("shift")
      && Boolean(event.metaKey) === parts.includes("win");
  }

  function applyEspControlState(enabled, announce = false) {
    espControlEnabled = Boolean(enabled);
    if (espControlEnabled && document.activeElement instanceof HTMLElement) document.activeElement.blur();
    syncEspControlIndicator();
    if (announce) showToast(`ESP32 屏幕控制已${espControlEnabled ? "开启" : "关闭"}`);
  }

  async function setEspControlEnabled(enabled, announce = true) {
    if (enabled && !espControlAvailable) {
      showToast("ESP32 离线，屏幕控制不可用", true);
      return;
    }
    const previous = espControlEnabled;
    applyEspControlState(enabled, announce);
    espControlUpdatePending = true;
    try {
      const result = await api("/api/display/control", {
        method: "POST",
        body: JSON.stringify({ enabled: Boolean(enabled) }),
      });
      applyEspControlState(result.enabled);
    } catch (error) {
      applyEspControlState(previous);
      showToast(error.message || String(error), true);
    } finally {
      espControlUpdatePending = false;
    }
  }

  async function sendEspControlCommand(action) {
    const now = performance.now();
    if (espControlCommandPending || now - espControlLastCommandAt < 70) return;
    espControlCommandPending = true;
    espControlLastCommandAt = now;
    try {
      await action();
      await refreshState();
    } catch (error) {
      showToast(error.message || String(error), true);
    } finally {
      espControlCommandPending = false;
    }
  }

  function cycleEspScene(direction) {
    const currentIndex = espControlScenes.findIndex((item) => item.id === currentState?.active_scene);
    const baseIndex = currentIndex >= 0 ? currentIndex : (direction > 0 ? -1 : 0);
    const nextIndex = (baseIndex + direction + espControlScenes.length) % espControlScenes.length;
    const target = espControlScenes[nextIndex];
    sendEspControlCommand(async () => {
      if (target.moduleId) {
        await api(`/api/modules/${encodeURIComponent(target.moduleId)}/activate`, {
          method: "POST",
          body: JSON.stringify({ target: "esp32" }),
        });
      } else {
        await api(`/api/scene/${target.id}`, { method: "POST", body: JSON.stringify({}) });
      }
      if (currentState) currentState.active_scene = target.id;
      showToast(`设备页面 · ${target.label}`);
    });
  }

  function scrollEspScene(delta) {
    sendEspControlCommand(() => api("/api/display/scroll", {
      method: "POST",
      body: JSON.stringify({ delta }),
    }));
  }

  function setShortcutCapture(target) {
    if (target === null) {
      shortcutCaptureTarget = null;
      shortcutCaptureKeys.clear();
    } else if (shortcutCaptureTarget === target) {
      const completedTarget = shortcutCaptureTarget;
      const key = `chord:${Array.from(shortcutCaptureKeys.values()).join(",")}`;
      shortcutCaptureTarget = null;
      shortcutCaptureKeys.clear();
      if (key !== "chord:") {
        const directionPrefix = completedTarget === "navigation" || completedTarget === "scroll";
        postJson("/api/settings/shortcuts", directionPrefix
          ? {
              [`${completedTarget}_mode`]: "custom",
              [`${completedTarget}_prefix`]: key,
            }
          : {
              [`${completedTarget}_modifier`]: "none",
              [`${completedTarget}_key`]: key,
              ...(completedTarget === "exit" ? { exit_mode: "custom" } : {}),
            }, "快捷键已更新");
      } else {
        showToast("未录入按键", true);
      }
    } else {
      shortcutCaptureTarget = target;
      shortcutCaptureKeys = new Map();
    }
    ["toggle", "voice", "navigation", "scroll", "exit"].forEach((name) => {
      const button = $(`shortcut-${name}-capture`);
      const active = shortcutCaptureTarget === name;
      button.classList.toggle("is-capturing", active);
      button.textContent = active ? "停止" : "录入";
    });
    renderShortcutCapturePreview();
  }

  function renderShortcutCapturePreview() {
    if (!shortcutCaptureTarget) return;
    const preview = $(`shortcut-${shortcutCaptureTarget}-preview`);
    const keys = Array.from(shortcutCaptureKeys.values());
    preview.textContent = keys.length
      ? keys.map(shortcutKeyLabel).join(" + ")
      : "等待按键";
  }

  function captureShortcut(event) {
    if (event.repeat) return true;
    const key = encodedShortcutKey(event);
    if (!key) {
      showToast("系统未提供该按键的 Windows 键码", true);
      return true;
    }
    if (!shortcutCaptureKeys.has(event.code) && shortcutCaptureKeys.size >= 12) {
      showToast("单个快捷键最多包含 12 个按键", true);
      return true;
    }
    shortcutCaptureKeys.set(event.code, key);
    renderShortcutCapturePreview();
    return true;
  }

  async function beginVoiceShortcut() {
    if (voiceShortcutPending || voiceShortcutActive || !espControlAvailable) return;
    voiceShortcutHeld = true;
    voiceShortcutPending = true;
    let startAttempted = false;
    try {
      if (!espControlEnabled) await setEspControlEnabled(true, false);
      if (!espControlEnabled) return;
      if (currentState?.active_scene !== "hermes") {
        await api("/api/scene/hermes", { method: "POST", body: JSON.stringify({}) });
        if (currentState) currentState.active_scene = "hermes";
      }
      await api("/api/voice/enable", {
        method: "POST",
        body: JSON.stringify({ enabled: true }),
      });
      startAttempted = true;
      await api("/api/hermes/input/voice/start", {
        method: "POST",
        body: JSON.stringify({}),
      });
      voiceShortcutActive = true;
      if (!voiceShortcutHeld) await endVoiceShortcut();
      else await refreshState();
    } catch (error) {
      if (startAttempted && !voiceShortcutActive) {
        try {
          await api("/api/hermes/input/voice/stop", {
            method: "POST",
            body: JSON.stringify({}),
          });
        } catch (_) {
          // Preserve the original start error; the global runtime also stops on release.
        }
      }
      showToast(error.message || String(error), true);
    } finally {
      voiceShortcutPending = false;
      if (!voiceShortcutHeld && voiceShortcutActive) await endVoiceShortcut();
    }
  }

  async function endVoiceShortcut() {
    voiceShortcutHeld = false;
    if (!voiceShortcutActive) return;
    voiceShortcutActive = false;
    try {
      await api("/api/hermes/input/voice/stop", {
        method: "POST",
        body: JSON.stringify({}),
      });
      await refreshState();
    } catch (error) {
      showToast(error.message || String(error), true);
    }
  }

  function voiceModifierReleased(code) {
    const parts = shortcutSettings.voice_modifier === "none"
      ? []
      : shortcutSettings.voice_modifier.split("_");
    return (parts.includes("ctrl") && ["ControlLeft", "ControlRight"].includes(code))
      || (parts.includes("shift") && ["ShiftLeft", "ShiftRight"].includes(code))
      || (parts.includes("alt") && ["AltLeft", "AltRight"].includes(code))
      || (parts.includes("win") && ["MetaLeft", "MetaRight"].includes(code));
  }

  function processEspControlKey({ code, keyCode = 0, altKey = false, ctrlKey = false, shiftKey = false, metaKey = false, repeat = false }) {
    if (globalHotkeysAvailable) return false;
    if (!espControlAvailable) return false;
    const keyState = { code, keyCode, altKey, ctrlKey, shiftKey, metaKey };
    if (shortcutBindingMatches(keyState, shortcutSettings.voice_key, shortcutSettings.voice_modifier)) {
      if (!repeat && !shortcutChordDown.voice) beginVoiceShortcut();
      if (shortcutSettings.voice_key.startsWith("chord:")) shortcutChordDown.voice = true;
      return true;
    }
    if (shortcutBindingMatches(keyState, shortcutSettings.toggle_key, shortcutSettings.toggle_modifier)) {
      if (!repeat && !shortcutChordDown.toggle) setEspControlEnabled(!espControlEnabled);
      if (shortcutSettings.toggle_key.startsWith("chord:")) shortcutChordDown.toggle = true;
      return true;
    }
    if (!espControlEnabled) return false;
    if (shortcutSettings.exit_mode === "custom"
      && shortcutBindingMatches(keyState, shortcutSettings.exit_key, shortcutSettings.exit_modifier)) {
      if (!repeat && !shortcutChordDown.exit) setEspControlEnabled(false);
      if (shortcutSettings.exit_key.startsWith("chord:")) shortcutChordDown.exit = true;
      return true;
    }
    if (directionBindingMatches(
      keyState,
      shortcutSettings.navigation_mode,
      shortcutSettings.navigation_prefix,
      ["ArrowLeft", "ArrowRight"],
    )) {
      cycleEspScene(code === "ArrowRight" ? 1 : -1);
      return true;
    }
    if (directionBindingMatches(
      keyState,
      shortcutSettings.scroll_mode,
      shortcutSettings.scroll_prefix,
      ["ArrowUp", "ArrowDown"],
    )) {
      scrollEspScene(code === "ArrowDown" ? 36 : -36);
      return true;
    }
    return false;
  }

  function handleEspControlKey(event) {
    if (event.isComposing) return;
    if (shortcutCaptureTarget) {
      if (captureShortcut(event)) {
        event.preventDefault();
        event.stopPropagation();
      }
      return;
    }
    pressedShortcutCodes.add(event.code);
    if (!processEspControlKey(event)) return;
    event.preventDefault();
    event.stopPropagation();
  }

  function handleEspControlKeyUp(event) {
    pressedShortcutCodes.delete(event.code);
    if (!shortcutChordHeld(shortcutSettings.toggle_key)) shortcutChordDown.toggle = false;
    if (!shortcutChordHeld(shortcutSettings.exit_key)) shortcutChordDown.exit = false;
    const voiceReleased = shortcutSettings.voice_key.startsWith("chord:")
      ? !shortcutChordHeld(shortcutSettings.voice_key)
      : shortcutKeyMatches(event, shortcutSettings.voice_key) || voiceModifierReleased(event.code);
    if (!shortcutChordHeld(shortcutSettings.voice_key)) shortcutChordDown.voice = false;
    if (globalHotkeysAvailable || (!voiceShortcutHeld && !voiceShortcutActive) || !voiceReleased) return;
    endVoiceShortcut();
    event.preventDefault();
    event.stopPropagation();
  }

  function setView(name, updateHash = true) {
    const selectedModule = name.startsWith("module/")
      ? installedModules.find((item) => item.enabled && item.id === name.slice("module/".length))
      : null;
    if (!selectedModule && !viewTitles[name]) name = "overview";
    const panelName = selectedModule ? "module-host" : name;
    currentView = name;
    if (!selectedModule) activeModuleId = null;
    all("[data-view]").forEach((button) => {
      const active = !selectedModule && button.dataset.view === name;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    all("[data-module-id]").forEach((button) => {
      const active = selectedModule?.id === button.dataset.moduleId;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    all("[data-view-panel]").forEach((panel) => panel.classList.toggle("is-active", panel.dataset.viewPanel === panelName));
    syncModuleVisibility(selectedModule?.id || null);
    $("view-title").textContent = selectedModule?.name || viewTitles[name];
    if (selectedModule) mountModule(selectedModule);
    if (updateHash && window.location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    if (name === "overview" || name === "display") startPreview();
    else stopPreview();
    if (name === "diagnostics" && currentState) $("raw-state").textContent = JSON.stringify(currentState, null, 2);
    if (name === "firmware") refreshFirmwareStatus();
  }

  function mountModule(module) {
    if (!module.enabled) return;
    const frame = $("module-frame");
    if (module.app_route && frame.dataset.moduleId !== module.id) {
      frame.src = module.app_route;
      frame.dataset.moduleId = module.id;
    }
    if (activeModuleId === module.id) return;
    activeModuleId = module.id;
    api(`/api/modules/${encodeURIComponent(module.id)}/activate`, {
      method: "POST",
      body: JSON.stringify({ target: module.esp32_route ? "app_and_esp32" : "app" }),
    }).then(refreshState).catch((error) => {
      activeModuleId = null;
      showToast(error.message || String(error), true);
    });
  }

  function syncModuleVisibility(visibleModuleId = null) {
    const frame = $("module-frame");
    frame.contentWindow?.postMessage({
      type: "espagent:module-visibility",
      active: Boolean(visibleModuleId && frame.dataset.moduleId === visibleModuleId),
    }, location.origin);
  }

  function createModuleButton(module) {
    const button = document.createElement("button");
    button.className = "nav-item";
    button.dataset.moduleId = module.id;
    const glyph = document.createElement("span");
    glyph.className = "nav-glyph";
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = { gauge: "◔", monitor: "▣", light: "✦" }[module.navigation?.icon] || "□";
    const label = document.createElement("span");
    label.textContent = module.name;
    button.append(glyph, label);
    button.addEventListener("click", () => setView(`module/${module.id}`));
    return button;
  }

  function renderModules() {
    const pinned = installedModules.filter((module) => module.enabled && module.navigation?.pinned && module.app_route);
    const moduleNav = $("module-nav");
    moduleNav.replaceChildren(...pinned.map(createModuleButton));
    $("module-nav-block").hidden = pinned.length === 0;

    const catalog = $("module-catalog");
    if (installedModules.length === 0) {
      const empty = document.createElement("div");
      empty.className = "module-empty";
      empty.textContent = "没有发现可用模块。";
      catalog.replaceChildren(empty);
      return;
    }
    const rows = installedModules.map((module) => {
      const row = document.createElement("div");
      row.className = "module-catalog-row";
      row.classList.toggle("is-disabled", !module.enabled);
      const identity = document.createElement("button");
      identity.type = "button";
      identity.className = "module-identity module-open-command";
      identity.disabled = !module.enabled || !module.app_route;
      const name = document.createElement("strong");
      name.textContent = module.name;
      const description = document.createElement("small");
      description.textContent = module.description || module.id;
      identity.append(name, description);
      const meta = document.createElement("span");
      meta.className = "module-meta";
      const details = document.createElement("span");
      details.className = "module-meta-details";
      const lifecycle = document.createElement("b");
      lifecycle.dataset.tone = module.enabled ? (module.lifecycle === "running" ? "ok" : "warn") : "muted";
      lifecycle.textContent = module.enabled
        ? (module.lifecycle === "running" ? "运行中" : module.lifecycle)
        : "已关闭";
      const version = document.createElement("small");
      version.textContent = `v${module.version}`;
      details.append(lifecycle, version);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "switch-control module-enabled-toggle";
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-checked", module.enabled ? "true" : "false");
      toggle.setAttribute("aria-label", `${module.enabled ? "关闭" : "启用"}${module.name}`);
      toggle.title = module.enabled ? "关闭模块" : "启用模块";
      toggle.append(document.createElement("span"));
      toggle.addEventListener("click", () => setModuleEnabled(module, !module.enabled, toggle));
      meta.append(details, toggle);
      row.append(identity, meta);
      identity.addEventListener("click", () => setView(`module/${module.id}`));
      return row;
    });
    catalog.replaceChildren(...rows);
  }

  async function setModuleEnabled(module, enabled, toggle) {
    toggle.disabled = true;
    try {
      const payload = await api(`/api/modules/${encodeURIComponent(module.id)}/enabled`, {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      const updated = payload.module;
      installedModules = installedModules.map((item) => item.id === module.id ? { ...item, ...updated } : item);
      if (!enabled && activeModuleId === module.id) activeModuleId = null;
      if (!enabled && currentView === `module/${module.id}`) {
        setView("modules");
      }
      renderModules();
      showToast(`${module.name}已${enabled ? "启用" : "关闭"}`);
      await refreshState();
    } catch (error) {
      toggle.disabled = false;
      showToast(error.message || String(error), true);
    }
  }

  async function loadModules() {
    try {
      const payload = await api("/api/modules");
      installedModules = Array.isArray(payload.modules) ? payload.modules : [];
    } catch (error) {
      installedModules = [];
      showToast(`模块运行时不可用：${error.message}`, true);
    }
    renderModules();
  }

  function setText(id, value) {
    const element = $(id);
    if (element) element.textContent = value;
  }

  function setControlValue(id, value) {
    const element = $(id);
    if (!element || document.activeElement === element) return;
    element.value = String(value);
  }

  function setSwitch(id, enabled) {
    const button = $(id);
    if (button) button.setAttribute("aria-checked", enabled ? "true" : "false");
  }

  function switchValue(id) {
    return $(id).getAttribute("aria-checked") === "true";
  }

  function setOnlineToken(id, online, warning = false) {
    const element = $(id);
    element.classList.toggle("is-online", Boolean(online));
    element.classList.toggle("is-warning", !online && Boolean(warning));
  }

  function timestamp(value) {
    if (!value) return "尚未同步";
    const date = new Date(number(value) * 1000);
    return Number.isNaN(date.getTime()) ? "尚未同步" : date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function formatBytes(value) {
    const bytes = number(value);
    if (bytes <= 0) return "等待构建";
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  }

  function renderFirmware(firmware) {
    firmwareState = firmware || {};
    const status = firmwareState.status || "idle";
    const running = Boolean(firmwareState.running);
    const progress = Math.max(0, Math.min(100, number(firmwareState.progress)));
    const device = firmwareState.device || null;
    const deviceCount = number(firmwareState.device_count);
    const wifi = firmwareState.wifi || {};
    const displayGpio = firmwareState.display_gpio || {};
    const statusLabels = {
      idle: device ? "设备就绪" : "等待设备",
      detecting: "正在识别",
      connecting: "正在连接",
      flashing: "正在写入",
      verifying: "正在校验",
      restarting: "正在重启",
      success: "烧录完成",
      error: "烧录失败",
      unavailable: "不可用",
    };
    const statusLabel = $("firmware-status-label");
    setText("firmware-status-label", statusLabels[status] || status.toUpperCase());
    statusLabel.classList.toggle("is-online", status === "success" || (status === "idle" && device));
    statusLabel.classList.toggle("is-error", status === "error" || status === "unavailable");
    $("firmware-status-label").closest(".firmware-stage").dataset.flashStatus = status;

    if (device) {
      setText("firmware-device", `${device.device} · ESP32-S3`);
      setText("firmware-device-detail", deviceCount > 1
        ? `${device.description} · 将自动验证 ${deviceCount} 个候选设备`
        : `${device.description} · 端口已自动选择`);
    } else {
      setText("firmware-device", firmwareState.runtime_ready === false ? "内置烧录器不可用" : "未发现 ESP32-S3");
      setText("firmware-device-detail", firmwareState.runtime_error || firmwareState.package_error || "连接 USB 后将自动识别端口");
    }

    setText("firmware-message", firmwareState.message || "准备烧录环境");
    setText("firmware-progress-value", `${progress}%`);
    const progressNode = $("firmware-progress");
    progressNode.setAttribute("aria-valuenow", String(progress));
    progressNode.querySelector("span").style.width = `${progress}%`;
    setText("firmware-phase", String(firmwareState.phase || "ready").toUpperCase());
    const activityTime = firmwareState.finished_at || firmwareState.started_at;
    setText("firmware-updated", activityTime ? timestamp(activityTime) : (device ? "设备已就绪" : "等待设备"));

    setText("firmware-target", firmwareState.target || "ESP32-S3");
    setText("firmware-project", firmwareState.project || "espagent_display");
    setText("firmware-idf", firmwareState.flasher_version ? `esptool v${firmwareState.flasher_version}` : "未检测到");
    const buildTime = firmwareState.build_time ? new Date(number(firmwareState.build_time) * 1000).toLocaleDateString("zh-CN") : "";
    const firmwareVersion = firmwareState.firmware_version ? `v${firmwareState.firmware_version}` : "";
    setText("firmware-build", [firmwareVersion, formatBytes(firmwareState.binary_size), buildTime].filter(Boolean).join(" · "));
    setText("firmware-port-count", `${deviceCount} DEVICE${deviceCount === 1 ? "" : "S"}`);
    $("firmware-port-count").classList.toggle("is-online", deviceCount > 0);

    const button = $("firmware-flash");
    button.disabled = running || !Boolean(firmwareState.can_flash);
    setText("firmware-flash-label", running ? "烧录中" : (status === "success" ? "再次烧录" : "烧录固件"));
    setText("firmware-command-icon", running ? "…" : "↓");
    if (running) setText("firmware-notice", "正在独占 USB 串口，请保持设备连接。");
    else if (status === "success") setText("firmware-notice", "设备正在重启，网络状态恢复后即可继续使用。");
    else if (status === "error") setText("firmware-notice", "重新插拔设备后可直接再次烧录。");
    else setText("firmware-notice", device ? "内置固件、端口和芯片参数已自动配置。" : "连接 ESP32-S3 后将自动启用烧录。");

    const logs = Array.isArray(firmwareState.logs) ? firmwareState.logs : [];
    const logText = logs.length ? logs.join("\n") : "等待开始…";
    if (logText !== firmwareLogSignature) {
      firmwareLogSignature = logText;
      const logNode = $("firmware-log");
      logNode.textContent = logText;
      logNode.scrollTop = logNode.scrollHeight;
    }

    const wifiStatus = wifi.status || "idle";
    const wifiStatusLabels = {
      idle: "WAITING",
      detecting: "DETECTING",
      applying: "WRITING",
      connecting: "CONNECTING",
      success: "CONNECTED",
      error: "FAILED",
      unavailable: "UNAVAILABLE",
    };
    const wifiPanel = $("firmware-wifi-state").closest(".firmware-wifi-panel");
    wifiPanel.dataset.wifiStatus = wifiStatus;
    setText("firmware-wifi-state", wifiStatusLabels[wifiStatus] || wifiStatus.toUpperCase());
    const wifiDevice = wifi.device?.device ? ` · ${wifi.device.device}` : "";
    const wifiIp = wifi.ip ? ` · ${wifi.ip}` : "";
    setText("firmware-wifi-message", wifi.message
      ? `${wifi.message}${wifiStatus === "success" ? `${wifiDevice}${wifiIp}` : ""}`
      : "连接 USB 后可写入网络，端口无需手动选择。");
    if (wifiStatus === "success") $("firmware-wifi-password").value = "";
    updateWifiConfigureButton();

    const gpioStatus = displayGpio.status || "idle";
    const gpioStatusLabels = {
      idle: "WAITING",
      detecting: "DETECTING",
      applying: "WRITING",
      success: "READY",
      error: "FAILED",
      unavailable: "UNAVAILABLE",
    };
    const gpioPanel = $("firmware-gpio-state").closest(".firmware-gpio-panel");
    gpioPanel.dataset.gpioStatus = gpioStatus;
    setText("firmware-gpio-state", gpioStatusLabels[gpioStatus] || gpioStatus.toUpperCase());
    setText("firmware-gpio-message", displayGpio.message || "修改后通过 USB 写入 NVS，设备重启后生效。");
    const gpioFormFocused = $("firmware-gpio-form").contains(document.activeElement);
    if (!displayGpioDirty && !gpioFormFocused) {
      const values = displayGpio.values || displayGpioDefaults;
      displayGpioKeys.forEach((key) => {
        const input = $(`firmware-gpio-${key}`);
        if (Number.isInteger(values[key])) input.value = String(values[key]);
      });
    }
    updateDisplayGpioButtons();
  }

  function updateWifiConfigureButton() {
    const button = $("firmware-wifi-apply");
    const ssidReady = $("firmware-wifi-ssid").value.trim().length > 0;
    const wifi = firmwareState?.wifi || {};
    button.disabled = !ssidReady || !Boolean(wifi.can_configure) || Boolean(wifi.running);
    button.querySelector("strong").textContent = wifi.running ? "正在配置" : "写入并连接";
  }

  function readDisplayGpioForm() {
    const values = {};
    displayGpioKeys.forEach((key) => {
      const raw = $(`firmware-gpio-${key}`).value.trim();
      const value = Number(raw);
      if (!raw || !Number.isInteger(value)) throw new Error(`${key.toUpperCase()} 需要整数 GPIO`);
      values[key] = value;
    });
    return values;
  }

  function updateDisplayGpioButtons() {
    const state = firmwareState?.display_gpio || {};
    const available = Boolean(state.can_configure) && !Boolean(state.running);
    $("firmware-gpio-read").disabled = !available;
    $("firmware-gpio-apply").disabled = !available;
    $("firmware-gpio-apply").querySelector("strong").textContent = state.running ? "正在写入" : "写入并重启";
  }

  async function readDisplayGpio() {
    if (!firmwareState?.display_gpio?.can_configure || firmwareState?.display_gpio?.running) return;
    displayGpioDirty = false;
    try {
      renderFirmware(await api("/api/firmware/display-gpio/read", { method: "POST" }));
      showToast("正在通过 USB 读取屏幕引脚");
    } catch (error) {
      showToast(error.message || String(error), true);
    } finally {
      await refreshFirmwareStatus();
    }
  }

  async function configureDisplayGpio(event) {
    event.preventDefault();
    if (!firmwareState?.display_gpio?.can_configure || firmwareState?.display_gpio?.running) return;
    try {
      const values = readDisplayGpioForm();
      displayGpioDirty = false;
      renderFirmware(await api("/api/firmware/display-gpio", {
        method: "POST",
        body: JSON.stringify(values),
      }));
      showToast("屏幕引脚写入已开始");
    } catch (error) {
      displayGpioDirty = true;
      showToast(error.message || String(error), true);
    } finally {
      await refreshFirmwareStatus();
    }
  }

  function resetDisplayGpioForm() {
    displayGpioKeys.forEach((key) => {
      $(`firmware-gpio-${key}`).value = String(displayGpioDefaults[key]);
    });
    displayGpioDirty = true;
  }

  async function refreshFirmwareStatus() {
    if (firmwarePollBusy) return;
    firmwarePollBusy = true;
    try {
      renderFirmware(await api("/api/firmware/status"));
    } catch (error) {
      renderFirmware({
        status: "unavailable",
        message: error.message || String(error),
        runtime_ready: false,
        environment_ready: false,
        can_flash: false,
        ports: [],
        logs: [],
      });
    } finally {
      firmwarePollBusy = false;
    }
  }

  async function startFirmwareFlash() {
    if (!firmwareState?.can_flash || firmwareState?.running) return;
    $("firmware-flash").disabled = true;
    try {
      renderFirmware(await api("/api/firmware/flash", { method: "POST" }));
      showToast("ESP32-S3 烧录已开始");
    } catch (error) {
      showToast(error.message || String(error), true);
    } finally {
      await refreshFirmwareStatus();
    }
  }

  async function startWifiProvisioning(event) {
    event.preventDefault();
    const ssid = $("firmware-wifi-ssid").value.trim();
    const password = $("firmware-wifi-password").value;
    if (!ssid || !firmwareState?.wifi?.can_configure || firmwareState?.wifi?.running) return;
    $("firmware-wifi-apply").disabled = true;
    try {
      renderFirmware(await api("/api/firmware/wifi", {
        method: "POST",
        body: JSON.stringify({ ssid, password }),
      }));
      showToast("网络配置已通过 USB 发送");
    } catch (error) {
      showToast(error.message || String(error), true);
    } finally {
      await refreshFirmwareStatus();
    }
  }

  async function refreshState() {
    if (pollBusy) return;
    pollBusy = true;
    try {
      currentState = await api("/api/state");
      renderState(currentState);
      if (currentView === "firmware") refreshFirmwareStatus();
    } catch (error) {
      setText("overview-summary", "本机后端暂时不可用，正在重试连接。");
      setOnlineToken("top-hermes", false, true);
      setOnlineToken("top-home", false, true);
      setOnlineToken("top-display", false, true);
    } finally {
      pollBusy = false;
    }
  }

  function renderState(state) {
    const hermes = state.hermes || {};
    const light = state.light || {};
    const link = state.link || {};
    const tone = state.display_tone || {};
    const ui = state.display_ui || {};
    const espOnline = Boolean(link.esp32_online);
    espControlAvailable = espOnline;
    shortcutSettings = shortcutSettingsFromUi(ui);
    globalHotkeysAvailable = Boolean(ui.global_hotkeys_available);
    if (!espControlUpdatePending) applyEspControlState(Boolean(ui.keyboard_control_enabled) && espOnline);
    else syncEspControlIndicator();
    const hostFps = number(link.fps);
    const lcdFps = number(link.display_fps);
    const frameLag = Math.max(0, number(link.frame_id) - number(link.displayed_frame_id));
    const mode = link.transport_mode === "indexed" ? `I${link.indexed_bpp || 2}` : String(link.transport_mode || "--").toUpperCase();
    const softwareVersion = String(state.software_version || "--");
    const panelSize = ui.orientation === "landscape" ? "320×240" : "240×320";

    $("rail-device-dot").classList.toggle("is-online", espOnline);
    setText("rail-device-state", espOnline ? "ESP32 在线" : "ESP32 离线");
    setText("rail-device-ip", link.esp32_ip || "等待状态");
    setText("app-version", `v${softwareVersion}`);
    setText("app-build-label", `VERSION ${softwareVersion} / ${panelSize}`);
    setOnlineToken("top-hermes", Boolean(hermes.online), true);
    setOnlineToken("top-home", Boolean(light.online), true);
    setOnlineToken("top-display", espOnline, true);

    setText("overview-summary", espOnline
      ? `设备 ${link.esp32_ip || ""} 已连接，${mode} 链路正在同步当前界面。`
      : "本机服务已运行，正在等待 ESP32-S3 连接。");
    setText("overview-host-fps", fixed(hostFps));
    setText("overview-lcd-fps", fixed(lcdFps));
    setText("overview-lag", frameLag);
    setText("overview-mode", mode);
    setText("overview-mode-note", link.transport_mode === "indexed" ? "INDEXED" : "STREAM");
    setText("overview-hermes-activity", hermes.activity || hermes.status || "待命");
    setText("overview-hermes-time", hermes.session_id ? `SESSION ${String(hermes.session_id).slice(0, 8)}` : "本机会话");
    setText("overview-light-activity", light.online ? (light.on ? "灯具已开启" : "灯具已关闭") : "设备离线");
    setText("overview-light-detail", `${number(light.brightness)}%`);
    setText("overview-display-activity", espOnline ? `${mode} · ${fixed(lcdFps)} FPS` : "等待设备");
    setText("overview-display-detail", `frame ${number(link.displayed_frame_id) || "--"}`);
    setText("overview-frame-id", `FRAME ${number(link.displayed_frame_id) || "--"}`);
    setText("display-frame-id", `FRAME ${number(link.displayed_frame_id) || "--"}`);

    if (!hermesStreaming) {
      setText("assistant-reply", hermes.interim_message || hermes.last_reply || "等待 Hermes 回复");
      const activity = hermes.activity || (hermes.status === "idle" ? "待命" : hermes.status) || "待命";
      setText("assistant-activity", hermes.online ? `在线 - ${activity}` : activity);
      $("assistant-tool").hidden = !hermes.active_tool;
      if (hermes.active_tool) setText("assistant-tool", hermes.tool_progress || `正在使用 ${hermes.active_tool}`);
    }
    setText("assistant-session", hermes.session_id ? `SESSION ${String(hermes.session_id).slice(0, 12)}` : "LOCAL SESSION");
    renderVoice(hermes);
    renderLight(light);
    renderTransport(link);
    renderAppearance(ui, tone);
    renderDiagnostics(link);
    if (currentView === "diagnostics") setText("raw-state", JSON.stringify(state, null, 2));
  }

  function renderVoice(hermes) {
    const voiceStatus = hermes.voice_status || "disabled";
    const voiceLabels = {
      disabled: "未启用", ready: "可以录音", listening: "正在聆听", recording: "正在聆听",
      transcribing: "正在转写", submitting: "正在提交", recognized: "已识别", unavailable: "不可用",
      failed: "录音失败", no_speech: "未检测到语音",
    };
    setText("voice-status", voiceLabels[voiceStatus] || voiceStatus);
    setText("voice-detail", hermes.voice_error || hermes.voice_details || (hermes.voice_available ? "本机语音输入已就绪" : "当前未检测到语音能力"));
    setText("voice-enable", hermes.voice_enabled ? "关闭麦克风" : "启用麦克风");
    const listening = voiceStatus === "listening" || voiceStatus === "recording";
    $("voice-record").setAttribute("aria-pressed", listening ? "true" : "false");
    setText("voice-record", listening ? "结束说话" : "开始说话");
    $("voice-enable").disabled = !Boolean(hermes.voice_supported);
    $("voice-record").disabled = !hermes.voice_enabled || !hermes.voice_available;
    setControlValue("voice-silence", number(hermes.voice_silence_seconds, 2));
    setText("voice-transcript", hermes.voice_transcript || "尚无语音转写");
    setText("audio-status", hermes.audio_status === "playing" ? "正在播放" : (hermes.audio_status === "disabled" ? "静音" : (hermes.audio_status || "待命")));
    setText("audio-detail", `队列 ${number(hermes.audio_queue_depth)} · ${(number(hermes.audio_bytes) / 1024).toFixed(1)} KB`);
  }

  function renderLight(light) {
    const level = number(light.brightness);
    const minTemperature = number(light.min_color_temp_kelvin, 2000);
    const maxTemperature = number(light.max_color_temp_kelvin, 6500);
    const temperature = number(light.color_temp_kelvin, Math.round((minTemperature + maxTemperature) / 2));
    setText("light-name", light.name || "客厅主灯");
    setText("light-entity", light.entity_id || "light.demo_light");
    setText("light-online", light.online ? "在线" : "离线");
    $("light-online").classList.toggle("is-online", Boolean(light.online));
    setText("light-percent", level);
    setText("light-power-label", light.on ? "已开启" : "已关闭");
    setSwitch("light-power", Boolean(light.on));
    setControlValue("light-brightness", Math.max(1, level || 40));
    setText("light-brightness-output", `${level}%`);
    $("light-temperature-control").min = String(minTemperature);
    $("light-temperature-control").max = String(maxTemperature);
    $("light-temperature-control").disabled = !light.online;
    setControlValue("light-temperature-control", temperature);
    setText("light-temperature-output", `${temperature} K`);
    const alpha = light.on ? Math.max(0.16, level / 100) : 0.08;
    $("light-visual").style.setProperty("--light-alpha", alpha.toFixed(2));
    $("light-visual").style.setProperty("--light-level", light.on ? level : 0);
    setText("light-sync", timestamp(light.last_synced_at));
    setText("light-temperature", light.color_temp_kelvin ? `${light.color_temp_kelvin} K` : "-- K");
    setText("light-temperature-range", `${minTemperature}–${maxTemperature} K`);
    setText("light-error", light.error || "无错误");
  }

  function renderTransport(link) {
    const transport = link.transport_mode || "indexed";
    all("[data-transport]").forEach((button) => button.setAttribute("aria-pressed", button.dataset.transport === transport ? "true" : "false"));
    all("[data-transport-panel]").forEach((panel) => { panel.hidden = panel.dataset.transportPanel !== transport; });
    setText("transport-sync", link.esp_transport_mode === transport ? "设备已同步" : "等待设备同步");
    $("transport-sync").classList.toggle("is-online", link.esp_transport_mode === transport);
    setControlValue("indexed-bpp", link.indexed_bpp || 2);
    setText("indexed-capacity", `${2 ** number(link.indexed_bpp, 2)} COLORS`);
    setControlValue("mjpeg-quality", number(link.mjpeg_quality, 90));
    setText("mjpeg-quality-output", number(link.mjpeg_quality, 90));
    setControlValue("mjpeg-subsampling", link.mjpeg_subsampling || "420");
    setSwitch("nack-toggle", Boolean(link.nack_enabled));
    setControlValue("backlight", number(link.backlight_percent, 100));
    setText("backlight-output", `${number(link.backlight_percent, 100)}%`);
    setControlValue("panel-gamma", link.panel_gamma || "1.8");
    setSwitch("preview-limit-toggle", Boolean(link.preview_fps_limited));
    setControlValue("preview-limit", number(link.preview_fps_limit, 30));
    $("preview-limit").disabled = !link.preview_fps_limited;
    setText("esp-log-toggle", `ESP 日志：${link.esp_log_enabled ? "开" : "关"}`);
  }

  function renderAppearance(ui, tone) {
    all("[data-theme]").forEach((button) => button.setAttribute("aria-pressed", button.dataset.theme === (ui.theme || "first") ? "true" : "false"));
    const landscape = ui.orientation === "landscape";
    all("[data-orientation]").forEach((button) => button.setAttribute("aria-pressed", button.dataset.orientation === (landscape ? "landscape" : "portrait") ? "true" : "false"));
    all(".device-stage, .screen-frame, .live-preview").forEach((node) => node.classList.toggle("is-landscape", landscape));
    setControlValue("font-chinese", ui.font_chinese || "auto");
    setControlValue("font-latin", ui.font_latin || "auto");
    setControlValue("font-numeric", ui.font_numeric || "auto");
    setControlValue("ui-scale", number(ui.scale_percent, 100));
    setControlValue("font-hinting", ui.font_hinting || "auto");
    setSwitch("grayscale-toggle", Boolean(ui.grayscale_antialiasing));
    setSwitch("motion-toggle", ui.motion_enabled !== false);
    setSwitch("tone-toggle", Boolean(tone.enabled));
    setControlValue("tone-preset", tone.preset || "off");
    setText("tone-contrast-value", `${number(tone.contrast_percent, 100)}%`);
    setText("tone-brightness-value", `${number(tone.brightness_percent, 100)}%`);
    setText("tone-color-value", `${number(tone.color_percent, 100)}%`);
    setText("tone-sharpness-value", `${number(tone.sharpness_percent, 100)}%`);
    renderShortcutSettings(ui);
  }

  function renderShortcutSettings(ui) {
    setControlValue("shortcut-navigation-mode", shortcutSettings.navigation_mode);
    setControlValue("shortcut-scroll-mode", shortcutSettings.scroll_mode);
    if (shortcutCaptureTarget !== "toggle") {
      setText("shortcut-toggle-preview", shortcutLabel(
        shortcutSettings.toggle_modifier,
        shortcutKeyLabel(shortcutSettings.toggle_key),
      ));
    }
    if (shortcutCaptureTarget !== "voice") {
      setText("shortcut-voice-preview", shortcutLabel(
        shortcutSettings.voice_modifier,
        shortcutKeyLabel(shortcutSettings.voice_key),
      ));
    }
    setControlValue("shortcut-exit-mode", shortcutSettings.exit_mode);
    const customExit = shortcutSettings.exit_mode === "custom";
    $("shortcut-exit-capture").hidden = !customExit;
    if (shortcutCaptureTarget !== "exit") {
      setText("shortcut-exit-preview", shortcutSettings.exit_mode === "disabled"
        ? "关闭"
        : shortcutSettings.exit_mode === "same"
          ? shortcutLabel(shortcutSettings.toggle_modifier, shortcutKeyLabel(shortcutSettings.toggle_key))
          : shortcutLabel(shortcutSettings.exit_modifier, shortcutKeyLabel(shortcutSettings.exit_key)));
    }
    const customNavigation = shortcutSettings.navigation_mode === "custom";
    const customScroll = shortcutSettings.scroll_mode === "custom";
    $("shortcut-navigation-capture").hidden = !customNavigation;
    $("shortcut-scroll-capture").hidden = !customScroll;
    if (shortcutCaptureTarget !== "navigation") {
      setText("shortcut-navigation-preview", shortcutSettings.navigation_mode === "disabled"
        ? "关闭"
        : customNavigation
          ? `${shortcutKeyLabel(shortcutSettings.navigation_prefix)} + ← / →`
          : "← / →");
    }
    if (shortcutCaptureTarget !== "scroll") {
      setText("shortcut-scroll-preview", shortcutSettings.scroll_mode === "disabled"
        ? "关闭"
        : customScroll
          ? `${shortcutKeyLabel(shortcutSettings.scroll_prefix)} + ↑ / ↓`
          : "↑ / ↓");
    }
    setText("shortcut-runtime", globalHotkeysAvailable ? "全局可用" : "窗口内");
    $("shortcut-runtime").classList.toggle("is-online", globalHotkeysAvailable);
  }

  function renderDiagnostics(link) {
    const indexed = link.transport_mode === "indexed";
    setText("diag-host-fps", fixed(link.fps));
    setText("diag-lcd-fps", fixed(link.display_fps));
    setText("diag-preview-fps", fixed(link.preview_fps));
    setText("diag-lag", Math.max(0, number(link.frame_id) - number(link.displayed_frame_id)));
    setText("diag-decode", fixed(number(link.decode_avg_us) / 1000, 2));
    setText("diag-lcd", fixed(number(link.lcd_avg_us) / 1000, 2));
    setText("diag-total", fixed(number(link.frame_avg_us) / 1000, 2));
    setText("diag-queue", indexed ? number(link.indexed_queue_depth) : number(link.jpeg_queued_frames));
    setText("diag-rx", `${number(link.rx_packets)} / ${number(link.accepted_packets)}`);
    setText("diag-errors", `${number(link.rejected_packets)} / ${number(link.draw_failed_packets)}`);
    setText("diag-drops", indexed
      ? `${number(link.indexed_dropped_frames)} / ${number(link.indexed_incomplete_frames)}`
      : `${number(link.jpeg_dropped_frames)} / ${number(link.jpeg_incomplete_frames)}`);
    const stage = indexed ? number(link.indexed_worker_stage) : number(link.jpeg_worker_stage);
    const workerFrame = indexed ? number(link.indexed_worker_frame_id) : number(link.jpeg_worker_frame_id);
    setText("diag-worker", stage ? `stage ${stage} · frame ${workerFrame}` : "idle");
  }

  function startPreview() {
    if (previewRunning) return;
    previewRunning = true;
    requestPreview();
  }

  function stopPreview() {
    previewRunning = false;
  }

  async function requestPreview() {
    if (!previewRunning || (currentView !== "overview" && currentView !== "display")) return;
    const started = performance.now();
    try {
      const endpoint = currentState?.link?.transport_mode === "mjpeg" ? "/api/display/preview.jpg" : "/api/preview.png";
      const response = await fetch(`${endpoint}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Preview ${response.status}`);
      const frameId = response.headers.get("X-Frame-Id");
      if (frameId === null || frameId !== lastPreviewFrame) {
        const blob = await response.blob();
        const nextUrl = URL.createObjectURL(blob);
        all(".live-preview").forEach((image) => { image.src = nextUrl; });
        if (previewBlobUrl) URL.revokeObjectURL(previewBlobUrl);
        previewBlobUrl = nextUrl;
        lastPreviewFrame = frameId;
      }
      const previewFps = number(currentState?.link?.preview_fps);
      setText("overview-preview-fps", `${fixed(previewFps)} FPS`);
      setText("display-preview-fps", `${fixed(previewFps)} FPS`);
    } catch (_) {
      setText("overview-preview-fps", "WAITING");
      setText("display-preview-fps", "WAITING");
    }
    if (!previewRunning) return;
    const sourceFps = Math.max(1, Math.min(30, number(currentState?.link?.display_fps) || number(currentState?.link?.fps) || 10));
    const delay = Math.max(12, (1000 / sourceFps) - (performance.now() - started));
    window.setTimeout(requestPreview, delay);
  }

  async function sendHermes(prompt) {
    if (hermesStreaming) return;
    hermesStreaming = true;
    $("assistant-form").querySelector("button[type=submit]").disabled = true;
    setText("assistant-send-status", "正在连接 Hermes");
    setText("assistant-activity", "处理中");
    setText("assistant-reply", "正在思考…");
    $("assistant-tool").hidden = true;
    let reply = "";
    try {
      const response = await fetch("/api/hermes/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `Hermes ${response.status}`);
      }
      if (!response.body) throw new Error("Hermes 流式响应不可用");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const packets = buffer.split("\n\n");
        buffer = packets.pop() || "";
        for (const packet of packets) {
          const line = packet.split("\n").find((item) => item.startsWith("data: "));
          if (!line) continue;
          const event = JSON.parse(line.slice(6));
          const payload = event.payload || {};
          if (event.type === "message.start") {
            reply = "";
            setText("assistant-reply", "正在思考…");
          } else if (event.type === "message.delta") {
            reply += payload.text || "";
            setText("assistant-reply", reply);
          } else if (event.type === "tool.start") {
            $("assistant-tool").hidden = false;
            setText("assistant-tool", payload.context || `正在使用 ${payload.name || "工具"}`);
          } else if (event.type === "tool.complete") {
            $("assistant-tool").hidden = false;
            setText("assistant-tool", payload.summary || `${payload.name || "工具"} 已完成`);
          } else if (event.type === "status.update") {
            setText("assistant-send-status", payload.text || "Hermes 正在处理");
          } else if (event.type === "message.complete") {
            reply = payload.text || reply;
            setText("assistant-reply", reply);
          } else if (event.type === "done") {
            setText("assistant-reply", payload.reply || payload.error || reply || "Hermes 已完成");
          }
        }
        if (done) break;
      }
      $("assistant-prompt").value = "";
    } catch (error) {
      setText("assistant-reply", `请求失败：${error.message}`);
      showToast(error.message, true);
    } finally {
      hermesStreaming = false;
      $("assistant-form").querySelector("button[type=submit]").disabled = false;
      setText("assistant-send-status", "");
      await refreshState();
    }
  }

  function postJson(path, body, successMessage) {
    return runAction(() => api(path, { method: "POST", body: JSON.stringify(body) }), successMessage);
  }

  function bindEvents() {
    all("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
    all("[data-go]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.go)));
    all("[data-scene]").forEach((button) => button.addEventListener("click", () => {
      activeModuleId = null;
      postJson(`/api/scene/${button.dataset.scene}`, {}, "设备页面已切换");
    }));
    window.addEventListener("hashchange", () => setView(location.hash.slice(1), false));
    window.addEventListener("keydown", handleEspControlKey, true);
    window.addEventListener("keyup", handleEspControlKeyUp, true);
    window.addEventListener("message", (event) => {
      if (event.origin !== location.origin || event.data?.type !== "espagent:control-key") return;
      if (event.data.phase === "up") {
        handleEspControlKeyUp({
          ...event.data,
          preventDefault() {},
          stopPropagation() {},
        });
      } else {
        pressedShortcutCodes.add(event.data.code);
        processEspControlKey(event.data);
      }
    });
    $("esp-control-toggle").addEventListener("click", () => setEspControlEnabled(!espControlEnabled));
    $("module-frame").addEventListener("load", () => {
      syncEspControlIndicator();
      syncModuleVisibility(currentView.startsWith("module/") ? currentView.slice("module/".length) : null);
    });

    $("assistant-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const prompt = $("assistant-prompt").value.trim();
      if (prompt) sendHermes(prompt);
    });
    $("assistant-new-session").addEventListener("click", () => postJson("/api/hermes/session/new", {}, "新会话已创建"));
    $("assistant-interrupt").addEventListener("click", () => postJson("/api/hermes/interrupt", {}, "当前任务已打断"));
    $("voice-enable").addEventListener("click", () => postJson("/api/voice/enable", { enabled: !Boolean(currentState?.hermes?.voice_enabled) }, "语音输入状态已更新"));
    $("voice-record").addEventListener("click", () => {
      const listening = ["listening", "recording"].includes(currentState?.hermes?.voice_status);
      postJson(listening ? "/api/voice/record/stop" : "/api/voice/record/start", {}, listening ? "录音已结束" : "录音已开始");
    });
    $("voice-silence").addEventListener("change", () => postJson("/api/voice/silence", { seconds: number($("voice-silence").value, 2) }, "静音结束时间已更新"));

    $("light-power").addEventListener("click", () => postJson("/api/light", {
      on: !Boolean(currentState?.light?.on),
      brightness: number($("light-brightness").value, 40),
    }, "灯具状态已发送"));
    $("light-brightness").addEventListener("input", () => {
      setText("light-brightness-output", `${$("light-brightness").value}%`);
      setText("light-percent", $("light-brightness").value);
    });
    $("light-brightness").addEventListener("change", () => postJson("/api/light", { on: true, brightness: number($("light-brightness").value, 40) }, "亮度已发送"));
    $("light-temperature-control").addEventListener("input", () => setText("light-temperature-output", `${$("light-temperature-control").value} K`));
    $("light-temperature-control").addEventListener("change", () => postJson("/api/light", {
      on: true,
      brightness: number($("light-brightness").value, 40),
      color_temp_kelvin: number($("light-temperature-control").value, 4000),
    }, "色温已发送"));
    all("[data-light-level]").forEach((button) => button.addEventListener("click", () => postJson("/api/light", { on: true, brightness: number(button.dataset.lightLevel) }, `亮度已设为 ${button.dataset.lightLevel}%`)));

    all("[data-transport]").forEach((button) => button.addEventListener("click", () => postJson("/api/display/transport", { transport_mode: button.dataset.transport }, `已切换为 ${button.textContent.trim()}`)));
    $("indexed-bpp").addEventListener("change", () => postJson("/api/display/transport", { indexed_bpp: number($("indexed-bpp").value) }, "索引格式已更新"));
    $("mjpeg-quality").addEventListener("input", () => setText("mjpeg-quality-output", $("mjpeg-quality").value));
    $("mjpeg-quality").addEventListener("change", () => postJson("/api/display/transport", { mjpeg_quality: number($("mjpeg-quality").value) }, "JPEG 质量已更新"));
    $("mjpeg-subsampling").addEventListener("change", () => postJson("/api/display/transport", { mjpeg_subsampling: $("mjpeg-subsampling").value }, "色度采样已更新"));
    $("nack-toggle").addEventListener("click", () => postJson("/api/display/transport", { nack_enabled: !switchValue("nack-toggle") }, "NACK 设置已更新"));
    $("backlight").addEventListener("input", () => setText("backlight-output", `${$("backlight").value}%`));
    $("backlight").addEventListener("change", () => postJson("/api/display/transport", { backlight_percent: number($("backlight").value) }, "背光已更新"));
    $("panel-gamma").addEventListener("change", () => postJson("/api/display/transport", { panel_gamma: $("panel-gamma").value }, "Gamma 已更新"));
    $("preview-limit-toggle").addEventListener("click", () => postJson("/api/display/transport", { preview_fps_limited: !switchValue("preview-limit-toggle") }, "预览限制已更新"));
    $("preview-limit").addEventListener("change", () => postJson("/api/display/transport", { preview_fps_limit: number($("preview-limit").value, 30) }, "预览帧率已更新"));

    all("[data-theme]").forEach((button) => button.addEventListener("click", () => updateDisplayUi({ theme: button.dataset.theme })));
    all("[data-orientation]").forEach((button) => button.addEventListener("click", () => updateDisplayUi({ orientation: button.dataset.orientation })));
    ["font-chinese", "font-latin", "font-numeric", "ui-scale", "font-hinting"].forEach((id) => {
      $(id).addEventListener("change", () => {
        const key = id === "ui-scale" ? "scale_percent" : id.replaceAll("-", "_");
        const value = id === "ui-scale" ? number($(id).value) : $(id).value;
        updateDisplayUi({ [key]: value });
      });
    });
    $("grayscale-toggle").addEventListener("click", () => updateDisplayUi({ grayscale_antialiasing: !switchValue("grayscale-toggle") }));
    $("motion-toggle").addEventListener("click", () => updateDisplayUi({ motion_enabled: !switchValue("motion-toggle") }));
    $("tone-toggle").addEventListener("click", () => postJson("/api/display/tone", { enabled: !switchValue("tone-toggle") }, "输出调校状态已更新"));
    $("tone-preset").addEventListener("change", () => postJson("/api/display/tone", tonePresets[$("tone-preset").value], "输出预设已应用"));
    $("shortcut-toggle-capture").addEventListener("click", () => setShortcutCapture("toggle"));
    $("shortcut-voice-capture").addEventListener("click", () => setShortcutCapture("voice"));
    $("shortcut-navigation-capture").addEventListener("click", () => setShortcutCapture("navigation"));
    $("shortcut-scroll-capture").addEventListener("click", () => setShortcutCapture("scroll"));
    $("shortcut-exit-capture").addEventListener("click", () => setShortcutCapture("exit"));
    $("shortcut-exit-mode").addEventListener("change", () => {
      setShortcutCapture(null);
      postJson("/api/settings/shortcuts", { exit_mode: $("shortcut-exit-mode").value }, "退出快捷键已更新");
    });
    [
      ["shortcut-navigation-mode", "navigation"],
      ["shortcut-scroll-mode", "scroll"],
    ].forEach(([id, target]) => $(id).addEventListener("change", () => {
      setShortcutCapture(null);
      postJson("/api/settings/shortcuts", { [`${target}_mode`]: $(id).value }, "方向快捷键已更新");
    }));
    $("shortcut-reset").addEventListener("click", () => {
      setShortcutCapture(null);
      postJson("/api/settings/shortcuts", shortcutDefaults, "快捷键已恢复默认");
    });
    $("firmware-flash").addEventListener("click", startFirmwareFlash);
    $("firmware-wifi-form").addEventListener("submit", startWifiProvisioning);
    $("firmware-wifi-ssid").addEventListener("input", updateWifiConfigureButton);
    $("firmware-wifi-password").addEventListener("input", updateWifiConfigureButton);
    $("firmware-gpio-form").addEventListener("submit", configureDisplayGpio);
    $("firmware-gpio-read").addEventListener("click", readDisplayGpio);
    $("firmware-gpio-reset").addEventListener("click", resetDisplayGpioForm);
    all("[data-gpio-key]").forEach((input) => input.addEventListener("input", () => {
      displayGpioDirty = true;
      updateDisplayGpioButtons();
    }));

    all("[data-pattern]").forEach((button) => button.addEventListener("click", () =>
      postJson(`/api/test-pattern/${button.dataset.pattern}`, {}, "测试画面已发送")
    ));
    $("video-test").addEventListener("click", () =>
      postJson("/api/video/start", {}, "视频测试已开始")
    );
    $("esp-log-toggle").addEventListener("click", () => postJson("/api/display/transport", { esp_log_enabled: !Boolean(currentState?.link?.esp_log_enabled) }, "ESP 日志设置已更新"));
    $("copy-state").addEventListener("click", copyState);
  }

  function updateDisplayUi(patch) {
    if (patch.theme === "second" && currentState?.display_ui?.font_chinese === "humanist") patch.font_chinese = "auto";
    return postJson("/api/display/ui", patch, "设备界面设置已更新");
  }

  async function copyState() {
    const text = JSON.stringify(currentState || {}, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      showToast("状态 JSON 已复制");
    } catch (_) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
      showToast("状态 JSON 已复制");
    }
  }

  async function initialize() {
    bindEvents();
    syncEspControlIndicator();
    previewRunning = false;
    await loadModules();
    setView(location.hash.slice(1) || "overview", false);
    await refreshState();
    window.setInterval(refreshState, 1000);
  }

  window.addEventListener("beforeunload", () => {
    previewRunning = false;
    if (voiceShortcutActive) {
      fetch("/api/hermes/input/voice/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        keepalive: true,
      }).catch(() => {});
    }
    if (previewBlobUrl) URL.revokeObjectURL(previewBlobUrl);
  });
  initialize();
})();

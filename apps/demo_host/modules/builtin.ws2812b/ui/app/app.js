(() => {
  "use strict";
  const moduleId = "builtin.ws2812b";
  const $ = (id) => document.getElementById(id);
  let state = null;
  let colorTimer = null;
  let brightnessTimer = null;
  let effectSignature = "";
  const editing = new Set();

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

  async function invoke(action, payload = {}) {
    const envelope = await api(`/api/modules/${moduleId}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({ payload }),
    });
    state = envelope.result;
    render();
  }

  function render() {
    if (!state) return;
    const online = Boolean(state.online);
    const flash = state.flash || {};
    const firmwareTargets = state.firmware_targets || [];
    const activeTarget = firmwareTargets.find((target) => target.id === state.firmware_target) || firmwareTargets[0] || {};
    const labels = { stopped: "已停止", connecting: "正在连接", online: "在线", offline: "离线" };
    $("status").textContent = labels[state.status] || state.status;
    $("device-name").textContent = state.device_name || "ESP-01S / WLED";
    $("device-ip").textContent = state.ip ? `LAN ${state.ip}` : "LAN --";
    $("firmware").textContent = state.firmware ? `WLED ${state.firmware}` : "WLED --";
    $("led-count").textContent = state.led_count ? `LED ${state.led_count}` : "LED --";
    $("architecture").textContent = state.architecture || "--";
    $("signal").textContent = state.signal == null ? "--" : `${state.signal}%`;
    $("command-state").textContent = state.command_pending ? "SENDING" : "IDLE";
    $("provision-status").textContent = state.provision_status || "连接 USB 后可首次配网或修改现有网络。";
    $("network-port").textContent = state.provision_port ? `PORT ${state.provision_port}` : "PORT --";
    $("network-ssid").textContent = state.provision_ssid ? `SSID ${state.provision_ssid}` : "SSID --";
    $("network-ip").textContent = state.provision_ip ? `IP ${state.provision_ip}` : "IP --";
    $("provision").disabled = Boolean(state.provisioning) || (state.serial_ports || []).length === 0;
    $("refresh-ports").disabled = Boolean(state.provisioning);
    $("provision").textContent = state.provisioning ? "正在写入" : "写入并连接";
    $("flash-version").textContent = flash.firmware_version ? `WLED ${flash.firmware_version}` : "WLED --";
    $("flash-port").textContent = flash.device?.device || "--";
    $("flash-state").textContent = String(flash.status || "idle").toUpperCase();
    $("flash-message").textContent = flash.message || `连接 ${activeTarget.name || "控制器"} 后即可烧录`;
    $("flash-firmware").disabled = !flash.can_flash;
    $("flash-firmware").textContent = flash.running ? "正在烧录" : "烧录 WLED";
    $("flash-target-description").textContent = activeTarget.description
      ? `${activeTarget.description}。应用内置 WLED 与烧录器，无需安装 ESP-IDF。`
      : "应用内置 WLED 与烧录器，无需安装 ESP-IDF。";
    $("hardware-note").textContent = activeTarget.hardware_note || "选择开发板后显示接线说明。";
    const targetSelect = $("firmware-target");
    const targetSignature = JSON.stringify(firmwareTargets);
    if (targetSelect.dataset.signature !== targetSignature) {
      targetSelect.replaceChildren(...firmwareTargets.map((target) => {
        const option = document.createElement("option");
        option.value = target.id;
        option.textContent = `${target.name} · ${target.description}`;
        return option;
      }));
      targetSelect.dataset.signature = targetSignature;
    }
    targetSelect.value = state.firmware_target || "";
    targetSelect.disabled = Boolean(flash.running);
    const progress = Math.max(0, Math.min(100, Number(flash.progress || 0)));
    $("flash-progress").style.width = `${progress}%`;
    $("flash-progress").parentElement.setAttribute("aria-valuenow", String(progress));
    $("flash-log").textContent = (flash.logs || []).join("\n") || "暂无日志";
    $("power").setAttribute("aria-checked", state.power ? "true" : "false");
    $("power-detail").textContent = state.power ? "灯条已开启" : "灯条已关闭";
    renderEffects(online);
    if (!editing.has("color")) $("color").value = state.color || "#ffffff";
    if (!editing.has("brightness")) $("brightness").value = String(state.brightness ?? 100);
    if (!editing.has("led-count")) $("led-count-input").value = String(state.led_count ?? 60);
    $("color-value").textContent = $("color").value.toUpperCase();
    $("brightness-value").textContent = `${$("brightness").value}%`;
    if (document.activeElement !== $("endpoint")) $("endpoint").value = state.endpoint || "http://wled.local";
    $("light-preview").style.setProperty("--light-color", state.color || "#ffffff");
    $("light-preview").style.setProperty("--light-alpha", state.power ? String(Math.max(.08, Number(state.brightness || 0) / 100)) : ".04");
    $("error").hidden = !state.error;
    $("error").textContent = state.error || "";
    $("power").disabled = !online;
    $("color").disabled = !online;
    $("brightness").disabled = !online;
    $("led-count-input").disabled = !online;
    $("save-led-count").disabled = !online;
    document.querySelectorAll(".swatches button").forEach((button) => { button.disabled = !online; });
    const portSelect = $("serial-port");
    const portSignature = JSON.stringify(state.serial_ports || []);
    if (portSelect.dataset.signature !== portSignature) {
      const previous = portSelect.value;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = (state.serial_ports || []).length === 1 ? "自动选择唯一串口" : "自动检测";
      const options = (state.serial_ports || []).map((port) => {
        const option = document.createElement("option");
        option.value = port.device;
        option.textContent = `${port.device} · ${port.description || "USB 串口"}`;
        return option;
      });
      portSelect.replaceChildren(placeholder, ...options);
      portSelect.dataset.signature = portSignature;
      portSelect.value = [...portSelect.options].some((option) => option.value === previous) ? previous : "";
    }
  }

  function renderEffects(online) {
    const presets = [
      {
        id: "solid",
        name: "纯色",
        effect_name: "Solid",
        description: "使用当前颜色静态点亮",
        preview: ["#2d333b", "#aeb5bf", "#ffffff"],
        supported: true,
      },
      ...(state.effect_presets || []),
    ];
    const signature = JSON.stringify(presets.map(({ id, name, effect_name, description, preview, supported }) => (
      { id, name, effect_name, description, preview, supported }
    )));
    const grid = $("effect-grid");
    if (signature !== effectSignature) {
      effectSignature = signature;
      const buttons = presets.map((preset) => {
        const button = document.createElement("button");
        const sample = document.createElement("i");
        const labels = document.createElement("span");
        const name = document.createElement("strong");
        const sourceName = document.createElement("small");
        button.type = "button";
        button.className = "effect-option";
        button.dataset.presetId = preset.id;
        button.title = preset.description;
        button.setAttribute("aria-label", `${preset.name}：${preset.description}`);
        const colors = preset.preview || ["#171a20", "#5b626b", "#ffffff"];
        sample.style.setProperty("--effect-a", colors[0]);
        sample.style.setProperty("--effect-b", colors[1]);
        sample.style.setProperty("--effect-c", colors[2]);
        name.textContent = preset.name;
        sourceName.textContent = preset.supported === false ? "固件不支持" : preset.effect_name;
        labels.append(name, sourceName);
        button.append(sample, labels);
        return button;
      });
      grid.replaceChildren(...buttons);
    }
    grid.querySelectorAll(".effect-option").forEach((button) => {
      const preset = presets.find((item) => item.id === button.dataset.presetId);
      const active = button.dataset.presetId === state.active_effect_preset;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.disabled = !online || preset?.supported !== true;
    });
    $("active-effect").textContent = state.active_effect_name || "Solid";
    const catalogLabels = {
      pending: "正在读取 WLED 效果列表",
      ready: `${(state.effect_presets || []).filter((preset) => preset.supported).length}/10 个 WLED 官方特效可用`,
      error: state.effect_catalog_error || "WLED 效果列表读取失败",
    };
    $("effect-status").textContent = catalogLabels[state.effect_catalog_status] || "";
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

  $("refresh").addEventListener("click", () => invoke("refresh"));
  $("refresh-ports").addEventListener("click", () => invoke("refresh").catch(showError));
  $("save-endpoint").addEventListener("click", () => invoke("configure", { endpoint: $("endpoint").value }).catch(showError));
  $("endpoint").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("save-endpoint").click();
  });
  $("power").addEventListener("click", () => invoke("set_power", { power: !state?.power }).catch(showError));
  $("led-count-input").addEventListener("focus", () => editing.add("led-count"));
  $("led-count-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("save-led-count").click();
  });
  $("save-led-count").addEventListener("click", () => invoke("set_led_count", {
    led_count: Number($("led-count-input").value),
  }).then(() => editing.delete("led-count")).catch(showError));
  $("firmware-target").addEventListener("change", () => invoke("set_firmware_target", {
    target: $("firmware-target").value,
  }).catch(showError));
  $("flash-firmware").addEventListener("click", () => invoke("flash_firmware", {
    port: $("serial-port").value,
  }).catch(showError));
  $("provision").addEventListener("click", () => invoke("provision_wifi", {
    port: $("serial-port").value,
    ssid: $("wifi-ssid").value,
    password: $("wifi-password").value,
  }).then(() => { $("wifi-password").value = ""; }).catch(showError));
  $("color").addEventListener("input", () => {
    editing.add("color");
    $("color-value").textContent = $("color").value.toUpperCase();
    $("light-preview").style.setProperty("--light-color", $("color").value);
    clearTimeout(colorTimer);
    colorTimer = setTimeout(() => invoke("set_color", { color: $("color").value }).catch(showError).finally(() => editing.delete("color")), 90);
  });
  $("brightness").addEventListener("input", () => {
    editing.add("brightness");
    $("brightness-value").textContent = `${$("brightness").value}%`;
    $("light-preview").style.setProperty("--light-alpha", String(Math.max(.08, Number($("brightness").value) / 100)));
    clearTimeout(brightnessTimer);
    brightnessTimer = setTimeout(() => invoke("set_brightness", { brightness: Number($("brightness").value) }).catch(showError).finally(() => editing.delete("brightness")), 90);
  });
  document.querySelectorAll(".swatches button").forEach((button) => button.addEventListener("click", () => {
    $("color").value = button.dataset.color;
    $("color").dispatchEvent(new Event("input"));
  }));
  $("effect-grid").addEventListener("click", (event) => {
    const button = event.target.closest(".effect-option");
    if (!button || button.disabled) return;
    invoke("set_effect", { preset_id: button.dataset.presetId }).catch(showError);
  });

  function showError(error) {
    $("error").hidden = false;
    $("error").textContent = error.message;
  }

  refresh();
  window.setInterval(refresh, 1000);
})();

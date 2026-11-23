// 串流版本更新监控 · 设置页
// 通过 window.AstrBotPluginView（旧别名 AstrBotPluginPage）与插件后端通信。

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

let state = null;
let groups = [];

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("err", !!isError);
  el.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function askConfirm(message) {
  return new Promise((resolve) => {
    const modal = $("confirmModal");
    const text = $("confirmText");
    const ok = $("confirmOk");
    const cancel = $("confirmCancel");
    if (!modal || !text || !ok || !cancel) {
      resolve(false);
      return;
    }
    text.textContent = message;
    modal.classList.remove("hidden");

    const cleanup = (result) => {
      modal.classList.add("hidden");
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onKey = (event) => {
      if (event.key === "Escape") cleanup(false);
      if (event.key === "Enter") cleanup(true);
    };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
    ok.focus?.();
  });
}

async function apiGet(endpoint, params) {
  if (!bridge) throw new Error("页面桥接未加载，请从 AstrBot 插件页面重新打开");
  return bridge.apiGet(endpoint, params || {});
}

async function apiPost(endpoint, body) {
  if (!bridge) throw new Error("页面桥接未加载，请从 AstrBot 插件页面重新打开");
  return bridge.apiPost(endpoint, body || {});
}

function renderGroups() {
  const box = $("groupChips");
  box.innerHTML = "";
  groups.forEach((gid, index) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = gid;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "移除";
    remove.addEventListener("click", () => {
      groups.splice(index, 1);
      renderGroups();
    });
    chip.appendChild(remove);
    box.appendChild(chip);
  });
}

function addGroupFromInput() {
  const input = $("groupInput");
  const raw = (input.value || "").trim();
  if (!raw) return;
  const candidates = raw
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  for (const item of candidates) {
    let gid = item;
    if (gid.includes(":")) {
      const parts = gid.split(":");
      gid = parts[parts.length - 1];
    }
    if (!/^\d{4,}$/.test(gid)) {
      toast(`「${item}」不是有效的群号`, true);
      continue;
    }
    if (!groups.includes(gid)) groups.push(gid);
  }
  input.value = "";
  renderGroups();
}

function setSwitch(id, value) {
  $(id).checked = !!value;
}

const UPLOAD_FOLDER_MAP = {
  u_sunshine_stable: { key: "sunshine", label: "Sunshine 正式版" },
  u_sunshine_pre: { key: "sunshine", label: "Sunshine 预发布版" },
  u_vplus: { key: "vplus", label: "Moonlight V+（安卓）" },
  u_pc: { key: "pc", label: "Moonlight V+ PC" },
};

function folderNames() {
  return {
    sunshine: ($("u_folder_sunshine").value || "").trim(),
    vplus: ($("u_folder_vplus").value || "").trim(),
    pc: ($("u_folder_pc").value || "").trim(),
  };
}

// 没填对应群文件夹名称时，不允许开启文件上传开关
function updateUploadConstraints() {
  const names = folderNames();
  for (const [id, meta] of Object.entries(UPLOAD_FOLDER_MAP)) {
    const el = $(id);
    if (!el) continue;
    const allowed = !!names[meta.key];
    el.disabled = !allowed;
    if (!allowed && el.checked) el.checked = false;
    const row = el.closest(".switch");
    if (row) row.classList.toggle("disabled", !allowed);
  }
  const anyName = !!(names.sunshine || names.vplus || names.pc);
  for (const id of ["u_enabled", "u_dry_run"]) {
    const el = $(id);
    if (!el) continue;
    el.disabled = !anyName;
    if (!anyName && el.checked) el.checked = false;
    const row = el.closest(".switch");
    if (row) row.classList.toggle("disabled", !anyName);
  }
}

function uploadValidationError() {
  const names = folderNames();
  for (const [id, meta] of Object.entries(UPLOAD_FOLDER_MAP)) {
    if ($(id)?.checked && !names[meta.key]) {
      return `「${meta.label}」的群文件夹名称为空，不能开启该上传开关`;
    }
  }
  if ($("u_enabled")?.checked && !(names.sunshine || names.vplus || names.pc)) {
    return "文件上传总开关开启前，请先至少填写一个群文件夹名称";
  }
  return null;
}

function render(settings) {
  state = settings;
  const g = settings.global || {};
  const n = settings.notify || {};
  const u = settings.upload || {};
  const runtime = settings.runtime || {};

  setSwitch("g_enable", g.enable);
  setSwitch("g_prerelease", g.include_prerelease);
  setSwitch("g_dry_run", g.dry_run);
  $("g_interval").value = g.interval_minutes ?? 15;
  $("g_delay").value = g.initial_delay_seconds ?? 20;
  $("g_upload_group").value = g.upload_group_id || "";
  $("g_summary_chars").value = g.summary_max_chars ?? 220;
  $("g_provider").value = g.summary_provider_id || "";

  groups = Array.isArray(g.target_groups) ? g.target_groups.map((x) => String(x)) : [];
  renderGroups();

  setSwitch("n_enabled", n.enabled);
  setSwitch("n_sunshine_stable", n.sunshine_stable);
  setSwitch("n_sunshine_pre", n.sunshine_prerelease);
  setSwitch("n_vplus", n.moonlight_vplus);
  setSwitch("n_pc", n.moonlight_pc);

  setSwitch("u_enabled", u.enabled);
  setSwitch("u_dry_run", u.dry_run);
  setSwitch("u_sunshine_stable", u.sunshine_stable);
  setSwitch("u_sunshine_pre", u.sunshine_prerelease);
  setSwitch("u_vplus", u.moonlight_vplus);
  setSwitch("u_pc", u.moonlight_pc);
  setSwitch("u_cleanup", u.cleanup_when_full);
  $("u_keep").value = u.cleanup_keep_count ?? 2;
  $("u_max_mb").value = u.asset_max_mb ?? 200;
  $("u_timeout").value = u.download_timeout_seconds ?? 600;
  $("u_folder_sunshine").value = u.sunshine_folder_name || "";
  $("u_folder_vplus").value = u.moonlight_android_folder_name || "";
  $("u_folder_pc").value = u.moonlight_pc_folder_name || "";
  $("u_id_sunshine").value = u.sunshine_folder_id || "";
  $("u_id_vplus").value = u.moonlight_android_folder_id || "";
  $("u_id_pc").value = u.moonlight_pc_folder_id || "";

  updateUploadConstraints();

  const pill = $("runState");
  if (runtime.running) {
    pill.textContent = "监控运行中";
    pill.className = "pill pill-on";
  } else {
    pill.textContent = "监控已停止";
    pill.className = "pill pill-off";
  }
}

function collect() {
  return {
    global: {
      enable: $("g_enable").checked,
      platform_id: (state?.global?.platform_id || "").trim(),
      target_groups: groups.slice(),
      upload_group_id: $("g_upload_group").value.trim(),
      interval_minutes: Number($("g_interval").value) || 15,
      include_prerelease: $("g_prerelease").checked,
      summary_max_chars: Number($("g_summary_chars").value) || 220,
      summary_provider_id: $("g_provider").value.trim(),
      dry_run: $("g_dry_run").checked,
      initial_delay_seconds: Number($("g_delay").value) || 20,
    },
    notify: {
      enabled: $("n_enabled").checked,
      sunshine_stable: $("n_sunshine_stable").checked,
      sunshine_prerelease: $("n_sunshine_pre").checked,
      moonlight_vplus: $("n_vplus").checked,
      moonlight_pc: $("n_pc").checked,
    },
    upload: {
      enabled: $("u_enabled").checked,
      dry_run: $("u_dry_run").checked,
      sunshine_stable: $("u_sunshine_stable").checked,
      sunshine_prerelease: $("u_sunshine_pre").checked,
      moonlight_vplus: $("u_vplus").checked,
      moonlight_pc: $("u_pc").checked,
      sunshine_folder_name: $("u_folder_sunshine").value.trim(),
      moonlight_android_folder_name: $("u_folder_vplus").value.trim(),
      moonlight_pc_folder_name: $("u_folder_pc").value.trim(),
      sunshine_folder_id: $("u_id_sunshine").value.trim(),
      moonlight_android_folder_id: $("u_id_vplus").value.trim(),
      moonlight_pc_folder_id: $("u_id_pc").value.trim(),
      cleanup_when_full: $("u_cleanup").checked,
      cleanup_keep_count: Number($("u_keep").value) || 2,
      download_timeout_seconds: Number($("u_timeout").value) || 600,
      asset_max_mb: Number($("u_max_mb").value) || 200,
    },
  };
}

async function load() {
  try {
    const settings = await apiGet("settings");
    render(settings);
  } catch (err) {
    toast(`读取设置失败：${err?.message || err}`, true);
  }
}

async function save() {
  const btn = $("btnSave");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    const validationError = uploadValidationError();
    if (validationError) {
      toast(validationError, true);
      return;
    }
    const payload = collect();
    const result = await apiPost("settings", payload);
    if (result?.settings) render(result.settings);
    toast("已保存");
  } catch (err) {
    toast(`保存失败：${err?.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "保存设置";
  }
}

async function checkNow() {
  const btn = $("btnCheck");
  btn.disabled = true;
  btn.textContent = "检查中…";
  try {
    const result = await apiPost("check", {});
    const report = result?.report || [];
    $("logBox").textContent = report.length ? report.join("\n") : "检查完成，没有新版本。";
    toast("检查完成");
    await load();
  } catch (err) {
    toast(`检查失败：${err?.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "立即检查";
  }
}

async function testNotify() {
  if (!(await askConfirm("会按当前开启的通知开关，把各项目最新版本以【测试通知】推送到配置的群里，继续？"))) return;
  toast("开始测试通知…");
  const btn = $("btnTestNotify");
  btn.disabled = true;
  btn.textContent = "推送中…";
  try {
    const result = await apiPost("test/notify", {});
    const lines = result?.results || [];
    $("logBox").textContent = lines.join("\n") || "没有可测试的通知版本。";
    toast(`测试通知完成：已推送 ${result?.sent ?? 0} 条`);
    await load();
  } catch (err) {
    toast(`测试通知失败：${err?.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "测试通知";
  }
}

async function testUpload() {
  if (!(await askConfirm("会真实下载各项目当前最新安装包并上传到群文件夹（空间不足可能触发清理旧包），继续？"))) return;
  toast("开始测试上传（下载较慢，请稍候）…");
  const btn = $("btnTestUpload");
  btn.disabled = true;
  btn.textContent = "上传中…";
  try {
    const result = await apiPost("test/upload", {});
    const lines = result?.results || [];
    $("logBox").textContent = lines.join("\n") || "没有可测试的上传版本。";
    toast(`测试上传完成：已上传 ${result?.uploaded ?? 0} 个`);
    await load();
  } catch (err) {
    toast(`测试上传失败：${err?.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "测试上传";
  }
}

async function refreshLog() {
  try {
    const result = await apiGet("log");
    const lines = result?.lines || [];
    $("logBox").textContent = lines.length ? lines.join("\n") : "暂无日志。";
  } catch (err) {
    toast(`读取日志失败：${err?.message || err}`, true);
  }
}

async function openGroupPicker() {
  const picker = $("groupPicker");
  picker.classList.remove("hidden");
  const list = $("groupList");
  list.textContent = "读取中…";
  try {
    const result = await apiGet("groups");
    const rows = result?.groups || [];
    list.innerHTML = "";
    if (!rows.length) {
      list.textContent = "没有读到群列表（检查平台是否在线）。";
      return;
    }
    for (const row of rows) {
      const gid = String(row.group_id || "").trim();
      if (!gid) continue;
      const el = document.createElement("div");
      el.className = "picker-row";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = row.group_name ? `${row.group_name}（${gid}）` : gid;
      const add = document.createElement("button");
      add.type = "button";
      add.className = "btn btn-ghost";
      add.textContent = groups.includes(gid) ? "已添加" : "添加";
      add.disabled = groups.includes(gid);
      add.addEventListener("click", () => {
        if (!groups.includes(gid)) {
          groups.push(gid);
          renderGroups();
          add.textContent = "已添加";
          add.disabled = true;
          toast(`已添加群 ${gid}`);
        }
      });
      el.appendChild(name);
      el.appendChild(add);
      list.appendChild(el);
    }
  } catch (err) {
    list.textContent = `读取群列表失败：${err?.message || err}`;
  }
}

const FOLDER_ID_INPUT = {
  u_folder_sunshine: "u_id_sunshine",
  u_folder_vplus: "u_id_vplus",
  u_folder_pc: "u_id_pc",
};
let folderListCache = null; // { ts, group_id, folders }

function withTimeout(promise, ms, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(message)), ms)),
  ]);
}

async function fetchFolderList(force = false) {
  const now = Date.now();
  if (!force && folderListCache && now - folderListCache.ts < 120000) {
    return folderListCache;
  }
  const result = await withTimeout(apiGet("folders" + (force ? "?refresh=1" : "")), 30000, "读取群文件夹超时（30 秒）");
  const data = {
    ts: now,
    group_id: result?.group_id || "",
    folders: result?.folders || [],
  };
  folderListCache = data;
  return data;
}

async function browseFolders(inputId) {
  const target = $(inputId);
  if (!target) return;
  const existing = document.querySelector(`select[data-folder-for="${inputId}"]`);
  if (existing) {
    existing.remove();
    return;
  }
  const btn = document.querySelector(`[data-folder-browse="${inputId}"]`);
  const btnText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "读取中…";
  }
  try {
    const data = await fetchFolderList(false);
    const folders = data.folders || [];
    if (!folders.length) {
      toast("没有读到群文件夹，检查是否已配置推送群/上传群。", true);
      return;
    }
    const select = document.createElement("select");
    select.className = "folder-select";
    select.dataset.folderFor = inputId;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = `选择群文件夹（当前群 ${data.group_id || ""}，共 ${folders.length} 个）`;
    select.appendChild(placeholder);
    for (const f of folders) {
      const option = document.createElement("option");
      option.value = f.folder_name || "";
      option.dataset.folderId = f.folder_id || "";
      option.textContent = `${f.folder_name}（${f.file_count || 0} 个文件）`;
      select.appendChild(option);
    }
    select.addEventListener("change", () => {
      const picked = select.selectedOptions && select.selectedOptions[0];
      if (select.value) {
        target.value = select.value;
        // 自动绑定 fallback ID：把选中的 folder_id 写进高级设置里对应的字段
        const idInput = $(FOLDER_ID_INPUT[inputId]);
        const folderId = picked?.dataset?.folderId || "";
        if (idInput && folderId) {
          idInput.value = folderId;
          toast("已填入文件夹名称并自动绑定文件夹 ID");
        } else {
          toast(`已填入文件夹：${select.value}`);
        }
        updateUploadConstraints();
      }
      select.remove();
    });
    target.parentElement.insertAdjacentElement("afterend", select);
  } catch (err) {
    toast(`读取文件夹失败：${err?.message || err}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = btnText;
    }
  }
}

async function main() {
  if (bridge?.ready) {
    try {
      await bridge.ready();
    } catch {
      /* 忽略 ready 超时，后续调用会给出明确错误 */
    }
  }
  $("btnSave").addEventListener("click", save);
  $("btnReload").addEventListener("click", load);
  $("btnCheck").addEventListener("click", checkNow);
  $("btnTestNotify").addEventListener("click", testNotify);
  $("btnTestUpload").addEventListener("click", testUpload);
  $("btnLog").addEventListener("click", refreshLog);
  $("btnAddGroup").addEventListener("click", addGroupFromInput);
  $("btnPickGroups").addEventListener("click", openGroupPicker);
  $("btnCloseGroupPicker").addEventListener("click", () => {
    $("groupPicker").classList.add("hidden");
  });
  document.querySelectorAll("[data-folder-browse]").forEach((btn) => {
    btn.addEventListener("click", () => browseFolders(btn.dataset.folderBrowse));
  });
  for (const id of ["u_folder_sunshine", "u_folder_vplus", "u_folder_pc"]) {
    $(id).addEventListener("input", updateUploadConstraints);
  }
  $("groupInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addGroupFromInput();
    }
  });
  await load();
}

main();

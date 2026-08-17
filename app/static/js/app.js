const state = {
      page: 1,
      pageSize: 25,
      totalPages: 0,
      categories: [],
      activeJobId: "",
      activeBatchId: "",
      evaluatingProjectId: "",
      currentRows: [],
      selectedProjectIds: new Set(),
      selectedProjectNames: new Map(),
      modalRequestToken: 0,
      activeModalProjectId: "",
      chatRequestToken: 0,
      activeChatSessionId: "",
      chatSessions: [],
      chatSending: false,
      chatTypewriterTimer: null,
      chatTypewriterToken: 0,
      contractDiscoveryRequestToken: 0,
      mainChatRequestToken: 0,
      mainChatProjectId: "",
      mainChatProjectName: "",
      mainChatSessions: [],
      mainActiveChatSessionId: "",
      mainChatSending: false,
      mainChatTypewriterTimer: null,
      mainChatTypewriterToken: 0,
      mainChatFloatOpen: false,
      mainChatFloatMinimized: false,
      mainChatFloatDrag: null,
      mainChatFloatResizeDrag: null,
      mainChatFloatPosition: null,
      mainChatFloatSize: null,
      layoutPreset: "wide-table",
      layoutRatio: 0.8,
      viewSettingsOpen: false,
      layoutResizeDrag: null,
      compactTable: false,
      tableZoom: 0.85,
      tablePanEnabled: false,
      tablePanDrag: null,
      modalOpener: null,
      historyTrendChart: null,
      historyTrendWidth: 0
    };

    let historyTrendResizeObserver = null;
    let historyTrendFrame = 0;
    const LAYOUT_PRESETS = {
      "wide-table": {ratio: 0.8, className: "layout-wide-table"},
      balanced: {ratio: 0.7, className: "layout-balanced"},
      stacked: {ratio: 0.8, className: "layout-stacked"}
    };

    function password() {
      const input = document.getElementById("passwordInput");
      return (input && input.value) || localStorage.getItem("agentPassword") || "";
    }
    async function savePassword() {
      const value = document.getElementById("passwordInput").value;
      try { localStorage.setItem("agentPassword", value); } catch (_) {}
      await withButtonBusy("savePasswordButton", "加载中", () => boot()).catch(showError);
    }
    /* Three states, not two. "Follow the system" has to remain reachable:
     * a two-way toggle silently opts the user out of their OS setting the
     * first time they press it, and there is then no way back. The inline
     * script in <head> applies the stored choice before first paint; this
     * only owns the control and the writes. */
    const THEME_ORDER = ["system", "light", "dark"];
    const THEME_LABELS = { system: "跟随系统", light: "浅色", dark: "深色" };
    const THEME_ICONS = { system: "◐", light: "☀", dark: "☾" };

    function currentTheme() {
      const attribute = document.documentElement.getAttribute("data-theme");
      return THEME_ORDER.includes(attribute) ? attribute : "system";
    }

    function applyTheme(theme) {
      const root = document.documentElement;
      if (theme === "system") root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", theme);
      try {
        if (theme === "system") localStorage.removeItem("riskAgentTheme");
        else localStorage.setItem("riskAgentTheme", theme);
      } catch (_) { /* private mode: the choice just will not persist */ }
      renderThemeToggle();
    }

    function renderThemeToggle() {
      const button = document.getElementById("themeToggle");
      if (!button) return;
      const theme = currentTheme();
      button.textContent = `${THEME_ICONS[theme]} ${THEME_LABELS[theme]}`;
      // The label names the CURRENT state, so the accessible name has to say
      // what pressing it will do, or a screen-reader user hears only "深色"
      // with no indication that it is a control.
      const next = THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length];
      button.setAttribute("aria-label", `配色：${THEME_LABELS[theme]}，点击切换为${THEME_LABELS[next]}`);
    }

    function initThemeToggle() {
      const button = document.getElementById("themeToggle");
      if (!button) return;
      renderThemeToggle();
      button.addEventListener("click", () => {
        const index = THEME_ORDER.indexOf(currentTheme());
        applyTheme(THEME_ORDER[(index + 1) % THEME_ORDER.length]);
      });
    }

    function showError(err) {
      document.getElementById("projectStatus").textContent = "加载失败：" + err.message;
      document.getElementById("riskSummary").textContent = "加载失败：" + err.message;
    }
    function showDashboardError(err) {
      document.getElementById("metricProjects").textContent = "-";
      document.getElementById("projectStatus").textContent = "看板加载较慢，可先使用项目列表。";
      console.warn(err);
    }
    async function withButtonBusy(id, busyText, fn) {
      const button = document.getElementById(id);
      const original = button ? button.textContent : "";
      if (button) {
        button.disabled = true;
        button.textContent = busyText;
      }
      try {
        return await fn();
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      }
    }
    async function api(path, options = {}) {
      const headers = Object.assign({"X-Agent-Password": password()}, options.headers || {});
      const response = await fetch(path, Object.assign({}, options, {headers}));
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function pick(row, names) {
      for (const name of names) if (row[name] !== undefined && row[name] !== null) return row[name];
      return "";
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }

    function riskClass(level) {
      return level === "严重" ? "risk-severe" : level === "高" ? "risk-high" : level === "中" ? "risk-mid" : "risk-low";
    }

    // A score with no date is not actionable: a result from three months ago
    // looks identical to one from this morning, and someone will take a stale
    // conclusion into a meeting. created_at was already sent by the API and
    // simply discarded here.
    const RISK_STALE_DAYS = 30;

    function riskAgeDays(createdAt) {
      if (!createdAt) return null;
      // SQLite stores "YYYY-MM-DD HH:MM:SS" in UTC with no zone marker.
      const parsed = Date.parse(String(createdAt).replace(" ", "T") + "Z");
      if (Number.isNaN(parsed)) return null;
      return Math.floor((Date.now() - parsed) / 86400000);
    }

    function riskAgeLabel(days) {
      if (days === null) return "";
      if (days <= 0) return "今天";
      if (days === 1) return "昨天";
      if (days < 30) return `${days} 天前`;
      if (days < 365) return `${Math.floor(days / 30)} 个月前`;
      return `${Math.floor(days / 365)} 年前`;
    }

    // A project can carry several category paths. Rendering them all inline
    // wrapped the cell over two or three lines and inflated the row height,
    // pushing status, audit and risk toward the edge of the viewport. Show the
    // first and let the rest expand on demand.
    function renderCategoryCell(cell, row, fallbackText) {
      cell.classList.add("category-cell");
      cell.replaceChildren();
      const paths = Array.isArray(row.category_paths) ? row.category_paths : [];
      if (paths.length <= 1) {
        cell.textContent = paths[0] || fallbackText || "";
        return;
      }
      const first = document.createElement("span");
      first.textContent = paths[0];
      const more = document.createElement("button");
      more.type = "button";
      more.className = "category-more";
      more.textContent = `等 ${paths.length} 项`;
      more.title = paths.join("；");
      let expanded = false;
      more.addEventListener("click", () => {
        expanded = !expanded;
        first.textContent = expanded ? paths.join("；") : paths[0];
        more.textContent = expanded ? "收起" : `等 ${paths.length} 项`;
      });
      cell.append(first, more);
    }

    function riskBadge(risk) {
      if (!risk) return '<span class="muted small">未评估</span>';
      const days = riskAgeDays(risk.created_at);
      const stale = days !== null && days >= RISK_STALE_DAYS;
      const age = riskAgeLabel(days);
      const ageMarkup = age
        ? `<span class="risk-age${stale ? " risk-age-stale" : ""}" title="评估于 ${escapeHtml(risk.created_at || "")}">${escapeHtml(age)}${stale ? " · 已过期" : ""}</span>`
        : "";
      // The tier, not the score, is what says whether to act. 71% of projects
      // score in the 60s because one rule (项目仍处于未审核状态, severity 60)
      // sets the floor for all of them, so the number cannot carry triage on
      // its own. Shown first for that reason.
      const tierMarkup = risk.tier_label
        ? `<span class="tier-badge tier-${escapeHtml(risk.tier || "record")}">${escapeHtml(risk.tier_label)}</span>`
        : "";
      return `${tierMarkup}<span class="badge ${riskClass(risk.level)}">${escapeHtml(risk.level)} ${escapeHtml(risk.score)}</span>${ageMarkup}`;
    }

    async function boot() {
      initializeTableView();
      initializeDashboardLayout();
      initializeMainChatFloat();
      loadDashboard().catch(showDashboardError);
      await loadCategories();
      await loadMainChatProjects();
      await loadProjects();
    }

    async function loadDashboard() {
      const data = await api("/api/dashboard");
      document.getElementById("metricProjects").textContent = data.project_total ?? "-";
      document.getElementById("metricEvaluated").textContent = data.latest_project_count ?? 0;
      document.getElementById("metricSevere").textContent = data.latest_level_counts?.["严重"] ?? 0;
      document.getElementById("metricHigh").textContent = data.latest_level_counts?.["高"] ?? 0;
      document.getElementById("metricAverage").textContent = data.average_score ?? "-";
      document.getElementById("metricLlm").textContent = data.llm_configured ? "已配" : "未配";
      // Kept on state as well as painted, so the contract chat's summary bar
      // can report the same fact instead of carrying its own hardcoded answer.
      state.llmConfigured = Boolean(data.llm_configured);
      loadStaleRuleVersions().catch(() => {});
    }

    /* Stored scores from different rule versions are not comparable, and the
     * metric cards above average them together regardless. This says so, and
     * offers the one action that fixes it. Deliberately not automatic: a
     * re-score writes history and costs a source-database read per project. */
    async function loadStaleRuleVersions() {
      const banner = document.getElementById("staleRuleBanner");
      if (state.staleBannerDismissed) return;
      let data;
      try {
        data = await api("/api/risk/stale-evaluations");
      } catch (error) {
        // A failure here must not disturb the dashboard it sits under.
        banner.classList.add("hidden");
        return;
      }
      state.staleProjectIds = (data.projects || []).map(item => item.project_id);
      if (!data.stale_count) {
        banner.classList.add("hidden");
        return;
      }
      const versions = Object.entries(data.by_rule_version || {})
        .map(([version, count]) => `${version} ${count} 个`)
        .join("，");
      document.getElementById("staleRuleHeadline").textContent =
        `${data.stale_count} / ${data.evaluated_project_count} 个项目的评分仍来自旧规则版本（当前 ${data.current_rule_version}）`;
      document.getElementById("staleRuleDetail").textContent =
        `分布：${versions}。不同版本的分数由不同规则产生，上面的平均分与等级统计把它们混在了一起。`;
      banner.classList.remove("hidden");
    }

    async function reevaluateStaleProjects() {
      const projectIds = state.staleProjectIds || [];
      if (!projectIds.length || state.activeBatchId) return;
      await withButtonBusy("staleRuleReevaluate", "提交中", async () => {
        // Reuses the existing batch machinery rather than a second runner, so
        // progress, cancellation and per-project failure all behave identically
        // to a normal batch.
        const batch = await api("/api/risk/evaluate/batches", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({project_ids: projectIds})
        });
        state.activeBatchId = batch.batch_id;
        renderBatchProgress(batch);
        setEvaluationButtonsBusy(true);
        await pollEvaluationBatch(batch.batch_id);
        await loadStaleRuleVersions();
      }).catch(showError);
    }

    async function loadCategories() {
      const data = await api("/api/categories");
      state.categories = data.categories || [];
      filterCategoryOptions();
    }

    function categoryOptionLabel(item, parents) {
      const parent = parents.get(Number(item.parent_id));
      return parent && item.category_level !== 1 ? `${parent} / ${item.category_name}` : item.category_name;
    }

    function filterCategoryOptions() {
      const select = document.getElementById("categoryFilter");
      const current = select.value;
      const parents = new Map(state.categories.map(item => [Number(item.id), item.category_name]));
      const query = (document.getElementById("categorySearch").value || "").trim().toLowerCase();
      select.innerHTML = '<option value="">全部分类</option>';
      for (const item of state.categories) {
        const label = categoryOptionLabel(item, parents);
        if (query && !label.toLowerCase().includes(query) && !String(item.category_name || "").toLowerCase().includes(query)) {
          continue;
        }
        const option = document.createElement("option");
        option.value = item.category_name;
        option.textContent = label;
        select.appendChild(option);
      }
      select.value = Array.from(select.options).some(option => option.value === current) ? current : "";
    }

    function initializeTableView() {
      try {
        const raw = localStorage.getItem("riskTableZoom");
        const saved = raw === null ? NaN : Number(raw);
        state.tableZoom = Number.isFinite(saved) ? Math.max(0.5, Math.min(saved, 1.4)) : 0.85;
      } catch (_) {
        state.tableZoom = 0.85;
      }
      applyTableZoom();
    }

    function initializeDashboardLayout() {
      try {
        const savedPreset = localStorage.getItem("riskLayoutPreset") || "wide-table";
        state.layoutPreset = LAYOUT_PRESETS[savedPreset] ? savedPreset : "wide-table";
        const savedRatio = Number(localStorage.getItem("riskLayoutRatio"));
        state.layoutRatio = Number.isFinite(savedRatio) ? constrainLayoutRatio(savedRatio) : LAYOUT_PRESETS[state.layoutPreset].ratio;
        state.viewSettingsOpen = localStorage.getItem("riskViewSettingsOpen") === "true";
        state.compactTable = localStorage.getItem("riskCompactTable") === "true";
      } catch (_) {
        state.layoutPreset = "wide-table";
        state.layoutRatio = 0.8;
        state.viewSettingsOpen = false;
        state.compactTable = false;
      }
      applyDashboardLayout();
    }

    function constrainLayoutRatio(ratio) {
      return Math.max(0.55, Math.min(Number(ratio) || 0.8, 0.9));
    }

    function layoutColumnsForRatio(ratio) {
      const project = Math.round(constrainLayoutRatio(ratio) * 100);
      return `minmax(0, ${project}fr) 8px minmax(280px, ${100 - project}fr)`;
    }

    function applyDashboardLayout() {
      const grid = document.getElementById("dashboardGrid");
      if (!grid) return;
      for (const preset of Object.values(LAYOUT_PRESETS)) {
        grid.classList.remove(preset.className);
      }
      const preset = LAYOUT_PRESETS[state.layoutPreset] || LAYOUT_PRESETS["wide-table"];
      grid.classList.add(preset.className);
      grid.classList.toggle("compact-table", state.compactTable);
      const ratio = constrainLayoutRatio(state.layoutRatio || preset.ratio);
      state.layoutRatio = ratio;
      grid.style.setProperty("--risk-layout-columns", state.layoutPreset === "stacked" ? "1fr" : layoutColumnsForRatio(ratio));
      for (const [presetName, buttonId] of Object.entries({
        "wide-table": "layoutPresetWideTable",
        balanced: "layoutPresetBalanced",
        stacked: "layoutPresetStacked"
      })) {
        const button = document.getElementById(buttonId);
        if (button) button.classList.toggle("active", presetName === state.layoutPreset);
      }
      const viewSettingsButton = document.getElementById("viewSettingsToggle");
      if (viewSettingsButton) {
        viewSettingsButton.classList.toggle("active", state.viewSettingsOpen);
        viewSettingsButton.textContent = state.viewSettingsOpen ? "收起视图设置" : "视图设置";
      }
      const viewSettingsPanel = document.getElementById("viewSettingsPanel");
      if (viewSettingsPanel) viewSettingsPanel.classList.toggle("hidden", !state.viewSettingsOpen);
      const layoutWidthSlider = document.getElementById("layoutWidthSlider");
      if (layoutWidthSlider) {
        layoutWidthSlider.value = String(Math.round(state.layoutRatio * 100));
        layoutWidthSlider.disabled = state.layoutPreset === "stacked";
      }
      const layoutWidthValue = document.getElementById("layoutWidthValue");
      if (layoutWidthValue) layoutWidthValue.textContent = state.layoutPreset === "stacked" ? "上下" : `${Math.round(state.layoutRatio * 100)}%`;
      const compactToggle = document.getElementById("compactTableToggle");
      if (compactToggle) compactToggle.checked = state.compactTable;
      const fitTableButton = document.getElementById("fitTableButton");
      if (fitTableButton) fitTableButton.disabled = state.layoutPreset === "stacked";
      try {
        localStorage.setItem("riskLayoutPreset", state.layoutPreset);
        localStorage.setItem("riskLayoutRatio", String(state.layoutRatio));
        localStorage.setItem("riskViewSettingsOpen", String(state.viewSettingsOpen));
        localStorage.setItem("riskCompactTable", String(state.compactTable));
      } catch (_) {}
    }

    function applyLayoutPreset(presetName) {
      if (!LAYOUT_PRESETS[presetName]) return;
      state.layoutPreset = presetName;
      state.layoutRatio = LAYOUT_PRESETS[presetName].ratio;
      applyDashboardLayout();
    }

    function setLayoutWidthFromSlider(value) {
      if (state.layoutPreset === "stacked") return;
      state.layoutRatio = constrainLayoutRatio(Number(value) / 100);
      applyDashboardLayout();
    }

    function setTableZoomFromSlider(value) {
      state.tableZoom = Math.max(0.5, Math.min(1.4, Math.round(Number(value) || 85) / 100));
      applyTableZoom();
    }

    function toggleViewSettings() {
      state.viewSettingsOpen = !state.viewSettingsOpen;
      applyDashboardLayout();
    }

    function toggleCompactTable(enabled) {
      state.compactTable = Boolean(enabled);
      applyDashboardLayout();
    }

    function fitProjectTable() {
      const viewport = document.getElementById("projectTableViewport");
      const table = document.getElementById("projectTable");
      if (!viewport || !table) return;
      const tableWidth = Number(table.offsetWidth || table.scrollWidth || 1120);
      const viewportWidth = Number(viewport.clientWidth || 0);
      if (!tableWidth || !viewportWidth) return;
      state.tableZoom = Math.max(0.5, Math.min(1.4, Math.floor((viewportWidth / tableWidth) * 100) / 100));
      applyTableZoom();
    }

    function resetDashboardLayout() {
      state.layoutPreset = "wide-table";
      state.layoutRatio = LAYOUT_PRESETS["wide-table"].ratio;
      state.viewSettingsOpen = false;
      state.compactTable = false;
      state.tableZoom = 0.85;
      applyDashboardLayout();
      applyTableZoom();
    }

    function handleLayoutResizePointerDown(event) {
      if (state.layoutPreset === "stacked" || event.button !== 0) return;
      const grid = document.getElementById("dashboardGrid");
      const rect = grid.getBoundingClientRect();
      state.layoutResizeDrag = {
        pointerId: event.pointerId,
        left: rect.left,
        width: rect.width
      };
      grid.classList.add("resizing");
      event.currentTarget.setPointerCapture(event.pointerId);
      event.preventDefault();
    }

    function handleLayoutResizePointerMove(event) {
      if (!state.layoutResizeDrag || state.layoutResizeDrag.pointerId !== event.pointerId) return;
      const drag = state.layoutResizeDrag;
      state.layoutRatio = constrainLayoutRatio((event.clientX - drag.left) / Math.max(drag.width, 1));
      applyDashboardLayout();
    }

    function handleLayoutResizePointerUp(event) {
      if (!state.layoutResizeDrag || state.layoutResizeDrag.pointerId !== event.pointerId) return;
      document.getElementById("dashboardGrid").classList.remove("resizing");
      state.layoutResizeDrag = null;
      try { event.currentTarget.releasePointerCapture(event.pointerId); } catch (_) {}
    }

    function applyTableZoom() {
      const viewport = document.getElementById("projectTableViewport");
      if (!viewport) return;
      viewport.style.setProperty("--risk-table-zoom", String(state.tableZoom));
      const percent = `${Math.round(state.tableZoom * 100)}%`;
      document.getElementById("tableZoomValue").textContent = percent;
      const slider = document.getElementById("tableZoomSlider");
      if (slider) slider.value = String(Math.round(state.tableZoom * 100));
      const sliderValue = document.getElementById("tableZoomSliderValue");
      if (sliderValue) sliderValue.textContent = percent;
      try { localStorage.setItem("riskTableZoom", String(state.tableZoom)); } catch (_) {}
    }

    function changeTableZoom(delta) {
      state.tableZoom = Math.max(0.5, Math.min(1.4, Math.round((state.tableZoom + delta) * 20) / 20));
      applyTableZoom();
    }

    function resetTableView() {
      state.tableZoom = 0.85;
      const viewport = document.getElementById("projectTableViewport");
      if (viewport) {
        viewport.scrollLeft = 0;
        viewport.scrollTop = 0;
      }
      applyTableZoom();
    }

    function toggleTablePan(enabled) {
      state.tablePanEnabled = Boolean(enabled);
      document.getElementById("projectTableViewport").classList.toggle("pan-enabled", state.tablePanEnabled);
    }

    function handleTablePanPointerDown(event) {
      if (!state.tablePanEnabled || event.button !== 0) return;
      if (event.target.closest("button,input,select,textarea,label,a")) return;
      const viewport = document.getElementById("projectTableViewport");
      state.tablePanDrag = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        left: viewport.scrollLeft,
        top: viewport.scrollTop
      };
      viewport.classList.add("panning");
      viewport.setPointerCapture(event.pointerId);
      event.preventDefault();
    }

    function handleTablePanPointerMove(event) {
      if (!state.tablePanDrag || state.tablePanDrag.pointerId !== event.pointerId) return;
      const viewport = document.getElementById("projectTableViewport");
      viewport.scrollLeft = state.tablePanDrag.left - (event.clientX - state.tablePanDrag.x);
      viewport.scrollTop = state.tablePanDrag.top - (event.clientY - state.tablePanDrag.y);
    }

    function handleTablePanPointerUp(event) {
      if (!state.tablePanDrag || state.tablePanDrag.pointerId !== event.pointerId) return;
      const viewport = document.getElementById("projectTableViewport");
      state.tablePanDrag = null;
      viewport.classList.remove("panning");
      try { viewport.releasePointerCapture(event.pointerId); } catch (_) {}
    }

    function requestParams() {
      const params = filterParams();
      params.set("page", String(state.page));
      params.set("page_size", String(state.pageSize));
      return params;
    }

    function filterParams() {
      return new URLSearchParams({
        search: document.getElementById("search").value,
        project_status: document.getElementById("statusFilter").value,
        approval_status: document.getElementById("approvalFilter").value,
        category: document.getElementById("categoryFilter").value,
        risk_level: document.getElementById("riskFilter").value
      });
    }

    async function reloadProjects() {
      state.page = 1;
      state.pageSize = Number(document.getElementById("pageSize").value || 25);
      await withButtonBusy("queryButton", "查询中", () => loadProjects());
    }

    /* Rows in the table's own shape while the query runs. Reuses the ledger's
     * skeleton classes so the two tables load the same way, and so the
     * reduced-motion rule that suppresses the sweep covers both. */
    const PROJECT_SKELETON_ROWS = 8;

    function showProjectSkeleton(show) {
      const host = document.getElementById("projectLoadingSkeleton");
      if (!host) return;
      host.classList.toggle("hidden", !show);
      host.replaceChildren();
      if (!show) return;
      for (let index = 0; index < PROJECT_SKELETON_ROWS; index += 1) {
        const row = document.createElement("div");
        row.className = index === 0 ? "skeleton-row head" : "skeleton-row";
        row.style.animationDelay = `${index * 0.08}s`;
        host.appendChild(row);
      }
    }

    async function loadProjects() {
      document.getElementById("projectStatus").textContent = "加载中...";
      // A skeleton in the table's own shape, not a spinner. The previous
      // behaviour left the last result on screen with "加载中..." beside it,
      // so stale rows looked current for the length of the query.
      showProjectSkeleton(true);
      let data;
      try {
        data = await api(`/api/projects?${requestParams().toString()}`);
      } finally {
        showProjectSkeleton(false);
      }
      const tbody = document.getElementById("projectRows");
      tbody.innerHTML = "";
      state.currentRows = data.projects || [];
      for (const row of data.projects) {
        const id = String(pick(row, ["project_id", "project_no", "project_code", "id"]));
        const name = pick(row, ["name", "project_name", "title"]);
        const status = pick(row, ["project_status", "status"]);
        const audit = pick(row, ["approval_status", "audit_status"]);
        const categories = row.category_paths?.length ? row.category_paths.join("；") : pick(row, ["project_type"]);
        const tr = document.createElement("tr");
        const isEvaluating = state.evaluatingProjectId === id;
        const hasEvaluationInFlight = Boolean(state.activeJobId || state.activeBatchId || state.evaluatingProjectId);
        const displayName = String(name || id);
        tr.innerHTML = `<td><input type="checkbox" data-project-select /></td><td data-project-id></td><td class="name-cell"><button class="link" data-project-name></button></td><td data-project-category></td><td data-project-status></td><td data-project-audit></td><td data-project-risk>${riskBadge(row.latest_risk)}</td><td class="action-cell"><button data-project-evaluate data-evaluate-id></button></td>`;
        tr.querySelector("[data-project-id]").textContent = id;
        renderCategoryCell(tr.querySelector("[data-project-category]"), row, categories);
        tr.querySelector("[data-project-status]").textContent = status;
        tr.querySelector("[data-project-audit]").textContent = audit;
        const checkbox = tr.querySelector("[data-project-select]");
        checkbox.checked = state.selectedProjectIds.has(id);
        checkbox.addEventListener("change", () => toggleProjectSelection(id, displayName, checkbox.checked));
        const nameButton = tr.querySelector("[data-project-name]");
        nameButton.textContent = name;
        nameButton.addEventListener("click", () => openRiskModal(id, displayName));
        const evaluateButton = tr.querySelector("[data-project-evaluate]");
        evaluateButton.dataset.evaluateId = id;
        evaluateButton.disabled = hasEvaluationInFlight;
        evaluateButton.textContent = isEvaluating ? "评估中" : "评估";
        evaluateButton.addEventListener("click", () => evaluateProject(id));
        tbody.appendChild(tr);
      }
      const meta = data.pagination || {};
      state.page = meta.page || 1;
      state.totalPages = meta.total_pages || 0;
      document.getElementById("projectStatus").textContent = `共 ${meta.total ?? data.projects.length} 条，当前 ${data.projects.length} 条`;
      document.getElementById("pageText").textContent = state.totalPages ? `第 ${state.page} / ${state.totalPages} 页` : "暂无数据";
      document.getElementById("prevPage").disabled = !meta.has_previous;
      document.getElementById("nextPage").disabled = !meta.has_next;
      updateSelectionUi();
    }

    function toggleProjectSelection(id, name, checked) {
      const projectId = String(id);
      if (checked) {
        state.selectedProjectIds.add(projectId);
        state.selectedProjectNames.set(projectId, String(name || projectId));
      } else {
        state.selectedProjectIds.delete(projectId);
        state.selectedProjectNames.delete(projectId);
      }
      updateSelectionUi();
    }

    function toggleCurrentPageSelection(checked) {
      for (const row of state.currentRows) {
        const id = String(pick(row, ["project_id", "project_no", "project_code", "id"]));
        const name = String(pick(row, ["name", "project_name", "title"]) || id);
        if (id) toggleProjectSelection(id, name, checked);
      }
      for (const checkbox of document.querySelectorAll("input[data-project-select]")) {
        checkbox.checked = checked;
      }
      updateSelectionUi();
    }

    async function selectFilteredProjects() {
      await withButtonBusy("selectFilteredButton", "选择中", async () => {
        const params = filterParams();
        params.set("limit", "1000");
        const data = await api(`/api/projects/choices?${params.toString()}`);
        for (const item of data.projects || []) {
          const id = String(item.project_id || "");
          if (!id) continue;
          state.selectedProjectIds.add(id);
          state.selectedProjectNames.set(id, item.project_name || id);
        }
        updateSelectionUi();
        document.getElementById("projectStatus").textContent = data.truncated
          ? `已选择前 ${data.limit} 个匹配项目，筛选结果过多已截断。`
          : `已选择 ${data.projects.length} 个匹配项目。`;
      }).catch(showError);
    }

    function clearSelection() {
      state.selectedProjectIds.clear();
      state.selectedProjectNames.clear();
      for (const checkbox of document.querySelectorAll("input[data-project-select]")) {
        checkbox.checked = false;
      }
      updateSelectionUi();
    }

    function updateSelectionUi() {
      const count = state.selectedProjectIds.size;
      document.getElementById("selectionSummary").textContent = `已选择 ${count} 个项目`;
      document.getElementById("batchEvaluateButton").disabled = count === 0 || Boolean(state.activeBatchId);
      const pageIds = state.currentRows.map(row => String(pick(row, ["project_id", "project_no", "project_code", "id"]))).filter(Boolean);
      const pageChecked = pageIds.length > 0 && pageIds.every(id => state.selectedProjectIds.has(id));
      const pageCheckbox = document.getElementById("selectPageCheckbox");
      pageCheckbox.checked = pageChecked;
      pageCheckbox.indeterminate = pageIds.some(id => state.selectedProjectIds.has(id)) && !pageChecked;
    }

    async function evaluateSelectedProjects() {
      const projectIds = Array.from(state.selectedProjectIds);
      if (!projectIds.length || state.activeBatchId) return;
      await withButtonBusy("batchEvaluateButton", "提交中", async () => {
        const batch = await api("/api/risk/evaluate/batches", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({project_ids: projectIds})
        });
        state.activeBatchId = batch.batch_id;
        renderBatchProgress(batch);
        setEvaluationButtonsBusy(true);
        await pollEvaluationBatch(batch.batch_id);
      }).catch(showError);
    }

    async function pollEvaluationBatch(batchId) {
      while (true) {
        const batch = await api(`/api/risk/evaluate/batches/${encodeURIComponent(batchId)}`);
        renderBatchProgress(batch);
        if (isTerminalEvaluationStatus(batch.status)) {
          state.activeBatchId = "";
          setEvaluationButtonsBusy(false);
          updateSelectionUi();
          await Promise.all([loadProjects(), loadDashboard().catch(showDashboardError)]);
          return;
        }
        await delay(1200);
      }
    }

    function isTerminalEvaluationStatus(status) {
      return status === "succeeded" || status === "failed" || status === "cancelled";
    }

    function renderBatchProgress(batch) {
      document.getElementById("batchPanel").classList.remove("hidden");
      const completed = Number(batch.completed || 0);
      const failed = Number(batch.failed || 0);
      const cancelled = Number(batch.cancelled || 0);
      const total = Number(batch.total || 0);
      const done = completed + failed + cancelled;
      document.getElementById("batchMessage").textContent = batch.current_project_id
        ? `正在评估项目 ${batch.current_project_id}`
        : (batch.message || "批量评估进行中");
      document.getElementById("batchCount").textContent = `${done} / ${total}，失败 ${failed}，取消 ${cancelled}`;
      document.getElementById("batchProgressFill").style.width = `${Math.max(0, Math.min(Number(batch.progress || 0), 100))}%`;
      const finished = isTerminalEvaluationStatus(batch.status);
      document.getElementById("cancelBatchButton").disabled = finished;
      document.getElementById("cancelBatchButton").classList.toggle("hidden", finished);
      document.getElementById("dismissBatchButton").classList.toggle("hidden", !finished);

      const target = document.getElementById("batchItems");
      target.replaceChildren();
      if (finished) {
        // Once the run is over the chips are no longer a queue, they are a
        // wall of history. Cancelling a large batch used to leave one
        // "cancelled" chips on screen with no way to clear them. Collapse to a
        // one-line outcome and let the user dismiss the panel.
        const summary = document.createElement("span");
        summary.className = "small muted";
        summary.textContent = batch.status === "cancelled"
          ? `已取消，其中 ${completed} 项在取消前完成，${cancelled} 项未执行。`
          : `已结束：完成 ${completed} 项，失败 ${failed} 项，取消 ${cancelled} 项。`;
        target.appendChild(summary);
        return;
      }
      for (const item of (batch.items || [])) {
        // While running, only the unfinished and the failed are worth showing.
        // Listing every already-finished project buries the ones still moving.
        if (item.status === "succeeded") continue;
        const node = document.createElement("span");
        node.className = "batch-item";
        node.textContent = `${item.project_id} · ${item.status}`;
        target.appendChild(node);
      }
    }

    function dismissBatchPanel() {
      document.getElementById("batchPanel").classList.add("hidden");
      document.getElementById("batchItems").replaceChildren();
      document.getElementById("dismissBatchButton").classList.add("hidden");
      document.getElementById("cancelBatchButton").classList.remove("hidden");
    }

    async function cancelActiveBatch() {
      if (!state.activeBatchId) return;
      const batch = await api(`/api/risk/evaluate/batches/${encodeURIComponent(state.activeBatchId)}/cancel`, {method: "POST"});
      renderBatchProgress(batch);
    }

    async function changePage(delta) {
      const next = state.page + delta;
      if (next < 1 || (state.totalPages && next > state.totalPages)) return;
      state.page = next;
      await loadProjects();
    }

    async function resetFilters() {
      await withButtonBusy("resetButton", "重置中", async () => {
        document.getElementById("search").value = "";
        document.getElementById("statusFilter").value = "";
        document.getElementById("approvalFilter").value = "";
        document.getElementById("categorySearch").value = "";
        document.getElementById("categoryFilter").value = "";
        filterCategoryOptions();
        document.getElementById("riskFilter").value = "";
        document.getElementById("pageSize").value = "25";
        clearSelection();
        await reloadProjects();
      }).catch(showError);
    }

    async function evaluateProject(id) {
      if (state.activeJobId || state.activeBatchId) return;
      state.evaluatingProjectId = String(id);
      setEvaluationButtonsBusy(true);
      renderJobProgress({progress: 0, message: "已加入评估队列", elapsed_ms: 0});
      document.getElementById("riskSummary").textContent = "评估任务已提交，正在准备读取项目数据。";
      try {
        const job = await api(`/api/risk/evaluate/${encodeURIComponent(id)}/jobs`, {method: "POST"});
        state.activeJobId = job.job_id;
        renderJobProgress(job);
        await pollEvaluationJob(job.job_id, id);
      } catch (err) {
        document.getElementById("riskSummary").textContent = "评估失败：" + err.message;
        hideJobProgress();
      } finally {
        state.activeJobId = "";
        state.evaluatingProjectId = "";
        setEvaluationButtonsBusy(false);
      }
    }

    async function pollEvaluationJob(jobId, id) {
      while (true) {
        const job = await api(`/api/risk/evaluate/jobs/${encodeURIComponent(jobId)}`);
        renderJobProgress(job);
        if (job.status === "succeeded") {
          renderRiskResult(job.result);
          await Promise.all([loadHistory(id), loadProjects()]);
          loadDashboard().catch(showDashboardError);
          return;
        }
        if (job.status === "cancelled") {
          document.getElementById("riskSummary").textContent = "评估已取消";
          hideJobProgress();
          await loadProjects();
          return;
        }
        if (job.status === "failed") {
          throw new Error(job.error_message || "Evaluation failed");
        }
        await delay(1000);
      }
    }

    /* ------------------------------------------------------- risk explanation
     * The model answers in Markdown - headings, bold runs, and a per-dimension
     * table - and this panel used to escape the whole string into innerHTML.
     * Two defects stacked. The syntax arrived as literal `**`, `###` and
     * `|---|---|`, and because HTML collapses whitespace every newline became a
     * space, so the table landed as one unreadable run-on line. `.chat-bubble`
     * sets `white-space: pre-wrap` and `.detail-summary` does not, which is why
     * the chat log never showed the second half of this.
     *
     * Parsing is deliberately separate from rendering. `parseRiskMarkdown` is a
     * pure string->data function, so the Node harness can assert on its output
     * with no DOM at all, and `renderRiskMarkdown` only ever calls
     * createElement/textContent. Model output is data and must never become
     * markup: this replaces an innerHTML site rather than adding one.
     */
    const MD_HEADING = /^\s{0,3}(#{1,6})\s+(.*)$/;
    const MD_RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
    const MD_ORDERED = /^(\s*)\d{1,3}[.)]\s+(.*)$/;
    const MD_BULLET = /^(\s*)[-*+]\s+(.*)$/;
    const MD_TABLE_DIVIDER = /^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/;

    /* Every text run passes through here. Unmatched `**`, a lone `*` used for
     * emphasis, and stray bullets left mid-line all converge on this point, and
     * none of them should reach the reader - the brief is that no isolated
     * asterisk survives anywhere in this module. Backticks go too; this panel
     * has no code in it. */
    function cleanRun(value) {
      return String(value).replace(/[*`]/g, "").replace(/[ \t]+/g, " ");
    }

    function parseInlineSpans(text) {
      const spans = [];
      const pattern = /\*\*([^*]+)\*\*/g;
      let last = 0;
      let match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > last) spans.push({ text: cleanRun(text.slice(last, match.index)), strong: false });
        spans.push({ text: cleanRun(match[1]), strong: true });
        last = pattern.lastIndex;
      }
      if (last < text.length) spans.push({ text: cleanRun(text.slice(last)), strong: false });
      const kept = spans.filter(span => span.text !== "");
      if (kept.length) {
        // Trim only the outer edges: trimming every run would weld an English
        // word onto the bold one beside it.
        kept[0].text = kept[0].text.replace(/^ +/, "");
        kept[kept.length - 1].text = kept[kept.length - 1].text.replace(/ +$/, "");
      }
      return kept.filter(span => span.text !== "");
    }

    function splitTableRow(line) {
      const cells = line.split("|").map(cell => cleanRun(cell).trim());
      if (cells.length && cells[0] === "") cells.shift();
      if (cells.length && cells[cells.length - 1] === "") cells.pop();
      return cells;
    }

    function parseRiskMarkdown(text) {
      const lines = String(text ?? "").split(/\r?\n/);
      const blocks = [];
      let paragraph = [];
      let list = null;
      const flushParagraph = () => {
        if (!paragraph.length) return;
        blocks.push({ type: "paragraph", spans: parseInlineSpans(paragraph.join(" ")) });
        paragraph = [];
      };
      const flushList = () => {
        if (list) blocks.push(list);
        list = null;
      };
      const flushAll = () => { flushParagraph(); flushList(); };

      for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        if (!line.trim()) { flushAll(); continue; }

        const heading = MD_HEADING.exec(line);
        if (heading) {
          flushAll();
          blocks.push({ type: "heading", level: heading[1].length, spans: parseInlineSpans(heading[2]) });
          continue;
        }
        if (MD_RULE.test(line)) { flushAll(); blocks.push({ type: "rule" }); continue; }

        // A header row is only a table when the next line is the delimiter.
        // Without that check any prose containing a vertical bar becomes a
        // one-column table.
        if (line.includes("|") && MD_TABLE_DIVIDER.test(lines[index + 1] || "")) {
          flushAll();
          const head = splitTableRow(line);
          const rows = [];
          index += 1;
          while (index + 1 < lines.length && lines[index + 1].includes("|")) {
            index += 1;
            rows.push(splitTableRow(lines[index]));
          }
          blocks.push({ type: "table", head, rows });
          continue;
        }

        const ordered = MD_ORDERED.exec(line);
        const bullet = ordered ? null : MD_BULLET.exec(line);
        if (ordered || bullet) {
          flushParagraph();
          const isOrdered = Boolean(ordered);
          const indent = (ordered ? ordered[1] : bullet[1]).replace(/\t/g, "  ").length;
          if (!list || list.ordered !== isOrdered) {
            flushList();
            list = { type: "list", ordered: isOrdered, items: [] };
          }
          list.items.push({
            depth: Math.min(Math.floor(indent / 2), 2),
            spans: parseInlineSpans(ordered ? ordered[2] : bullet[2]),
          });
          continue;
        }

        flushList();
        paragraph.push(line.trim());
      }
      flushAll();
      return blocks;
    }

    function appendSpans(target, spans) {
      for (const span of spans) {
        if (span.strong) {
          const strong = document.createElement("strong");
          strong.textContent = span.text;
          target.appendChild(strong);
        } else {
          target.appendChild(document.createTextNode(span.text));
        }
      }
    }

    function buildRiskBlock(block) {
      if (block.type === "heading") {
        const node = document.createElement(block.level <= 2 ? "h4" : "h5");
        node.className = "risk-md-heading";
        appendSpans(node, block.spans);
        return node;
      }
      if (block.type === "rule") {
        const node = document.createElement("hr");
        node.className = "risk-md-rule";
        return node;
      }
      if (block.type === "list") {
        const node = document.createElement(block.ordered ? "ol" : "ul");
        node.className = "risk-md-list";
        for (const item of block.items) {
          const li = document.createElement("li");
          if (item.depth) li.className = `depth-${item.depth}`;
          appendSpans(li, item.spans);
          node.appendChild(li);
        }
        return node;
      }
      if (block.type === "table") {
        // Wrapped so a wide table scrolls inside the panel instead of forcing
        // the whole detail column sideways.
        const wrapper = document.createElement("div");
        wrapper.className = "risk-md-table-wrap";
        const table = document.createElement("table");
        table.className = "risk-md-table";
        if (block.head.length) {
          const thead = document.createElement("thead");
          const tr = document.createElement("tr");
          for (const cell of block.head) {
            const th = document.createElement("th");
            th.textContent = cell;
            tr.appendChild(th);
          }
          thead.appendChild(tr);
          table.appendChild(thead);
        }
        const tbody = document.createElement("tbody");
        for (const row of block.rows) {
          const tr = document.createElement("tr");
          for (const cell of row) {
            const td = document.createElement("td");
            td.textContent = cell;
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        wrapper.appendChild(table);
        return wrapper;
      }
      const node = document.createElement("p");
      node.className = "risk-md-paragraph";
      appendSpans(node, block.spans);
      return node;
    }

    function renderRiskMarkdown(host, text) {
      const blocks = parseRiskMarkdown(text);
      for (const block of blocks) host.appendChild(buildRiskBlock(block));
      return blocks;
    }

    function spansText(spans) {
      return (spans || []).map(span => span.text).join("");
    }

    /* The same content with the syntax already resolved, for the typewriter.
     * Typing the raw Markdown would show `**` appear and then vanish when the
     * structured render replaces it; typing this keeps the words identical, so
     * only the layout settles at the end. `.chat-bubble` is `pre-wrap`, so the
     * newlines here are real line breaks while typing. */
    function riskMarkdownPlainText(blocks) {
      const lines = [];
      for (const block of blocks) {
        if (block.type === "rule") continue;
        if (block.type === "table") {
          if (block.head.length) lines.push(block.head.join("  "));
          for (const row of block.rows) lines.push(row.join("  "));
          continue;
        }
        if (block.type === "list") {
          block.items.forEach((item, index) => {
            const marker = block.ordered ? `${index + 1}. ` : "· ";
            lines.push("  ".repeat(item.depth) + marker + spansText(item.spans));
          });
          continue;
        }
        lines.push(spansText(block.spans));
      }
      return lines.join("\n");
    }

    function renderRiskResult(result) {
      if (!result) return;
      hideJobProgress();
      const summary = document.getElementById("riskSummary");
      // `muted` is the placeholder's styling, for "请选择项目并点击评估。". It
      // used to survive into the real result, rendering the entire explanation
      // - now including a data table - in secondary grey.
      summary.classList.remove("muted");
      summary.replaceChildren();
      const header = document.createElement("div");
      header.className = "risk-md-header";
      const badge = document.createElement("span");
      badge.className = `badge ${riskClass(result.level)}`;
      badge.textContent = `${result.level}风险`;
      const score = document.createElement("strong");
      score.textContent = `综合分 ${result.score}`;
      header.append(badge, score);
      summary.appendChild(header);
      renderRiskMarkdown(summary, result.explanation);
      renderDimensions(result.dimensions || [], result.signals || []);
      renderList("hitList", result.hits || [], hit => `<strong>${escapeHtml(hit.dimension)}</strong> ${escapeHtml(hit.severity)}<br>${escapeHtml(hit.reason)}<br><span class="muted small">${escapeHtml(hit.evidence)}</span>`);
      renderList("suggestionList", result.suggestions || [], item => escapeHtml(item));
      document.getElementById("riskJson").textContent = JSON.stringify(result, null, 2);
    }

    function renderJobProgress(job) {
      const panel = document.getElementById("progressPanel");
      panel.classList.remove("hidden");
      const progress = Math.max(0, Math.min(Number(job.progress || 0), 100));
      document.getElementById("progressFill").style.width = `${progress}%`;
      document.getElementById("progressMessage").textContent = job.message || job.stage || "评估中";
      document.getElementById("progressElapsed").textContent = `${Math.round(Number(job.elapsed_ms || 0) / 1000)}s`;
    }

    function hideJobProgress() {
      document.getElementById("progressPanel").classList.add("hidden");
      document.getElementById("progressFill").style.width = "0%";
    }

    async function cancelActiveJob() {
      if (!state.activeJobId) return;
      const job = await api(`/api/risk/evaluate/jobs/${encodeURIComponent(state.activeJobId)}/cancel`, {method: "POST"});
      renderJobProgress(job);
      if (job.status === "cancelled") {
        document.getElementById("riskSummary").textContent = "评估已取消";
      }
    }

    function setEvaluationButtonsBusy(busy) {
      for (const button of document.querySelectorAll("button[data-evaluate-id]")) {
        const isTarget = button.dataset.evaluateId === state.evaluatingProjectId;
        button.disabled = busy;
        button.textContent = busy && isTarget ? "评估中" : "评估";
      }
    }

    function delay(ms) {
      return new Promise(resolve => setTimeout(resolve, ms));
    }

    function renderDimensions(dimensions, signals) {
      const target = document.getElementById("dimensionGrid");
      target.innerHTML = "";
      for (const item of dimensions) {
        const node = document.createElement("div");
        node.className = "dimension";
        node.innerHTML = `<div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.score)}</span></div><progress max="100" value="${Number(item.score) || 0}"></progress><div class="small muted">${escapeHtml(item.summary)}</div>`;
        target.appendChild(node);
      }
      // An absent contract dimension must never be read as "contracts are
      // fine". Say explicitly that it was not assessed.
      if ((signals || []).includes("contract_risk_not_assessed")) {
        const note = document.createElement("div");
        note.className = "dimension";
        const title = document.createElement("div");
        const strong = document.createElement("strong");
        strong.textContent = "合同风险";
        const span = document.createElement("span");
        span.textContent = "未评估";
        title.append(strong, span);
        const body = document.createElement("div");
        body.className = "small muted";
        body.textContent = "本项目未取得可评估的合同数据，合同风险未计入总分，不代表合同没有问题。";
        note.append(title, body);
        target.appendChild(note);
      }
    }

    function renderList(id, items, renderer) {
      const target = document.getElementById(id);
      target.innerHTML = "";
      if (!items.length) {
        target.innerHTML = '<li class="muted">无记录</li>';
        return;
      }
      for (const item of items) {
        const li = document.createElement("li");
        li.innerHTML = renderer(item);
        target.appendChild(li);
      }
    }

    async function loadHistory(id) {
      const data = await api(`/api/risk/history/${encodeURIComponent(id)}?limit=8`);
      renderList("historyList", data.history || [], item => `<strong>${escapeHtml(item.level)} ${escapeHtml(item.score)}</strong><br><span class="muted small">${escapeHtml(item.created_at)} · ${escapeHtml(item.rule_version)}</span><br>${escapeHtml(item.explanation)}`);
    }

    async function createNamedChatSession(projectId, defaultTitle) {
      const title = prompt("风险对话名称", defaultTitle || "风险对话");
      if (!String(title || "").trim()) return null;
      const data = await api("/api/chat/sessions", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({project_id: projectId, title: String(title).trim()})
      });
      return data.session || null;
    }

    function initializeMainChatFloat() {
      try {
        state.mainChatFloatOpen = localStorage.getItem("mainRiskChatFloatOpen") === "true";
        state.mainChatFloatMinimized = localStorage.getItem("mainRiskChatFloatMinimized") === "true";
        const saved = JSON.parse(localStorage.getItem("mainRiskChatFloatPosition") || "null");
        if (saved && Number.isFinite(Number(saved.left)) && Number.isFinite(Number(saved.top))) {
          state.mainChatFloatPosition = {left: Number(saved.left), top: Number(saved.top)};
        }
        const savedSize = JSON.parse(localStorage.getItem("mainRiskChatFloatSize") || "null");
        if (savedSize && Number.isFinite(Number(savedSize.width)) && Number.isFinite(Number(savedSize.height))) {
          state.mainChatFloatSize = {width: Number(savedSize.width), height: Number(savedSize.height)};
        }
      } catch (_) {
        state.mainChatFloatOpen = false;
        state.mainChatFloatMinimized = false;
        state.mainChatFloatPosition = null;
        state.mainChatFloatSize = null;
      }
      applyMainChatFloatState();
    }

    function applyMainChatFloatState() {
      const panel = document.getElementById("mainChatFloatPanel");
      panel.classList.toggle("hidden", !state.mainChatFloatOpen);
      panel.classList.toggle("minimized", state.mainChatFloatMinimized);
      document.getElementById("minimizeMainChatButton").textContent = state.mainChatFloatMinimized ? "□" : "_";
      const size = constrainMainChatFloatSize(state.mainChatFloatSize || {width: 560, height: 620});
      state.mainChatFloatSize = size;
      panel.style.width = `${size.width}px`;
      panel.style.height = state.mainChatFloatMinimized ? "auto" : `${size.height}px`;
      if (!state.mainChatFloatPosition) {
        state.mainChatFloatPosition = constrainMainChatFloatPosition({
          left: window.innerWidth - size.width - 22,
          top: window.innerHeight - Math.max(size.height, 120) - 22
        });
      }
      const position = constrainMainChatFloatPosition(state.mainChatFloatPosition);
      state.mainChatFloatPosition = position;
      panel.style.left = `${position.left}px`;
      panel.style.top = `${position.top}px`;
      panel.style.right = "auto";
      panel.style.bottom = "auto";
      try {
        localStorage.setItem("mainRiskChatFloatOpen", String(state.mainChatFloatOpen));
        localStorage.setItem("mainRiskChatFloatMinimized", String(state.mainChatFloatMinimized));
        localStorage.setItem("mainRiskChatFloatPosition", JSON.stringify(position));
        localStorage.setItem("mainRiskChatFloatSize", JSON.stringify(size));
      } catch (_) {}
    }

    function openMainChatFloat() {
      state.mainChatFloatOpen = true;
      state.mainChatFloatMinimized = false;
      applyMainChatFloatState();
      updateMainChatControls();
    }

    function closeMainChatFloat() {
      state.mainChatRequestToken += 1;
      cancelMainChatTypewriter();
      state.mainChatFloatOpen = false;
      applyMainChatFloatState();
    }

    function toggleMainChatMinimized() {
      state.mainChatFloatMinimized = !state.mainChatFloatMinimized;
      applyMainChatFloatState();
    }

    function constrainMainChatFloatPosition(position) {
      const panel = document.getElementById("mainChatFloatPanel");
      const size = constrainMainChatFloatSize(state.mainChatFloatSize || {width: panel.offsetWidth || 560, height: panel.offsetHeight || 620});
      const width = Math.max(size.width, 280);
      const height = state.mainChatFloatMinimized ? Math.max(panel.offsetHeight || 58, 58) : Math.max(size.height, 120);
      const margin = 12;
      return {
        left: Math.max(margin, Math.min(Number(position.left) || margin, window.innerWidth - width - margin)),
        top: Math.max(margin, Math.min(Number(position.top) || margin, window.innerHeight - height - margin))
      };
    }

    function constrainMainChatFloatSize(size) {
      const margin = 12;
      const minWidth = Math.min(420, Math.max(280, window.innerWidth - margin * 2));
      const minHeight = Math.min(360, Math.max(240, window.innerHeight - margin * 2));
      const maxWidth = Math.max(minWidth, window.innerWidth - margin * 2);
      const maxHeight = Math.max(minHeight, window.innerHeight - margin * 2);
      return {
        width: Math.max(minWidth, Math.min(Number(size.width) || 560, maxWidth)),
        height: Math.max(minHeight, Math.min(Number(size.height) || 620, maxHeight))
      };
    }

    function handleMainChatFloatPointerDown(event) {
      if (event.button !== 0) return;
      if (event.target.closest("button,input,select,textarea,label,a")) return;
      const panel = document.getElementById("mainChatFloatPanel");
      const rect = panel.getBoundingClientRect();
      state.mainChatFloatDrag = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        left: rect.left,
        top: rect.top
      };
      panel.classList.add("dragging");
      panel.setPointerCapture(event.pointerId);
      event.preventDefault();
    }

    function handleMainChatFloatPointerMove(event) {
      if (!state.mainChatFloatDrag || state.mainChatFloatDrag.pointerId !== event.pointerId) return;
      state.mainChatFloatPosition = constrainMainChatFloatPosition({
        left: state.mainChatFloatDrag.left + event.clientX - state.mainChatFloatDrag.x,
        top: state.mainChatFloatDrag.top + event.clientY - state.mainChatFloatDrag.y
      });
      applyMainChatFloatState();
    }

    function handleMainChatFloatPointerUp(event) {
      if (!state.mainChatFloatDrag || state.mainChatFloatDrag.pointerId !== event.pointerId) return;
      const panel = document.getElementById("mainChatFloatPanel");
      state.mainChatFloatDrag = null;
      panel.classList.remove("dragging");
      try { panel.releasePointerCapture(event.pointerId); } catch (_) {}
      applyMainChatFloatState();
    }

    function handleMainChatResizePointerDown(event) {
      if (event.button !== 0) return;
      if (state.mainChatFloatMinimized) return;
      const panel = document.getElementById("mainChatFloatPanel");
      const rect = panel.getBoundingClientRect();
      state.mainChatFloatResizeDrag = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        width: rect.width,
        height: rect.height,
        left: rect.left,
        top: rect.top
      };
      panel.classList.add("resizing");
      event.currentTarget.setPointerCapture(event.pointerId);
      event.preventDefault();
    }

    function handleMainChatResizePointerMove(event) {
      if (!state.mainChatFloatResizeDrag || state.mainChatFloatResizeDrag.pointerId !== event.pointerId) return;
      const drag = state.mainChatFloatResizeDrag;
      state.mainChatFloatSize = constrainMainChatFloatSize({
        width: drag.width + event.clientX - drag.x,
        height: drag.height + event.clientY - drag.y
      });
      state.mainChatFloatPosition = constrainMainChatFloatPosition({left: drag.left, top: drag.top});
      applyMainChatFloatState();
    }

    function handleMainChatResizePointerUp(event) {
      if (!state.mainChatFloatResizeDrag || state.mainChatFloatResizeDrag.pointerId !== event.pointerId) return;
      const panel = document.getElementById("mainChatFloatPanel");
      state.mainChatFloatResizeDrag = null;
      panel.classList.remove("resizing");
      try { event.currentTarget.releasePointerCapture(event.pointerId); } catch (_) {}
      applyMainChatFloatState();
    }

    function cancelMainChatTypewriter() {
      state.mainChatTypewriterToken += 1;
      if (state.mainChatTypewriterTimer) {
        clearTimeout(state.mainChatTypewriterTimer);
        state.mainChatTypewriterTimer = null;
      }
    }

    function cancelModalChatTypewriter() {
      state.chatTypewriterToken += 1;
      if (state.chatTypewriterTimer) {
        clearTimeout(state.chatTypewriterTimer);
        state.chatTypewriterTimer = null;
      }
    }

    function chatTypewriterDelay(char) {
      if ("。！？!?.".includes(char)) return 90;
      if ("，、；：,;:".includes(char)) return 45;
      return 18;
    }

    /* Assistant answers are Markdown too - the same `**` that used to print
     * verbatim in the risk panel prints verbatim here. The bubble keeps
     * `white-space: pre-wrap`, so this one never showed the collapsed-newline
     * half of that defect, only the raw syntax.
     *
     * Typing runs on the resolved plain text and the structured render replaces
     * it on the last character. Doing it here rather than at the three call
     * sites means the modal chat, the floating main chat and the contract chat
     * cannot drift apart. */
    function typeChatText(target, text, token, getCurrentToken, setTimer, onStep) {
      const source = String(text || "");
      const blocks = parseRiskMarkdown(source);
      const chars = Array.from(riskMarkdownPlainText(blocks));
      let index = 0;
      target.textContent = "";
      const step = () => {
        if (token !== getCurrentToken()) return;
        if (index >= chars.length) {
          setTimer(null);
          target.replaceChildren();
          for (const block of blocks) target.appendChild(buildRiskBlock(block));
          if (typeof onStep === "function") onStep();
          return;
        }
        const char = chars[index];
        target.textContent += char;
        index += 1;
        if (typeof onStep === "function") onStep();
        setTimer(setTimeout(step, chatTypewriterDelay(char)));
      };
      step();
    }

    function appendChatMessageToTarget(targetId, message) {
      const target = document.getElementById(targetId);
      if (target.children.length === 1 && target.firstElementChild?.classList.contains("muted")) {
        target.replaceChildren();
      }
      const wrapper = document.createElement("article");
      wrapper.className = `chat-message ${message.role === "user" ? "user" : "assistant"}`;
      const meta = document.createElement("div");
      meta.className = "chat-meta";
      meta.textContent = message.role === "user" ? "你" : "LLM 风险助手";
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      if (message.role === "user") {
        // The user's own words, verbatim. Parsing them would silently eat an
        // asterisk or a leading dash that someone typed on purpose.
        bubble.textContent = message.content || "";
      } else {
        renderRiskMarkdown(bubble, message.content || "");
      }
      wrapper.append(meta, bubble);
      const citations = Array.isArray(message.citations) ? message.citations : [];
      if (citations.length) {
        const citationList = document.createElement("div");
        citationList.className = "chat-citations";
        for (const citation of citations) {
          const item = document.createElement("button");
          item.type = "button";
          item.className = "chat-citation";
          item.textContent = citation.label || citation.source_id || "引用";
          item.title = citation.source_id || "";
          citationList.appendChild(item);
        }
        wrapper.appendChild(citationList);
      }
      target.appendChild(wrapper);
      target.scrollTop = target.scrollHeight;
      return {wrapper, bubble};
    }

    function appendMainChatMessage(message) {
      return appendChatMessageToTarget("mainChatMessages", message);
    }

    function typeMainChatAssistantMessage(message, requestToken) {
      if (requestToken !== state.mainChatRequestToken) return;
      cancelMainChatTypewriter();
      const rendered = appendMainChatMessage({...message, role: "assistant", content: ""});
      const token = ++state.mainChatTypewriterToken;
      typeChatText(
        rendered.bubble,
        message?.content || "",
        token,
        () => state.mainChatTypewriterToken,
        timer => { state.mainChatTypewriterTimer = timer; },
        () => {
          const target = document.getElementById("mainChatMessages");
          target.scrollTop = target.scrollHeight;
        }
      );
    }

    function typeModalChatAssistantMessage(message, requestToken, projectId) {
      if (!isCurrentChatRequest(requestToken, projectId)) return;
      cancelModalChatTypewriter();
      const rendered = appendChatMessage({...message, role: "assistant", content: ""});
      const token = ++state.chatTypewriterToken;
      typeChatText(
        rendered.bubble,
        message?.content || "",
        token,
        () => state.chatTypewriterToken,
        timer => { state.chatTypewriterTimer = timer; },
        () => {
          const target = document.getElementById("chatMessages");
          target.scrollTop = target.scrollHeight;
        }
      );
    }

    function renderMainChatError(error) {
      const target = document.getElementById("mainChatError");
      const message = error instanceof Error ? error.message : String(error || "");
      target.textContent = message ? `对话失败：${message}` : "";
      target.classList.toggle("hidden", !message);
    }

    function updateMainChatControls() {
      const hasProject = Boolean(state.mainChatProjectId);
      document.getElementById("mainChatProjectSelect").disabled = state.mainChatSending;
      document.getElementById("mainChatSessionSelect").disabled = !hasProject || !state.mainChatSessions.length || state.mainChatSending;
      document.getElementById("mainNewChatButton").disabled = !hasProject || state.mainChatSending;
      document.getElementById("mainChatInput").disabled = !hasProject || state.mainChatSending;
      document.getElementById("mainChatRemember").disabled = !hasProject || state.mainChatSending;
      document.getElementById("mainChatSendButton").disabled = !hasProject || state.mainChatSending;
      document.getElementById("mainChatSendButton").textContent = state.mainChatSending ? "发送中" : "发送";
    }

    async function loadMainChatProjects() {
      const requestToken = ++state.mainChatRequestToken;
      const params = new URLSearchParams({search: document.getElementById("mainChatProjectSearch").value || "", limit: "50"});
      const data = await api(`/api/projects/choices?${params.toString()}`);
      if (requestToken !== state.mainChatRequestToken) return;
      const select = document.getElementById("mainChatProjectSelect");
      const current = state.mainChatProjectId;
      select.replaceChildren();
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "选择项目";
      select.appendChild(empty);
      for (const project of data.projects || []) {
        const option = document.createElement("option");
        option.value = project.project_id || "";
        option.textContent = project.project_name || project.project_id || "";
        option.selected = option.value === current;
        select.appendChild(option);
      }
      if (current && Array.from(select.options).some(option => option.value === current)) {
        select.value = current;
      }
      updateMainChatControls();
    }

    async function selectMainChatProject(projectId) {
      cancelMainChatTypewriter();
      state.mainChatProjectId = String(projectId || "");
      const select = document.getElementById("mainChatProjectSelect");
      state.mainChatProjectName = select.selectedOptions[0]?.textContent || state.mainChatProjectId;
      state.mainActiveChatSessionId = "";
      state.mainChatSessions = [];
      renderMainChatMessages([]);
      renderMainChatMemoryStatus([]);
      renderMainChatError("");
      updateMainChatControls();
      if (!state.mainChatProjectId) return;
      const requestToken = ++state.mainChatRequestToken;
      const data = await api(`/api/chat/sessions?project_id=${encodeURIComponent(state.mainChatProjectId)}&limit=20`);
      if (requestToken !== state.mainChatRequestToken) return;
      state.mainChatSessions = data.sessions || [];
      renderMainChatSessionOptions();
      if (state.mainChatSessions[0]) {
        await selectMainChatSession(state.mainChatSessions[0].session_id);
      } else {
        renderMainChatMemoryStatus([]);
      }
    }

    function renderMainChatSessionOptions() {
      const select = document.getElementById("mainChatSessionSelect");
      select.replaceChildren();
      if (!state.mainChatSessions.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无对话";
        select.appendChild(option);
        return;
      }
      for (const session of state.mainChatSessions) {
        const option = document.createElement("option");
        option.value = session.session_id;
        option.textContent = session.title || session.session_id;
        option.selected = session.session_id === state.mainActiveChatSessionId;
        select.appendChild(option);
      }
    }

    async function selectMainChatSession(sessionId) {
      if (!state.mainChatProjectId || !sessionId) return;
      cancelMainChatTypewriter();
      state.mainActiveChatSessionId = String(sessionId);
      renderMainChatSessionOptions();
      const requestToken = ++state.mainChatRequestToken;
      const messages = await api(`/api/chat/sessions/${encodeURIComponent(state.mainActiveChatSessionId)}/messages?limit=80`);
      if (requestToken !== state.mainChatRequestToken || sessionId !== state.mainActiveChatSessionId) return;
      renderMainChatMessages(messages.messages || []);
      const memories = await api(`/api/chat/sessions/${encodeURIComponent(state.mainActiveChatSessionId)}/memories?limit=20`);
      if (requestToken !== state.mainChatRequestToken || sessionId !== state.mainActiveChatSessionId) return;
      renderMainChatMemoryStatus(memories.memories || []);
      updateMainChatControls();
    }

    async function createMainChatSession() {
      if (!state.mainChatProjectId) return "";
      const session = await createNamedChatSession(
        state.mainChatProjectId,
        `${state.mainChatProjectName || state.mainChatProjectId} 风险对话`
      );
      if (!session) return "";
      state.mainChatSessions = [session, ...state.mainChatSessions.filter(item => item.session_id !== session.session_id)];
      await selectMainChatSession(session.session_id);
      return session.session_id;
    }

    async function sendMainChatMessage(event) {
      if (event) event.preventDefault();
      if (state.mainChatSending) return;
      const content = document.getElementById("mainChatInput").value.trim();
      if (!state.mainChatProjectId || !content) return;
      state.mainChatSending = true;
      renderMainChatError("");
      updateMainChatControls();
      try {
        if (!state.mainActiveChatSessionId) {
          await createMainChatSession();
        }
        if (!state.mainActiveChatSessionId) return;
        const requestToken = ++state.mainChatRequestToken;
        const idempotencyKey = typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const remember = document.getElementById("mainChatRemember").checked;
        document.getElementById("mainChatInput").value = "";
        document.getElementById("mainChatRemember").checked = false;
        cancelMainChatTypewriter();
        appendMainChatMessage({role: "user", content});
        const data = await api(`/api/chat/sessions/${encodeURIComponent(state.mainActiveChatSessionId)}/messages`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            content,
            idempotency_key: idempotencyKey,
            remember
          })
        });
        if (requestToken !== state.mainChatRequestToken) return;
        typeMainChatAssistantMessage(data.assistant_message || {role: "assistant", content: ""}, requestToken);
        const memories = await api(`/api/chat/sessions/${encodeURIComponent(state.mainActiveChatSessionId)}/memories?limit=20`);
        if (requestToken !== state.mainChatRequestToken) return;
        renderMainChatMemoryStatus(memories.memories || []);
      } catch (err) {
        renderMainChatError(err);
      } finally {
        state.mainChatSending = false;
        updateMainChatControls();
      }
    }

    function renderMainChatMessages(messages) {
      cancelMainChatTypewriter();
      const target = document.getElementById("mainChatMessages");
      target.replaceChildren();
      if (!messages.length) {
        const empty = document.createElement("div");
        empty.className = "muted small";
        empty.textContent = state.mainChatProjectId ? "暂无消息" : "请选择项目后开始对话";
        target.appendChild(empty);
        return;
      }
      for (const message of messages) {
        appendMainChatMessage(message);
      }
      target.scrollTop = target.scrollHeight;
    }

    function renderMainChatMemoryStatus(memories) {
      const count = Array.isArray(memories) ? memories.length : 0;
      document.getElementById("mainChatMemoryStatus").textContent = count
        ? `本项目已有 ${count} 条可用记忆。`
        : (state.mainChatProjectId ? "本项目暂无长期记忆。" : "");
    }

    function renderRiskModalLoading() {
      document.getElementById("modalRiskLevel").textContent = "-";
      document.getElementById("modalRiskScore").textContent = "-";
      document.getElementById("modalHitCount").textContent = "-";
      document.getElementById("modalEvaluatedAt").textContent = "-";
      document.getElementById("modalExplanation").textContent = "";
      document.getElementById("modalDimensionChart").replaceChildren();
      document.getElementById("modalHitChart").replaceChildren();
      document.getElementById("modalHistoryChart").replaceChildren();
      state.historyTrendChart = null;
      state.historyTrendWidth = 0;
      if (historyTrendFrame) {
        cancelAnimationFrame(historyTrendFrame);
        historyTrendFrame = 0;
      }
      document.getElementById("modalSuggestionList").replaceChildren();
      document.getElementById("modalHistoryList").replaceChildren();
      document.getElementById("riskModalContent").classList.add("hidden");
      const empty = document.getElementById("riskModalEmpty");
      empty.textContent = "加载中...";
      empty.classList.remove("hidden");
    }

    function renderRiskModalError(message) {
      renderRiskModalLoading();
      const empty = document.getElementById("riskModalEmpty");
      empty.textContent = "加载失败：" + String(message || "未知错误");
    }

    function chatStorageKey(projectId) {
      return `riskChatSession:${projectId}`;
    }

    function isCurrentChatRequest(requestToken, projectId) {
      return requestToken === state.chatRequestToken && projectId === state.activeModalProjectId;
    }

    function renderChatError(error) {
      const target = document.getElementById("chatError");
      const message = error instanceof Error ? error.message : String(error || "");
      target.textContent = message ? `对话加载失败：${message}` : "";
      target.classList.toggle("hidden", !message);
    }

    function updateChatControls() {
      const hasProject = Boolean(state.activeModalProjectId);
      document.getElementById("chatSessionSelect").disabled = !hasProject || !state.chatSessions.length || state.chatSending;
      document.getElementById("newChatButton").disabled = !hasProject || state.chatSending;
      document.getElementById("chatInput").disabled = !hasProject || state.chatSending;
      document.getElementById("chatRemember").disabled = !hasProject || state.chatSending;
      document.getElementById("chatSendButton").disabled = !hasProject || state.chatSending;
      document.getElementById("chatSendButton").textContent = state.chatSending ? "发送中" : "发送";
    }

    function resetChatUi(projectId = "") {
      state.chatRequestToken += 1;
      cancelModalChatTypewriter();
      state.activeChatSessionId = "";
      state.chatSessions = [];
      state.chatSending = false;
      document.getElementById("chatSessionSelect").replaceChildren();
      document.getElementById("chatMessages").replaceChildren();
      document.getElementById("chatMemoryStatus").textContent = projectId ? "正在加载对话记忆..." : "";
      document.getElementById("chatInput").value = "";
      document.getElementById("chatRemember").checked = false;
      renderChatError("");
      updateChatControls();
    }

    async function loadChatSessions(projectId) {
      const requestToken = ++state.chatRequestToken;
      renderChatError("");
      document.getElementById("chatMemoryStatus").textContent = "正在加载对话...";
      updateChatControls();
      const data = await api(`/api/chat/sessions?project_id=${encodeURIComponent(projectId)}&limit=20`);
      if (!isCurrentChatRequest(requestToken, projectId)) return;
      state.chatSessions = data.sessions || [];
      renderChatSessionOptions();
      const saved = (() => {
        try { return localStorage.getItem(chatStorageKey(projectId)) || ""; } catch (_) { return ""; }
      })();
      const selected = state.chatSessions.some(session => session.session_id === saved)
        ? saved
        : (state.chatSessions[0]?.session_id || "");
      if (selected) {
        await selectChatSession(selected);
      } else {
        document.getElementById("chatMessages").replaceChildren();
        document.getElementById("chatMemoryStatus").textContent = "暂无对话，可新建后向 LLM 询问风险建议。";
        updateChatControls();
      }
    }

    function renderChatSessionOptions() {
      const select = document.getElementById("chatSessionSelect");
      select.replaceChildren();
      if (!state.chatSessions.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无对话";
        select.appendChild(option);
        return;
      }
      for (const session of state.chatSessions) {
        const option = document.createElement("option");
        option.value = session.session_id;
        option.textContent = session.title || session.session_id;
        option.selected = session.session_id === state.activeChatSessionId;
        select.appendChild(option);
      }
    }

    async function selectChatSession(sessionId) {
      const projectId = state.activeModalProjectId;
      if (!projectId || !sessionId) return;
      state.activeChatSessionId = String(sessionId);
      try { localStorage.setItem(`riskChatSession:${projectId}`, state.activeChatSessionId); } catch (_) {}
      renderChatSessionOptions();
      const requestToken = ++state.chatRequestToken;
      updateChatControls();
      await Promise.all([
        loadChatMessages(state.activeChatSessionId, requestToken, projectId),
        loadChatMemories(state.activeChatSessionId, requestToken, projectId),
      ]);
    }

    async function createChatSession() {
      const projectId = state.activeModalProjectId;
      if (!projectId) return "";
      const requestToken = ++state.chatRequestToken;
      renderChatError("");
      updateChatControls();
      const defaultTitle = `${document.getElementById("riskModalTitle").textContent || projectId} 风险对话`;
      const session = await createNamedChatSession(projectId, defaultTitle);
      if (!session) return "";
      if (!isCurrentChatRequest(requestToken, projectId)) return "";
      state.chatSessions = [session, ...state.chatSessions.filter(item => item.session_id !== session.session_id)];
      await selectChatSession(session.session_id);
      return session.session_id;
    }

    async function loadChatMessages(sessionId, requestToken = ++state.chatRequestToken, projectId = state.activeModalProjectId) {
      if (!sessionId) {
        renderChatMessages([]);
        return;
      }
      const data = await api(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages?limit=80`);
      if (!isCurrentChatRequest(requestToken, projectId) || sessionId !== state.activeChatSessionId) return;
      renderChatMessages(data.messages || []);
    }

    async function loadChatMemories(sessionId, requestToken = ++state.chatRequestToken, projectId = state.activeModalProjectId) {
      if (!sessionId) {
        renderChatMemoryStatus([]);
        return;
      }
      const data = await api(`/api/chat/sessions/${encodeURIComponent(sessionId)}/memories?limit=20`);
      if (!isCurrentChatRequest(requestToken, projectId) || sessionId !== state.activeChatSessionId) return;
      renderChatMemoryStatus(data.memories || []);
    }

    async function sendChatMessage(event) {
      event.preventDefault();
      if (state.chatSending) return;
      const projectId = state.activeModalProjectId;
      const input = document.getElementById("chatInput");
      const content = input.value.trim();
      if (!projectId || !content) return;
      state.chatSending = true;
      renderChatError("");
      updateChatControls();
      try {
        if (!state.activeChatSessionId) {
          await createChatSession();
        }
        if (!state.activeChatSessionId) return;
        const requestToken = ++state.chatRequestToken;
        const remember = document.getElementById("chatRemember").checked;
        const idempotencyKey = typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        input.value = "";
        document.getElementById("chatRemember").checked = false;
        cancelModalChatTypewriter();
        appendChatMessage({role: "user", content});
        const data = await api(`/api/chat/sessions/${encodeURIComponent(state.activeChatSessionId)}/messages`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({content, idempotency_key: idempotencyKey, remember})
        });
        if (!isCurrentChatRequest(requestToken, projectId)) return;
        typeModalChatAssistantMessage(data.assistant_message || {role: "assistant", content: ""}, requestToken, projectId);
        await loadChatMemories(state.activeChatSessionId, requestToken, projectId);
      } catch (err) {
        renderChatError(err);
      } finally {
        state.chatSending = false;
        updateChatControls();
      }
    }

    function renderChatMessages(messages) {
      cancelModalChatTypewriter();
      const target = document.getElementById("chatMessages");
      target.replaceChildren();
      if (!messages.length) {
        const empty = document.createElement("div");
        empty.className = "muted small";
        empty.textContent = "暂无消息。";
        target.appendChild(empty);
        return;
      }
      for (const message of messages) {
        appendChatMessage(message);
      }
      target.scrollTop = target.scrollHeight;
    }

    function appendChatMessage(message) {
      return appendChatMessageToTarget("chatMessages", message);
    }

    function renderChatMemoryStatus(memories) {
      const count = Array.isArray(memories) ? memories.length : 0;
      document.getElementById("chatMemoryStatus").textContent = count
        ? `本项目已有 ${count} 条可用记忆。`
        : "本项目暂无长期记忆。";
    }

    async function refreshContractAssets() {
      const projectId = state.activeModalProjectId;
      if (!projectId) return;
      const token = ++state.contractDiscoveryRequestToken;
      setContractRequestButtonsDisabled(true);
      document.getElementById("contractDiscoveryStatus").textContent = "正在刷新合同资产...";
      try {
        const data = await api("/api/contracts/assets/refresh", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({project_id: projectId})
        });
        if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
        renderContractAssetState(data);
        renderContractAnalysisState(data.latest_job || null, data.latest_summary || null);
        await loadAttachmentAuthorizations(projectId, token);
      } catch (_) {
        if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
        document.getElementById("contractDiscoveryStatus").textContent = "合同资产刷新失败";
      } finally {
        if (token === state.contractDiscoveryRequestToken && projectId === state.activeModalProjectId) {
          setContractRequestButtonsDisabled(false);
        }
      }
    }

    function discoverContracts() {
      return refreshContractAssets();
    }

    async function createAttachmentAuthorizationDrafts() {
      const projectId = state.activeModalProjectId;
      if (!projectId) return;
      const token = ++state.contractDiscoveryRequestToken;
      setContractRequestButtonsDisabled(true);
      document.getElementById("contractAuthorizationStatus").textContent = "正在生成本地授权草稿...";
      try {
        const data = await api("/api/contracts/attachments/authorizations/drafts/from-assets", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({project_id: projectId})
        });
        if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
        renderAttachmentAuthorizationState(data);
      } catch (_) {
        if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
        document.getElementById("contractAuthorizationStatus").textContent = "授权草稿生成失败";
      } finally {
        if (token === state.contractDiscoveryRequestToken && projectId === state.activeModalProjectId) {
          setContractRequestButtonsDisabled(false);
        }
      }
    }

    async function loadAttachmentAuthorizations(projectId = state.activeModalProjectId, token = state.contractDiscoveryRequestToken) {
      if (!projectId) return;
      const data = await api(`/api/contracts/attachments/authorizations?project_id=${encodeURIComponent(projectId)}`);
      if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
      renderAttachmentAuthorizationState(data);
    }

    async function createContractAnalysisJob() {
      const projectId = state.activeModalProjectId;
      if (!projectId) return;
      const token = ++state.contractDiscoveryRequestToken;
      setContractRequestButtonsDisabled(true);
      document.getElementById("contractAnalysisStatus").textContent = "正在创建合同分析任务...";
      // Drop the previous run's findings up front so a pending or failed
      // request can never leave stale results on screen reading as current.
      document.getElementById("contractRiskSummary").textContent = "";
      clearContractFindingsUi();
      try {
        const data = await api("/api/contracts/analysis/jobs", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({project_id: projectId})
        });
        if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
        renderContractAnalysisState(data.job || null, data.summary || null);
      } catch (_) {
        if (token !== state.contractDiscoveryRequestToken || projectId !== state.activeModalProjectId) return;
        document.getElementById("contractAnalysisStatus").textContent = "合同分析任务创建失败";
        clearContractFindingsUi();
      } finally {
        if (token === state.contractDiscoveryRequestToken && projectId === state.activeModalProjectId) {
          setContractRequestButtonsDisabled(false);
        }
      }
    }

    function setContractRequestButtonsDisabled(disabled) {
      document.getElementById("refreshContractAssetsButton").disabled = disabled;
      document.getElementById("createAttachmentAuthorizationDraftsButton").disabled = disabled;
      document.getElementById("createContractAnalysisJobButton").disabled = disabled;
      document.getElementById("discoverContractsButton").disabled = disabled;
    }

    function renderContractAssetState(data) {
      const assets = Array.isArray(data?.assets) ? data.assets : [];
      const counts = data?.asset_counts || {};
      const warnings = Array.isArray(data?.warnings) ? data.warnings : [];
      const contracts = assets.filter(item => item.asset_kind === "contract_metadata");
      const attachments = assets.filter(item => item.asset_kind === "attachment_candidate");
      const statusParts = [`合同资产 ${Number(counts.total || assets.length)}`, `合同 ${Number(counts.contract_metadata || contracts.length)}`, `附件 ${Number(counts.attachment_candidate || attachments.length)}`];
      if (warnings.length) statusParts.push(`提示 ${warnings.length} 条`);
      document.getElementById("contractDiscoveryStatus").textContent = statusParts.join("，");
      renderContractStatusPills(document.getElementById("contractAssetStatus"), [
        ["资产总数", counts.total ?? assets.length],
        ["合同元数据", counts.contract_metadata ?? contracts.length],
        ["附件候选", counts.attachment_candidate ?? attachments.length],
        ["状态", Object.entries(counts.by_status || {}).map(([name, value]) => `${name}:${value}`).join(" / ") || "-"]
      ]);
      renderContractItems(document.getElementById("contractList"), contracts, item => [
        item.display_name || item.source_ref || "-",
        item.source_ref || "",
        item.status || "",
        item.risk_signal || "",
        item.source_table || ""
      ]);
      renderContractItems(document.getElementById("contractAttachmentList"), attachments, item => [
        item.display_name || item.source_ref || "-",
        item.file_ext || "",
        item.file_size ? `${item.file_size} bytes` : "",
        item.status || "",
        item.source_table || ""
      ]);
    }

    function renderAttachmentAuthorizationState(data) {
      const authorizations = Array.isArray(data?.authorizations) ? data.authorizations : [];
      const counts = authorizations.reduce((acc, item) => {
        const status = String(item.status || "unknown");
        acc[status] = (acc[status] || 0) + 1;
        return acc;
      }, {});
      renderContractStatusPills(document.getElementById("contractAuthorizationStatus"), [
        ["授权草稿/记录", data?.total_count ?? authorizations.length],
        ["待审批", counts.draft || 0],
        ["已批准", counts.approved || 0],
        ["已拒绝", counts.rejected || 0],
        ["已过期", counts.expired || 0]
      ]);
    }

    const CONTRACT_JOB_STAGE_LABELS = {
      asset_check: "资产检查",
      metadata_rules: "元数据规则评估",
      waiting_for_text_extraction: "等待正文提取"
    };
    const CONTRACT_JOB_MESSAGE_LABELS = {
      no_contract_assets: "没有合同资产",
      no_project_linked_contracts: "没有关联到本项目的合同",
      metadata_analysis_complete: "元数据分析完成",
      text_extraction_not_enabled: "正文提取未启用"
    };

    // Rendered as the human-readable scope note, so they are not repeated in
    // the raw signal list.
    const CONTRACT_BOUNDARY_SIGNALS = new Set(["project_linked_contracts_only", "contract_text_not_analyzed", "budget_check_unavailable"]);

    function clearContractFindingsUi() {
      const scopeNote = document.getElementById("contractScopeNote");
      scopeNote.textContent = "";
      scopeNote.classList.add("hidden");
      document.getElementById("contractFindings").replaceChildren();
    }

    function contractSeverityClass(severity) {
      const value = Number(severity) || 0;
      if (value >= 76) return "sev-critical";
      if (value >= 51) return "sev-high";
      if (value >= 26) return "sev-medium";
      return "sev-low";
    }

    function renderContractAnalysisState(job, summary) {
      const findingCount = Array.isArray(summary?.findings) ? summary.findings.length : 0;
      renderContractStatusPills(document.getElementById("contractAnalysisStatus"), [
        ["任务状态", job?.status || "-"],
        ["阶段", CONTRACT_JOB_STAGE_LABELS[job?.stage] || job?.stage || "-"],
        ["已评估合同", summary?.asset_counts?.contracts_evaluated ?? "-"],
        ["命中规则", findingCount],
        ["消息", CONTRACT_JOB_MESSAGE_LABELS[job?.message] || job?.message || "-"]
      ]);
      const riskSummary = document.getElementById("contractRiskSummary");
      if (!summary) {
        riskSummary.textContent = "";
        clearContractFindingsUi();
        return;
      }
      const score = summary.risk_score == null ? "-" : summary.risk_score;
      const otherSignals = (Array.isArray(summary.signals) ? summary.signals : [])
        .filter(signal => !CONTRACT_BOUNDARY_SIGNALS.has(signal));
      const signalText = otherSignals.length ? ` 信号：${otherSignals.join(" / ")}` : "";
      riskSummary.textContent = `合同风险：${summary.risk_level || "-"}，分数：${score}。${summary.summary || ""}${signalText}`;
      renderContractScopeNote(summary);
      renderContractFindings(summary.findings);
    }

    function renderContractScopeNote(summary) {
      const target = document.getElementById("contractScopeNote");
      const signals = Array.isArray(summary?.signals) ? summary.signals : [];
      const notes = [];
      if (signals.includes("project_linked_contracts_only")) {
        notes.push("仅评估关联到本项目的合同；源系统中无项目关联的独立合同不在本入口覆盖范围内。");
      }
      if (signals.includes("contract_text_not_analyzed")) {
        notes.push("本结果只基于合同元数据，未下载附件、未解析合同正文。");
      }
      if (signals.includes("budget_check_unavailable")) {
        notes.push("未能读取项目预算，本次跳过了“合同总额超预算”检查；无该项发现不代表没有超预算。");
      }
      if (!notes.length) {
        target.textContent = "";
        target.classList.add("hidden");
        return;
      }
      target.textContent = notes.join(" ");
      target.classList.remove("hidden");
    }

    function renderContractFindings(findings) {
      const target = document.getElementById("contractFindings");
      target.replaceChildren();
      const items = Array.isArray(findings) ? findings.slice() : [];
      if (!items.length) return;
      items.sort((a, b) => (Number(b?.severity) || 0) - (Number(a?.severity) || 0));
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "contract-finding";

        const severity = document.createElement("span");
        severity.className = `contract-severity ${contractSeverityClass(item?.severity)}`;
        severity.textContent = item?.severity == null ? "-" : String(item.severity);
        row.appendChild(severity);

        const body = document.createElement("div");
        body.className = "contract-finding-body";

        const reason = document.createElement("span");
        reason.className = "contract-finding-reason";
        reason.textContent = item?.reason ? String(item.reason) : String(item?.rule || "未命名规则");
        body.appendChild(reason);

        const metaParts = [];
        if (item?.contract_ref) metaParts.push(String(item.contract_ref));
        if (item?.evidence) metaParts.push(String(item.evidence));
        if (metaParts.length) {
          const meta = document.createElement("span");
          meta.className = "contract-finding-meta";
          meta.textContent = metaParts.join(" · ");
          body.appendChild(meta);
        }

        row.appendChild(body);
        target.appendChild(row);
      }
    }

    function renderContractStatusPills(target, entries) {
      target.replaceChildren();
      for (const [label, value] of entries) {
        const pill = document.createElement("span");
        pill.className = "contract-status-pill";
        pill.textContent = `${label}: ${value == null || value === "" ? "-" : String(value)}`;
        target.appendChild(pill);
      }
    }

    function renderContractDiscovery(data) {
      const contracts = Array.isArray(data?.contracts) ? data.contracts : [];
      const attachments = Array.isArray(data?.attachments) ? data.attachments : [];
      const warnings = Array.isArray(data?.warnings) ? data.warnings : [];
      const statusParts = [`发现 ${contracts.length} 条合同元数据`, `${attachments.length} 个附件候选`];
      if (warnings.length) statusParts.push(`提示 ${warnings.length} 条`);
      document.getElementById("contractDiscoveryStatus").textContent = statusParts.join("，");
      renderContractItems(document.getElementById("contractList"), contracts, item => [
        item.contract_name || item.contract_id || "-",
        item.contract_code || "",
        item.contract_type || "",
        item.total_amount || "",
        item.start_date || "",
        item.end_date || ""
      ]);
      renderContractItems(document.getElementById("contractAttachmentList"), attachments, item => [
        item.file_name || item.attach_id || "-",
        item.file_ext || "",
        item.file_size ? `${item.file_size} bytes` : "",
        item.biz_type || "",
        item.source_table || ""
      ]);
    }

    function renderContractItems(target, items, fields) {
      target.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "muted small";
        empty.textContent = "暂无数据";
        target.appendChild(empty);
        return;
      }
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "contract-item";
        for (const value of fields(item)) {
          const span = document.createElement("span");
          span.textContent = value == null ? "" : String(value);
          row.appendChild(span);
        }
        target.appendChild(row);
      }
    }

    function resetContractDiscoveryUi() {
      state.contractDiscoveryRequestToken += 1;
      setContractRequestButtonsDisabled(false);
      document.getElementById("contractDiscoveryStatus").textContent = "尚未刷新合同资产";
      document.getElementById("contractAssetStatus").replaceChildren();
      document.getElementById("contractAuthorizationStatus").replaceChildren();
      document.getElementById("contractAnalysisStatus").replaceChildren();
      document.getElementById("contractRiskSummary").textContent = "";
      clearContractFindingsUi();
      document.getElementById("contractList").replaceChildren();
      document.getElementById("contractAttachmentList").replaceChildren();
    }

    async function openRiskModal(id, name) {
      const projectId = String(id);
      const requestToken = ++state.modalRequestToken;
      state.activeModalProjectId = projectId;
      state.modalOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      document.getElementById("riskModal").classList.remove("hidden");
      document.body.classList.add("modal-open");
      document.getElementById("riskModalTitle").textContent = name || projectId;
      document.getElementById("riskModalSubtitle").textContent = `项目编号：${projectId}`;
      document.getElementById("riskModalTitle").setAttribute("tabindex", "-1");
      document.getElementById("riskModalTitle").focus({preventScroll: true});
      renderRiskModalLoading();
      resetContractDiscoveryUi();
      resetChatUi(projectId);
      loadChatSessions(projectId).catch(renderChatError);
      try {
        const data = await api(`/api/risk/summary/${encodeURIComponent(projectId)}?limit=12`);
        if (!isCurrentModalRequest(requestToken, projectId)) return;
        renderRiskModal(data, projectId, name);
      } catch (err) {
        if (!isCurrentModalRequest(requestToken, projectId)) return;
        renderRiskModalError(err.message);
      }
    }

    function isCurrentModalRequest(requestToken, projectId) {
      return requestToken === state.modalRequestToken && projectId === state.activeModalProjectId;
    }

    function closeRiskModal() {
      state.modalRequestToken += 1;
      const opener = state.modalOpener;
      state.activeModalProjectId = "";
      state.historyTrendChart = null;
      state.historyTrendWidth = 0;
      if (historyTrendFrame) {
        cancelAnimationFrame(historyTrendFrame);
        historyTrendFrame = 0;
      }
      resetChatUi();
      resetContractDiscoveryUi();
      state.modalOpener = null;
      document.body.classList.remove("modal-open");
      document.getElementById("riskModal").classList.add("hidden");
      if (opener && typeof opener.focus === "function") {
        opener.focus({preventScroll: true});
      }
    }

    function renderRiskModal(data, id, name) {
      if (!data.evaluated) {
        document.getElementById("riskModalContent").classList.add("hidden");
        const empty = document.getElementById("riskModalEmpty");
        empty.classList.remove("hidden");
        const message = document.createTextNode("该项目暂无风险评估记录。");
        const button = document.createElement("button");
        button.textContent = "立即评估该项目";
        button.addEventListener("click", () => {
          closeRiskModal();
          evaluateProject(String(id));
        });
        empty.replaceChildren(message, document.createElement("br"), button);
        return;
      }
      document.getElementById("riskModalContent").classList.remove("hidden");
      document.getElementById("riskModalEmpty").classList.add("hidden");
      const latest = data.latest || {};
      const hits = latest.hits || [];
      document.getElementById("modalRiskLevel").innerHTML = `<span class="badge ${riskClass(latest.level)}">${escapeHtml(latest.level || "-")}</span>`;
      document.getElementById("modalRiskScore").textContent = latest.score ?? "-";
      document.getElementById("modalHitCount").textContent = hits.length;
      document.getElementById("modalEvaluatedAt").textContent = compactDate(latest.created_at);
      document.getElementById("modalExplanation").textContent = latest.explanation || "";
      renderBarChart("modalDimensionChart", data.dimension_chart || [], item => item.name, item => item.score);
      renderBarChart("modalHitChart", Object.entries(data.hit_distribution || {}).map(([name, count]) => ({name, count})), item => item.name, item => item.count, Math.max(1, ...Object.values(data.hit_distribution || {x: 1})));
      renderHistoryTrend(data.history_chart || {points: [], min_score: 0, max_score: 0});
      renderList("modalSuggestionList", latest.suggestions || [], item => escapeHtml(item));
      renderList("modalHistoryList", data.history || [], item => `<strong>${escapeHtml(item.level)} ${escapeHtml(item.score)}</strong><br><span class="muted small">${escapeHtml(item.created_at)} · ${escapeHtml(item.rule_version)}</span><br>${escapeHtml(item.explanation)}`);
    }

    function renderBarChart(targetId, items, labelFn, valueFn, maxValue = 100) {
      const target = document.getElementById(targetId);
      if (!items.length) {
        target.innerHTML = '<div class="muted small">暂无数据</div>';
        return;
      }
      target.innerHTML = items.map(item => {
        const value = Number(valueFn(item) || 0);
        const width = Math.max(4, Math.min(value / maxValue * 100, 100));
        return `<div class="bar-row"><span>${escapeHtml(labelFn(item))}</span><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div><strong>${escapeHtml(value)}</strong></div>`;
      }).join("");
    }

    function trendLabelWidth(label) {
      const width = Array.from(String(label || "")).reduce(
        (total, char) => total + (char.charCodeAt(0) <= 0xff ? 6.4 : 11),
        0
      );
      return Math.max(1, width);
    }

    function ellipsizeTrendLabel(label, maxWidth = 78) {
      const text = String(label || "");
      if (!text || trendLabelWidth(text) <= maxWidth) return text;
      const chars = Array.from(text);
      const ellipsis = "…";
      while (chars.length && trendLabelWidth(chars.join("") + ellipsis) > maxWidth) {
        chars.pop();
      }
      return trendLabelWidth(ellipsis) <= maxWidth ? chars.join("") + ellipsis : "";
    }

    function compactTrendLabel(point, sameDay, width) {
      const createdAt = String(point.created_at || "");
      if (sameDay && width < 520 && createdAt.length >= 16) {
        return String(point.created_at || "").slice(11, 16);
      }
      const label = String(point.label || createdAt || "");
      return width < 360 ? ellipsizeTrendLabel(label) : label;
    }

    function trendLabelBounds(x, label, anchor) {
      const center = Number(x);
      const width = trendLabelWidth(label);
      if (anchor === "start") return {left: center, right: center + width};
      if (anchor === "end") return {left: center - width, right: center};
      return {left: center - width / 2, right: center + width / 2};
    }

    function trendBoundsOverlap(left, right, gap = 10) {
      return left.left < right.right + gap && right.left < left.right + gap;
    }

    function selectTrendLabels(plotted, width, sameDay) {
      if (!plotted.length) return [];
      const maxLabels = width < 360 ? 2 : width < 520 ? 3 : 6;
      const candidateCount = Math.min(maxLabels, plotted.length);
      const candidateIndexes = width < 360
        ? [0, plotted.length - 1]
        : Array.from({length: candidateCount}, (_, position) => (
          candidateCount === 1
            ? 0
            : Math.round(position * (plotted.length - 1) / (candidateCount - 1))
        ));
      const uniqueIndexes = Array.from(new Set(candidateIndexes));
      const makeSelection = index => {
        const {point, x} = plotted[index];
        const anchor = plotted.length === 1 ? "middle" : index === 0 ? "start" : index === plotted.length - 1 ? "end" : "middle";
        const label = compactTrendLabel(point, sameDay, width);
        return {index, anchor, label, bounds: trendLabelBounds(x, label, anchor)};
      };
      const selected = uniqueIndexes
        .filter(index => index === 0 || index === plotted.length - 1)
        .map(makeSelection);
      for (const index of uniqueIndexes) {
        if (index === 0 || index === plotted.length - 1) continue;
        const candidate = makeSelection(index);
        if (selected.some(existing => trendBoundsOverlap(existing.bounds, candidate.bounds))) continue;
        selected.push(candidate);
      }
      return selected.sort((left, right) => left.index - right.index);
    }

    function ensureHistoryTrendResizeObserver() {
      if (historyTrendResizeObserver || typeof ResizeObserver === "undefined") return;
      const target = document.getElementById("modalHistoryChart");
      historyTrendResizeObserver = new ResizeObserver(() => {
        const width = Math.round(target.getBoundingClientRect().width || 0);
        if (!width || Math.abs(width - state.historyTrendWidth) < 1 || !state.historyTrendChart) return;
        if (historyTrendFrame) cancelAnimationFrame(historyTrendFrame);
        historyTrendFrame = requestAnimationFrame(() => {
          historyTrendFrame = 0;
          const modal = document.getElementById("riskModal");
          if (!state.historyTrendChart || modal.classList.contains("hidden")) return;
          renderHistoryTrend(state.historyTrendChart);
        });
      });
      historyTrendResizeObserver.observe(target);
    }

    function renderHistoryTrend(chart) {
      const target = document.getElementById("modalHistoryChart");
      state.historyTrendChart = chart;
      ensureHistoryTrendResizeObserver();
      const points = Array.isArray(chart?.points) ? chart.points : [];
      if (!points.length) {
        target.innerHTML = '<div class="history-trend-empty muted small">暂无历史</div>';
        return;
      }

      const measuredWidth = Math.round(target.getBoundingClientRect().width || 240);
      state.historyTrendWidth = measuredWidth;
      const width = Math.max(240, Math.min(720, measuredWidth));
      const height = width < 360 ? 180 : width < 520 ? 210 : 240;
      const padding = {top: 16, right: 18, bottom: 42, left: 42};
      const plotWidth = width - padding.left - padding.right;
      const plotHeight = height - padding.top - padding.bottom;
      const plotted = points.map((point, index) => {
        const rawScore = Number(point.score);
        const score = Number.isFinite(rawScore) ? Math.max(0, Math.min(rawScore, 100)) : 0;
        const x = points.length === 1
          ? padding.left + plotWidth / 2
          : padding.left + (index / (points.length - 1)) * plotWidth;
        const y = padding.top + ((100 - score) / 100) * plotHeight;
        return {point, score, x: x.toFixed(2), y: y.toFixed(2)};
      });
      const grid = [100, 50, 0].map(score => {
        const y = padding.top + ((100 - score) / 100) * plotHeight;
        return `<line class="trend-grid" x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}"></line><text class="trend-axis-label" x="${padding.left - 8}" y="${y + 4}" text-anchor="end">${score}</text>`;
      }).join("");
      const createdDates = plotted.map(({point}) => String(point.created_at || "").slice(0, 10));
      const sameDay = createdDates.every(date => /^\\d{4}-\\d{2}-\\d{2}$/.test(date) && date === createdDates[0]);
      const labels = selectTrendLabels(plotted, width, sameDay).map(({index, anchor, label}) => {
        const {x} = plotted[index];
        return `<text class="trend-axis-label" x="${x}" y="${height - 14}" text-anchor="${anchor}">${escapeHtml(label)}</text>`;
      }).join("");
      const linePoints = plotted.map(point => `${point.x},${point.y}`).join(" ");
      const circles = plotted.map(({point, score, x, y}) => {
        const tooltip = escapeHtml(`${point.created_at || ""} · ${point.level || ""} · ${score} 分`);
        return `<circle class="trend-point" cx="${x}" cy="${y}" r="4"><title>${tooltip}</title></circle>`;
      }).join("");
      const trendDescription = `纵轴从 0 到 100 分，共 ${plotted.length} 个评分点。` + plotted.map(({point, score}) => `${point.created_at || point.label || "时间未知"}，${point.level || "等级未知"}，${score} 分`).join("；");
      target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="historyTrendTitle historyTrendDescription" preserveAspectRatio="xMidYMid meet"><title id="historyTrendTitle">历史评分趋势</title><desc id="historyTrendDescription">${escapeHtml(trendDescription)}</desc>${grid}<polyline class="trend-line" points="${linePoints}"></polyline>${circles}${labels}</svg>`;
    }

    function compactDate(value) {
      return value ? String(value).slice(0, 16) : "-";
    }

    async function suggestSchema() {
      await withButtonBusy("schemaButton", "识别中", async () => {
        const data = await api("/api/schema/suggest");
        document.getElementById("riskSummary").textContent = `字段映射已生成/读取：${data.mapping_path}`;
        document.getElementById("riskJson").textContent = JSON.stringify(data.mapping, null, 2);
      }).catch(showError);
    }
    document.getElementById("newChatButton").addEventListener("click", () => {
      createChatSession().catch(renderChatError);
    });
    document.getElementById("chatSessionSelect").addEventListener("change", event => {
      selectChatSession(event.target.value).catch(renderChatError);
    });
    document.getElementById("chatForm").addEventListener("submit", sendChatMessage);
    function bind(id, event, handler) {
      const element = document.getElementById(id);
      if (element) element.addEventListener(event, handler);
      else console.warn(`[init] missing element: ${id}`);
    }

    bind("staleRuleReevaluate", "click", reevaluateStaleProjects);
    bind("staleRuleDismiss", "click", () => {
      // For this page load only. Not persisted: the banner describes a real
      // state of the data, and a stored dismissal would hide it permanently
      // while the mixed-version scores stayed mixed.
      state.staleBannerDismissed = true;
      document.getElementById("staleRuleBanner").classList.add("hidden");
    });
    document.getElementById("chatInput").addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        document.getElementById("chatForm").requestSubmit();
      }
    });
    bind("refreshContractAssetsButton", "click", refreshContractAssets);
    bind("createAttachmentAuthorizationDraftsButton", "click", createAttachmentAuthorizationDrafts);
    bind("createContractAnalysisJobButton", "click", createContractAnalysisJob);
    bind("discoverContractsButton", "click", refreshContractAssets);
    bind("categorySearch", "input", filterCategoryOptions);
    bind("layoutSplitter", "pointerdown", handleLayoutResizePointerDown);
    document.getElementById("layoutSplitter").addEventListener("pointermove", handleLayoutResizePointerMove);
    document.getElementById("layoutSplitter").addEventListener("pointerup", handleLayoutResizePointerUp);
    document.getElementById("layoutSplitter").addEventListener("pointercancel", handleLayoutResizePointerUp);
    document.getElementById("projectTableViewport").addEventListener("pointermove", handleTablePanPointerMove);
    document.getElementById("projectTableViewport").addEventListener("pointerup", handleTablePanPointerUp);
    document.getElementById("projectTableViewport").addEventListener("pointercancel", handleTablePanPointerUp);
    document.getElementById("mainChatFloatHeader").addEventListener("pointerdown", handleMainChatFloatPointerDown);
    document.getElementById("mainChatFloatPanel").addEventListener("pointermove", handleMainChatFloatPointerMove);
    document.getElementById("mainChatFloatPanel").addEventListener("pointerup", handleMainChatFloatPointerUp);
    document.getElementById("mainChatFloatPanel").addEventListener("pointercancel", handleMainChatFloatPointerUp);
    document.getElementById("mainChatResizeHandle").addEventListener("pointerdown", handleMainChatResizePointerDown);
    document.getElementById("mainChatResizeHandle").addEventListener("pointermove", handleMainChatResizePointerMove);
    document.getElementById("mainChatResizeHandle").addEventListener("pointerup", handleMainChatResizePointerUp);
    document.getElementById("mainChatResizeHandle").addEventListener("pointercancel", handleMainChatResizePointerUp);
    document.getElementById("minimizeMainChatButton").addEventListener("click", toggleMainChatMinimized);
    document.getElementById("closeMainChatButton").addEventListener("click", closeMainChatFloat);
    document.getElementById("mainChatProjectSearch").addEventListener("input", () => {
      loadMainChatProjects().catch(renderMainChatError);
    });
    document.getElementById("mainChatProjectSelect").addEventListener("change", event => {
      selectMainChatProject(event.target.value).catch(renderMainChatError);
    });
    document.getElementById("mainChatSessionSelect").addEventListener("change", event => {
      selectMainChatSession(event.target.value).catch(renderMainChatError);
    });
    document.getElementById("mainNewChatButton").addEventListener("click", () => {
      createMainChatSession().catch(renderMainChatError);
    });
    document.getElementById("mainChatSendButton").addEventListener("click", sendMainChatMessage);
    document.getElementById("mainChatForm").addEventListener("submit", sendMainChatMessage);
    document.getElementById("mainChatInput").addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        document.getElementById("mainChatForm").requestSubmit();
      }
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !document.getElementById("riskModal").classList.contains("hidden")) {
        closeRiskModal();
      }
    });
    window.addEventListener("resize", () => {
      applyDashboardLayout();
      if (state.mainChatFloatOpen) applyMainChatFloatState();
    });
    initThemeToggle();
    try { document.getElementById("passwordInput").value = localStorage.getItem("agentPassword") || ""; } catch (_) {}
    boot().catch(showError);

    /* ------------------------------------------------- shared with the contract module
     * The contract views are separate script files and borrow these five
     * helpers. They used to reach them through the bare global scope, which
     * made the dependency invisible: renaming `riskClass` here would have
     * broken the contract table at runtime, inside a handler, with nothing to
     * fail first. Naming the surface makes it greppable and testable, and
     * freezing it stops a later script from shadowing a member by accident.
     */
    window.RiskAgent = Object.freeze({
      api,
      parseRiskMarkdown,
      renderRiskMarkdown,
      riskClass,
      contractSeverityClass,
      appendChatMessageToTarget,
      typeChatText,
      // A getter, not the value: the object is frozen, and this is not known
      // until the dashboard loads. Returns null while still unknown, so a
      // caller can say "unknown" rather than guessing.
      llmConfigured: () => state.llmConfigured ?? null,
    });

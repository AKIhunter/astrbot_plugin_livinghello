(() => {
  "use strict";
  const byId = id => document.getElementById(id);
  const data = window.LHMappingExamples;
  const names = { file: "当前映射表已生效", previous: "上一份有效表继续生效", builtin: "正在使用内置规则" };
  let selected = 0;
  const nodeSet = (container, active, stopped = []) => {
    container.querySelectorAll(".node").forEach(node => {
      node.classList.toggle("active", active.some(id => node.id.startsWith("flowchart-" + id + "-")));
      node.classList.toggle("stopped", stopped.some(id => node.id.startsWith("flowchart-" + id + "-")));
    });
  };
  const showCase = index => {
    selected = index;
    const example = data.cases[index];
    const disabled = !data.scenarios[byId("loadScenario").value].policy.enabled;
    byId("caseButtons").querySelectorAll("button").forEach((button, i) =>
      button.setAttribute("aria-pressed", String(i === index)));
    byId("originalPrompt").textContent = example.prompt;
    byId("replacement").textContent = example.source + " → " + example.target;
    const accepted = example.manual.status === "ready";
    const rows = [
      ["手工规则", disabled ? "未应用" : accepted ? "允许替换" : "需要确认", accepted],
      ["LE 修复候选", disabled ? "已关闭自动修复" : example.repair.status === "ready" ? "允许替换" : "需要确认", accepted],
      ["历史成功词对", disabled ? "停止复用" : example.learned.status === "ready" ? "允许复用" : "跳过，保留旧数据", accepted],
    ];
    byId("decisions").replaceChildren(...rows.map(([label, result, allowed]) => {
      const row = document.createElement("div");
      row.className = "decision-row";
      row.dataset.result = allowed || disabled ? "ready" : "review";
      const name = document.createElement("span"), value = document.createElement("span");
      name.textContent = label; value.textContent = result; row.append(name, value);
      return row;
    }));
    byId("outcome").dataset.result = accepted || disabled ? "ready" : "review";
    byId("outcomeTitle").textContent = disabled ? "沿用原描述" : accepted ? "替换通过" : "保留原画面，等待确认";
    byId("outcomeDetail").textContent = disabled
      ? "映射与成功词对均未应用；后续仍检查实际生图权限、长度和额度。"
      : accepted ? "受保护要点保持一致；长度、额度与实际权限通过后，才可请求生图。"
      : "手工规则和 LE 候选不直接生效；历史词对跳过后，原描述继续原有流程。";
    byId("evidence").textContent = JSON.stringify({
      source: "修复后的 ImagePromptMapping，模型与上游均未调用",
      before: example.before, after: example.after,
      manual: example.manual.status, repair: example.repair.status,
      learned: example.learned.status,
    }, null, 2);
    nodeSet(byId("checkFlow"), disabled ? ["ENABLED", "ORIGINAL"]
      : ["ENABLED", "SOURCES", "STRUCTURE", "CHECK", ...(accepted ? ["ACCEPT"] : ["SKIP", "ORIGINAL"])],
      !accepted && !disabled ? ["REVIEW", "NOREQUEST"] : []);
  };
  const showScenario = () => {
    const scenario = data.scenarios[byId("loadScenario").value], policy = scenario.policy;
    byId("ruleCount").textContent = policy.rule_count;
    byId("sourceTitle").textContent = !policy.enabled ? "提示词映射已关闭" : names[policy.rules_source];
    byId("sourceDetail").textContent = scenario.detail;
    byId("statusBoard").dataset.tone = !policy.enabled ? "disabled" : policy.warning ? "warning" : "ready";
    byId("fileName").textContent = "文件 · " + policy.rules_file;
    byId("learnedCount").textContent = "当前模型成功用词 · " + policy.learned_count + " 条";
    byId("loadedAt").textContent = "示例读回 · " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    byId("loadWarning").hidden = !policy.warning;
    byId("loadWarning").textContent = policy.warning;
    byId("loadLog").textContent = scenario.log;
    const path = ["FILE", "EXISTS", "EFFECT"];
    if (policy.load_status !== "missing") path.push("READ", "VALID");
    if (policy.rules_source === "file") path.push("CURRENT");
    if (policy.rules_source === "previous") path.push("HISTORY", "PREVIOUS");
    if (policy.rules_source === "builtin") {
      path.push("BUILTIN");
      if (policy.load_status === "invalid") path.push("HISTORY");
    }
    nodeSet(byId("loadFlow"), path, policy.load_status === "invalid" ? ["VALID"] : []);
    showCase(selected);
  };
  data.cases.forEach((example, index) => {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = example.title;
    button.setAttribute("aria-pressed", String(index === 0));
    button.addEventListener("click", () => showCase(index));
    byId("caseButtons").append(button);
  });
  byId("loadScenario").addEventListener("change", showScenario);
  const showTab = load => {
    byId("loadTab").setAttribute("aria-selected", String(load));
    byId("checkTab").setAttribute("aria-selected", String(!load));
    byId("loadTab").tabIndex = load ? 0 : -1;
    byId("checkTab").tabIndex = load ? -1 : 0;
    byId("loadFlow").hidden = !load;
    byId("checkFlow").hidden = load;
  };
  byId("loadTab").addEventListener("click", () => showTab(true));
  byId("checkTab").addEventListener("click", () => showTab(false));
  document.querySelector(".tabs").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const load = byId("loadTab").getAttribute("aria-selected") !== "true";
    showTab(load); byId(load ? "loadTab" : "checkTab").focus();
  });
  const style = getComputedStyle(document.documentElement);
  mermaid.initialize({
    startOnLoad: false, theme: "base", securityLevel: "strict",
    fontFamily: style.getPropertyValue("--font").trim(),
    themeVariables: {
      primaryColor: style.getPropertyValue("--bg2").trim(),
      primaryTextColor: style.getPropertyValue("--ink").trim(),
      primaryBorderColor: style.getPropertyValue("--rule").trim(),
      lineColor: style.getPropertyValue("--muted").trim(),
      secondaryColor: style.getPropertyValue("--soft").trim(),
      tertiaryColor: style.getPropertyValue("--bg").trim(),
      edgeLabelBackground: style.getPropertyValue("--bg2").trim(),
      fontSize: "16px",
    },
    flowchart: { htmlLabels: false, curve: "linear", nodeSpacing: 16, rankSpacing: 22, padding: 10 },
  });
  // 隐藏容器的字形测量为0；先在可布局状态渲染，再切换面板，避免图被挤成一条长线。
  byId("checkFlow").hidden = false;
  const loadReady = () => {
    showScenario(); showTab(true);
    document.documentElement.classList.add("diagrams-ready");
  };
  document.fonts.ready.then(() => mermaid.run({ nodes: document.querySelectorAll(".mermaid") })).then(loadReady).catch(error => {
    byId("sourceDetail").textContent = "流程图渲染失败：" + error.message;
  });
  showScenario();
})();

(function () {
  "use strict";
  const root = document.querySelector("article.page");
  const status = root.querySelector("#render-status");
  const css = getComputedStyle(document.documentElement);
  const color = name => css.getPropertyValue(name).trim();
  const diagrams = Array.from(root.querySelectorAll(".mermaid"));
  const navLinks = Array.from(root.querySelectorAll("nav a"));
  root.querySelector("#print-button").addEventListener("click", () => window.print());

  function setActive(id) {
    navLinks.forEach(link => {
      const active = link.getAttribute("href") === "#" + id;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }

  navLinks.forEach(link => link.addEventListener("click", () => setActive(link.hash.slice(1))));
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
      if (visible.length) setActive(visible[0].target.id);
    }, {rootMargin: "-90px 0px -60% 0px", threshold: [0, 0.1, 0.4]});
    root.querySelectorAll("main section").forEach(section => observer.observe(section));
  }

  function decorateFigure(pre, svg) {
    const figure = pre.closest("figure");
    const caption = figure.querySelector("figcaption");
    const viewport = figure.querySelector(".diagram-viewport");
    const controls = document.createElement("span");
    controls.className = "diagram-controls";
    controls.setAttribute("aria-label", "流程图缩放");
    let zoom = 1;
    function fit() {
      const available = Math.max(620, viewport.clientWidth - 40);
      const natural = svg.viewBox.baseVal.width || available;
      // 大图保留至少 14px 的默认文字；小图不放大填满整行。
      const width = Math.max(Math.min(available, natural), natural * 14 / 17);
      svg.style.width = Math.ceil(width * zoom) + "px";
    }
    [
      ["缩小", "−", () => { zoom = Math.max(0.75, zoom - 0.25); fit(); }],
      ["适应宽度", "适应", () => { zoom = 1; fit(); }],
      ["放大", "+", () => { zoom = Math.min(2.5, zoom + 0.25); fit(); }]
    ].forEach(([label, text, action]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      button.setAttribute("aria-label", label);
      button.addEventListener("click", action);
      controls.appendChild(button);
    });
    caption.appendChild(controls);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", caption.firstChild.textContent.trim());
    fit();
    window.addEventListener("resize", fit);
  }

  async function renderAll() {
    if (!window.mermaid) {
      status.textContent = "流程图库未加载，保留流程源文。请保持 HTML 与 _shared、assets 目录在一起。";
      status.dataset.state = "failed";
      return;
    }
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "base",
      fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
      themeVariables: {
        fontSize: "17px",
        primaryColor: color("--bg2"),
        primaryTextColor: color("--ink"),
        primaryBorderColor: color("--rule"),
        lineColor: color("--muted"),
        secondaryColor: color("--soft"),
        tertiaryColor: color("--bg2"),
        edgeLabelBackground: color("--bg2"),
        clusterBkg: color("--bg2"),
        clusterBorder: color("--rule")
      },
      flowchart: {useMaxWidth: false, htmlLabels: true, curve: "basis", nodeSpacing: 30, rankSpacing: 46}
    });
    await document.fonts.ready;
    let success = 0;
    for (let index = 0; index < diagrams.length; index += 1) {
      const pre = diagrams[index];
      const source = pre.textContent.trim();
      const classes = [
        "classDef current fill:" + color("--soft") + ",stroke:" + color("--accent") + ",stroke-width:2px,color:" + color("--ink"),
        "classDef stop fill:" + color("--notice") + ",stroke:" + color("--accent2") + ",color:" + color("--ink")
      ].join("\n");
      try {
        const result = await mermaid.render("livinghello-diagram-" + index, source + "\n" + classes);
        pre.innerHTML = result.svg;
        pre.dataset.rendered = "true";
        if (result.bindFunctions) result.bindFunctions(pre);
        decorateFigure(pre, pre.querySelector("svg"));
        success += 1;
      } catch (error) {
        pre.textContent = source;
        pre.dataset.rendered = "failed";
        console.error("Diagram " + (index + 1) + ":", error);
      }
    }
    status.textContent = success === diagrams.length
      ? "全部 " + success + " 张流程图已排版 · 图内可放大与横向滚动"
      : success + " / " + diagrams.length + " 张流程图已排版，其余保留流程源文。";
    status.dataset.state = success === diagrams.length ? "complete" : "failed";
  }
  renderAll();
})();

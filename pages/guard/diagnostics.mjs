/* Native parent download preserves authentication and keeps ZIP bytes out of JSON. */
export function bindDiagnosticExport(button, status, resolveBridge, translate = value => value) {
  let busy = false;
  function label(key) {
    button.textContent = translate(key);
    if (button.dataset) button.dataset.i18n = key;
  }
  function message(key) {
    status.textContent = translate(key);
    if (status.dataset) status.dataset.i18n = key;
  }
  button.addEventListener("click", async () => {
    if (busy) return;
    const bridge = resolveBridge();
    if (typeof bridge?.download !== "function") {
      message("当前宿主不支持诊断包下载，请升级宿主后重试。");
      return;
    }
    busy = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    label("正在打包…");
    message("正在收集近3天的可用记录…");
    let timer;
    try {
      await Promise.race([
        Promise.resolve().then(() => bridge.download("console/diagnostics/export", {})),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("diagnostic_timeout")), 60000);
        }),
      ]);
      message("已发起诊断包下载。提交问题时请补充发生时间和异常现象。");
    } catch (error) {
      const reason = String(error?.message || "");
      message(
        /401|403|unauthorized|forbidden/i.test(reason) ? "登录已失效或无权限，请重新登录宿主后重试。" :
        /409|busy/i.test(reason) ? "已有诊断包正在打包或下载，请稍后重试。" :
        reason === "diagnostic_timeout" ? "下载尚未确认，可能仍在打包，请稍后检查下载记录。" :
        "诊断包导出失败，请稍后重试。"
      );
    } finally {
      clearTimeout(timer);
      busy = false;
      button.disabled = false;
      button.removeAttribute("aria-busy");
      label("导出诊断日志（近3天）");
    }
  });
}

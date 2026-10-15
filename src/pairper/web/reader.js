"use strict";
window.addEventListener("DOMContentLoaded", () => {
  let fallbacks = 0;
  document.querySelectorAll(".math").forEach(node => {
    try {
      katex.render(node.dataset.latex, node.querySelector(".math-output"), {
        displayMode: node.dataset.display === "true",
        throwOnError: true, trust: false, strict: "error", maxExpand: 1000
      });
      node.querySelector(".math-fallback").hidden = true;
    } catch (_) {
      fallbacks += 1;
      node.querySelector(".math-output").hidden = true;
    }
  });
  document.documentElement.dataset.mathFallbacks = String(fallbacks);
  document.documentElement.dataset.readerReady = "true";
});

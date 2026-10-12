// MathJax for pymdownx.arithmatex in generic mode, fetched only by pages that
// have an equation. Eight pages do (three research notes and the adaptive
// fusion design, in both languages); every other page used to download 265 KB
// of MathJax on its first visit and typeset nothing (measured 2026-10-12). The
// site does not use instant navigation, so this runs once per full page load.
window.MathJax = {
  tex: {
    inlineMath: [["\\(", "\\)"]],
    displayMath: [["\\[", "\\]"]],
    processEscapes: true,
    processEnvironments: true
  },
  options: {
    ignoreHtmlClass: ".*|",
    processHtmlClass: "arithmatex"
  }
};

var MATHJAX_URL = "https://unpkg.com/mathjax@3/es5/tex-mml-chtml.js";

document$.subscribe(function () {
  if (!document.querySelector(".arithmatex")) return;
  if (window.MathJax.typesetPromise) {
    window.MathJax.typesetPromise();
    return;
  }
  if (document.querySelector('script[src="' + MATHJAX_URL + '"]')) return;
  // MathJax typesets the page itself once it has loaded (its startup typeset).
  var script = document.createElement("script");
  script.src = MATHJAX_URL;
  script.async = true;
  document.head.appendChild(script);
});

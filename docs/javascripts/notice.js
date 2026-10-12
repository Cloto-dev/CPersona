// A notice from the CPersona project beside the documentation: under the table of contents where the theme
// shows it (60em and wider), at the end of the page on a narrower screen. The notice comes from the project's
// website, and that request is how the documentation's page views are counted: one per day, page and
// language, with nothing about the reader kept. It runs only on the published documentation, so previews and
// local builds neither fetch nor count. Without JavaScript, or when the website does not answer, nothing shows.
(function () {
  var ENDPOINT = "https://cpersona.cloto.dev/api/notice";
  var HOST = "cloto-dev.github.io";
  var WIDE = "(min-width: 60em)";
  if (location.hostname !== HOST || !window.fetch || !window.matchMedia) return;
  var lang = (document.documentElement.getAttribute("lang") || "").slice(0, 2) === "ja" ? "ja" : "en";

  var place = function (box) {
    var toc = document.querySelector(".md-sidebar--secondary .md-sidebar__inner");
    var article = document.querySelector(".md-content__inner");
    var target = window.matchMedia(WIDE).matches && toc ? toc : article;
    if (target && box.parentNode !== target) target.appendChild(box);
  };

  var part = function (tag, className, text) {
    var el = document.createElement(tag);
    el.className = className;
    el.textContent = text;
    return el;
  };

  var render = function (data) {
    var n = data && data.notice;
    if (!n || typeof n.heading !== "string" || typeof n.href !== "string" || !/^(https:|mailto:)/.test(n.href)) return;
    var label = typeof data.label === "string" ? data.label : "";
    var box = document.createElement("aside");
    box.className = "cp-notice";
    box.setAttribute("aria-label", label);
    var link = part("a", "cp-notice-link", String(n.link || ""));
    link.href = n.href;
    box.appendChild(part("p", "cp-notice-label", label));
    box.appendChild(part("p", "cp-notice-heading", n.heading));
    box.appendChild(part("p", "cp-notice-text", String(n.text || "")));
    box.appendChild(link);
    place(box);
    var wide = window.matchMedia(WIDE);
    if (wide.addEventListener) wide.addEventListener("change", function () { place(box); });
  };

  fetch(ENDPOINT + "?page=" + encodeURIComponent(location.pathname) + "&lang=" + lang, { credentials: "omit", cache: "no-store" })
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(render)
    .catch(function () {});
})();

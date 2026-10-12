// Site search reads the index of the page's language. scripts/docs_by_language.py writes the Japanese entries
// to search/search_index.ja.json; the theme requests search/search_index.json with XMLHttpRequest when its
// bundle starts, so on a Japanese page that request is pointed at the Japanese file before the bundle runs.
(function () {
  try {
    if ((document.documentElement.getAttribute("lang") || "").slice(0, 2) !== "ja") return;
    var open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      var args = Array.prototype.slice.call(arguments);
      if (/(^|\/)search\/search_index\.json$/.test(String(url))) {
        args[1] = String(url).replace(/search_index\.json$/, "search_index.ja.json");
      }
      return open.apply(this, args);
    };
  } catch (e) {}
})();

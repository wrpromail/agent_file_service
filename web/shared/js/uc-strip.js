/**
 * 同步剥地址栏工作台 token，必要时 hop 到 /auth。
 * 必须是经典脚本（非 module），在 React/ESM 启动前执行。
 */
(function () {
  try {
    var KEYS = ["token", "workbench_token"];
    var HANDOFF = "afs_uc_handoff_token";
    var url = new URL(location.href);
    var token = "";
    for (var i = 0; i < KEYS.length; i++) {
      var t = (url.searchParams.get(KEYS[i]) || "").trim();
      if (t && !token) token = t;
      url.searchParams.delete(KEYS[i]);
    }
    var hash = url.hash || "";
    if (hash.indexOf("?") >= 0) {
      var q = hash.indexOf("?");
      var pathPart = hash.slice(1, q);
      var hp = new URLSearchParams(hash.slice(q + 1));
      for (var j = 0; j < KEYS.length; j++) {
        var ht = (hp.get(KEYS[j]) || "").trim();
        if (ht && !token) token = ht;
        hp.delete(KEYS[j]);
      }
      var rest = hp.toString();
      hash = rest
        ? "#" + pathPart + "?" + rest
        : pathPart && pathPart !== "/"
          ? "#" + pathPart
          : "";
    }
    var next = url.pathname + url.search + hash;
    var cur = location.pathname + location.search + location.hash;
    if (next !== cur) history.replaceState({}, "", next);
    if (token) {
      try {
        sessionStorage.setItem(HANDOFF, token);
      } catch {}
    }
    var here = (location.pathname || "/").split("?")[0];
    var atAuth = here === "/auth" || here === "/auth/" || here === "/auth/index.html";
    var hashBody = (location.hash || "").replace(/^#/, "");
    var hashPath = hashBody.split("?")[0].replace(/\/+$/, "") || "/";
    var atAuthHash = hashPath === "/auth" || hashPath === "auth";
    var atRoot = here === "/" || here === "/index.html";
    if (!atAuth && (token || (atRoot && atAuthHash))) {
      window.__AFS_UC_AUTH_HOP__ = true;
      location.replace("/auth");
    }
  } catch {}
})();

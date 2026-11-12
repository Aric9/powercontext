// 引用一键复制（无外部依赖；clipboard 不可用时退化为临时 textarea）。
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest(".copy-btn");
  if (!btn) return;
  var text = btn.getAttribute("data-copy") || "";
  function done(ok) {
    var old = btn.textContent;
    btn.textContent = ok ? "已复制" : "复制失败";
    setTimeout(function () { btn.textContent = old; }, 1200);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, function () { fallback(); });
  } else {
    fallback();
  }
  function fallback() {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    done(ok);
  }
});

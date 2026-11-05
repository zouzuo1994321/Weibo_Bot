// 侧栏滑块逻辑冒烟：node nav_highlight_smoke.cjs <extracted.js>
// extracted.js 为从 app.js 抽出的 moveNavHighlight 代码块
const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");

function mkEl(cls) {
  const s = new Set(cls || []);
  return {
    style: {},
    classList: {
      add: (c) => s.add(c),
      remove: (c) => s.delete(c),
      contains: (c) => s.has(c),
      toggle: (c, f) => { if (f === undefined) { s.has(c) ? s.delete(c) : s.add(c); } else { f ? s.add(c) : s.delete(c); } },
    },
    offsetHeight: 100,
  };
}
const hi = mkEl();
const sidebar = mkEl();
global.document = { getElementById: (id) => (id === "navHighlight" ? hi : sidebar) };

eval(code);

function navItem(top, h, sub) {
  return Object.assign(mkEl(sub ? ["nav-item", "sub"] : ["nav-item"]), { offsetTop: top, offsetHeight: h });
}
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) pass++;
  else { fail++; console.log("FAIL " + name + " -> " + detail); }
}

moveNavHighlight(navItem(100, 38, false), undefined);
check("first.transform", hi.style.transform === "translate(0px,100px)", hi.style.transform);
check("first.right", hi.style.right === "0px", hi.style.right);
check("first.height", hi.style.height === "38px", hi.style.height);
check("first.transition", hi.style.transition === "", String(hi.style.transition));
check("first.on", hi.classList.contains("on"));

moveNavHighlight(navItem(140, 38, false), undefined);
check("same.dur", hi.style.transitionDuration === "0.244s", hi.style.transitionDuration);
check("same.transform", hi.style.transform === "translate(0px,140px)", hi.style.transform);

moveNavHighlight(navItem(200, 35, true), undefined);
check("cross.dur", hi.style.transitionDuration === "0.290s", hi.style.transitionDuration);
check("cross.transform", hi.style.transform === "translate(22px,200px)", hi.style.transform);
check("cross.right", hi.style.right === "22px", hi.style.right);
check("cross.height", hi.style.height === "35px", hi.style.height);

moveNavHighlight(navItem(460, 38, false), undefined);
check("far.dur", hi.style.transitionDuration === "0.510s", hi.style.transitionDuration);

moveNavHighlight(navItem(1400, 38, false), undefined);
check("clamp.dur", hi.style.transitionDuration === "0.520s", hi.style.transitionDuration);

moveNavHighlight(navItem(120, 32, false), false);
check("realign.transform", hi.style.transform === "translate(0px,120px)", hi.style.transform);
check("realign.right", hi.style.right === "0px", hi.style.right);
check("realign.transition", hi.style.transition === "", String(hi.style.transition));

moveNavHighlight(navItem(121, 32, false), undefined);
check("min.dur", hi.style.transitionDuration === "0.220s", hi.style.transitionDuration);

console.log("NAV_SMOKE " + pass + "/" + (pass + fail));
process.exit(fail ? 1 : 0);

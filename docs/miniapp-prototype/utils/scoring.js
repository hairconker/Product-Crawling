/* 评分引擎（纯函数，框架无关）
   对齐《docs/小白装机问卷设计稿.md》§十一（评分）/§十二（方向）/§十三（预算）/§十四（三档）。 */

const { QUESTIONS, Q1_GROUPS, SIDE_WEIGHTS } = require("../data/questions.js");
const { BUDGET_BUCKETS, CONFIG_TEMPLATES } = require("../data/configs.js");

function scoreAnswers(answers, sideState){
  const S = { cpu:0, gpu:0, balance:0, office:0, upgrade:0, budgetHint:null, budgetFloor:null, peripheral:0, side:[] };
  for (const q of QUESTIONS){
    const optId = (answers || {})[q.id];
    if (!optId) continue;
    const opt = q.opts.find(o => o.id === optId);
    if (!opt) continue;
    const s = opt.s || {};
    if (s.cpu)     S.cpu     += s.cpu;
    if (s.gpu)     S.gpu     += s.gpu;
    if (s.balance) S.balance += s.balance;
    if (s.office)  S.office  += s.office;
    if (s.upgrade !== undefined)     S.upgrade     = s.upgrade;
    if (s.budget !== undefined)      S.budgetHint  = s.budget;
    if (s.budgetFloor !== undefined) S.budgetFloor = s.budgetFloor;
    if (s.peripheral !== undefined)  S.peripheral  = s.peripheral;
  }
  for (const g of Q1_GROUPS){
    const list = (sideState && sideState[g.id]) || [];
    list.forEach((id, i) => {
      const opt = g.opts.find(o => o.id === id);
      if (!opt) return;
      const w = SIDE_WEIGHTS[i] !== undefined ? SIDE_WEIGHTS[i] : SIDE_WEIGHTS[SIDE_WEIGHTS.length - 1];
      const s = opt.s || {};
      if (s.cpu)     S.cpu     += s.cpu * w;
      if (s.gpu)     S.gpu     += s.gpu * w;
      if (s.balance) S.balance += s.balance * w;
      if (s.office)  S.office  += s.office * w;
      S.side.push({ id, text: opt.text, group: g.title, rank: i + 1, weight: w });
    });
  }
  for (const k of ["cpu","gpu","balance","office"]) S[k] = Math.round(S[k] * 10) / 10;
  return S;
}

function decideDirection(S){
  const reason = [];
  if (S.office >= 5 && S.gpu <= 2){
    reason.push("office=" + S.office + "≥5 且 gpu=" + S.gpu + "≤2");
    return { dir:"低功耗办公型", reason };
  }
  if (S.gpu - S.cpu >= 2){
    reason.push("gpu-cpu=" + (S.gpu - S.cpu).toFixed(1) + "≥2");
    return { dir:"GPU优先", reason };
  }
  if (S.cpu - S.gpu >= 2){
    reason.push("cpu-gpu=" + (S.cpu - S.gpu).toFixed(1) + "≥2");
    return { dir:"CPU优先", reason };
  }
  reason.push("|cpu-gpu|=" + Math.abs(S.cpu - S.gpu).toFixed(1) + "<2");
  return { dir:"均衡型", reason };
}

function decideBudget(S){
  let level, source;
  if (S.budgetFloor !== null){ level = S.budgetFloor; source = "第8题实际预算"; }
  else if (S.budgetHint !== null){ level = S.budgetHint; source = "第7题心理倾向"; }
  else { level = 2; source = "默认"; }
  const total = BUDGET_BUCKETS[level];
  const hostPlan = Math.max(total.plan - S.peripheral, BUDGET_BUCKETS[0].plan);
  let hostLevel = BUDGET_BUCKETS.findIndex(b => hostPlan >= b.lo && hostPlan <= b.hi);
  if (hostLevel < 0) hostLevel = 0;
  return {
    source, totalLevel: level, totalBucket: total,
    peripheralCost: S.peripheral,
    hostPlan, hostLevel, hostBucket: BUDGET_BUCKETS[hostLevel],
  };
}

function decideConfig(direction, hostLevel, S){
  const tpl = CONFIG_TEMPLATES[direction][hostLevel];
  const boosted = (S.upgrade >= 2);
  return [
    makeTier("低档 · 够用版",     tpl, -1, boosted),
    makeTier("中档 · 推荐版",     tpl,  0, boosted),
    makeTier("高档 · 一步到位版", tpl, +1, boosted),
  ];
}

function makeTier(name, tpl, offset, boosted){
  const scale = offset === -1 ? 0.85 : (offset === 1 ? 1.22 : 1);
  const lo = Math.round(tpl.range[0] * scale / 100) * 100;
  const hi = Math.round(tpl.range[1] * scale / 100) * 100;
  const mem = offset === -1 ? downMem(tpl.mem) : ((offset === 1 || boosted) ? upMem(tpl.mem) : tpl.mem);
  const ssd = offset === -1 ? downSsd(tpl.ssd) : ((offset === 1 || boosted) ? upSsd(tpl.ssd) : tpl.ssd);
  const psu = offset === 1 ? upPsu(tpl.psu) : tpl.psu;
  return { name, priceRange:[lo,hi], cpu: tpl.cpu, gpu: tpl.gpu, mem, ssd, psu, mb: tpl.mb };
}

function upMem(m)  { return m.replace(/8GB/,"16GB").replace(/16GB/,"32GB").replace(/32GB/,"64GB"); }
function downMem(m){ return m.replace(/64GB/,"32GB").replace(/32GB/,"16GB").replace(/16GB/,"8GB"); }
function upSsd(s)  { return s.replace(/256GB/,"512GB").replace(/500GB/,"1TB").replace(/512GB/,"1TB").replace(/1TB/,"2TB"); }
function downSsd(s){ return s.replace(/2TB/,"1TB").replace(/1TB/,"512GB").replace(/512GB/,"256GB").replace(/500GB/,"256GB"); }
function upPsu(p)  { return p.replace(/300W/,"450W").replace(/400W/,"550W").replace(/450W/,"550W").replace(/500W/,"650W").replace(/550W/,"750W").replace(/650W/,"850W").replace(/750W/,"850W").replace(/850W/,"1000W"); }

function suggestUpgrade(direction, S){
  const tips = [];
  if (S.upgrade >= 2) tips.push("重视寿命/升级：内存从 16GB 起步 32GB，SSD 从 512GB 升 1TB，电源留一档冗余。");
  if (direction === "GPU优先" && S.cpu <= 2) tips.push("显卡档位高但 CPU 分偏低：别让 CPU 拖后腿（i5-12400F / R5-7500F 起步）。");
  if (direction === "CPU优先" && S.gpu <= 1) tips.push("CPU 向且基本不玩游戏：只配亮机卡或核显，预算给 CPU/内存/SSD。");
  if (direction === "均衡型") tips.push("均衡型建议 B760/B650 + 16~32GB + RTX 4060 级显卡。");
  if (direction === "低功耗办公型") tips.push("办公向可选 APU（R7-8700G / R5-8600G）免独显。");
  if (S.peripheral > 0) tips.push("外设约扣除 ¥" + S.peripheral + "，主机预算已自动下调。");
  return tips;
}

function decideAll(answers, sideState){
  const missing = [];
  const sideTotal = Q1_GROUPS.reduce(function(n, g){
    return n + ((sideState && sideState[g.id] && sideState[g.id].length) || 0);
  }, 0);
  if (sideTotal === 0) missing.push("Q1（三类至少选 1 项）");
  for (let i = 0; i < QUESTIONS.length; i++){
    const q = QUESTIONS[i];
    if (!(answers || {})[q.id]) missing.push("Q" + (i + 2));
  }
  if (missing.length){
    return { error: "还有 " + missing.length + " 项未完成：" + missing.join(" / ") };
  }
  const S = scoreAnswers(answers, sideState);
  const dir = decideDirection(S);
  dir.reasonText = dir.reason.join("；");
  const budget = decideBudget(S);
  const tiers = decideConfig(dir.dir, budget.hostLevel, S);
  const tips = suggestUpgrade(dir.dir, S);
  return { S, dir, budget, tiers, tips, sideTags: S.side };
}

module.exports = { scoreAnswers, decideDirection, decideBudget, decideConfig, suggestUpgrade, decideAll };

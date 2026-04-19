const { QUESTIONS, Q1_GROUPS, SIDE_WEIGHTS } = require("../../data/questions.js");
const { decideAll } = require("../../utils/scoring.js");
const SAMPLES = require("../../data/samples.js");

function emptySideState(){
  const s = {};
  for (const g of Q1_GROUPS) s[g.id] = [];
  return s;
}

Page({
  data: {
    questions: QUESTIONS,
    q1Groups: [],
    answers: {},
    sideState: {},
    sampleIdx: 0,
    result: null,
  },

  onLoad(){
    const sideState = emptySideState();
    this.setData({ sideState });
    this.refreshGroups(sideState);
  },

  refreshGroups(stateOverride){
    const sideState = stateOverride || this.data.sideState;
    const groups = Q1_GROUPS.map(g => {
      const list = sideState[g.id] || [];
      const picked = list.map((id, i) => {
        const opt = g.opts.find(o => o.id === id);
        const weight = SIDE_WEIGHTS[i] !== undefined ? SIDE_WEIGHTS[i] : SIDE_WEIGHTS[SIDE_WEIGHTS.length - 1];
        return { id, text: opt ? opt.text : id, weight };
      });
      const pool = g.opts.filter(o => !list.includes(o.id));
      return { id: g.id, title: g.title, hint: g.hint, pool, picked };
    });
    this.setData({ q1Groups: groups });
  },

  onRadio(e){
    const qid = e.currentTarget.dataset.qid;
    this.setData({ ["answers." + qid]: e.detail.value });
  },

  addSide(e){
    const { group, id } = e.currentTarget.dataset;
    const arr = (this.data.sideState[group] || []).slice();
    if (arr.includes(id)) return;
    arr.push(id);
    this.setData({ ["sideState." + group]: arr });
    this.refreshGroups(Object.assign({}, this.data.sideState, { [group]: arr }));
  },

  removeSide(e){
    const { group, id } = e.currentTarget.dataset;
    const arr = (this.data.sideState[group] || []).filter(x => x !== id);
    this.setData({ ["sideState." + group]: arr });
    this.refreshGroups(Object.assign({}, this.data.sideState, { [group]: arr }));
  },

  moveUp(e){
    const { group, id } = e.currentTarget.dataset;
    const arr = (this.data.sideState[group] || []).slice();
    const i = arr.indexOf(id);
    if (i > 0){ const t = arr[i-1]; arr[i-1] = arr[i]; arr[i] = t; }
    this.setData({ ["sideState." + group]: arr });
    this.refreshGroups(Object.assign({}, this.data.sideState, { [group]: arr }));
  },

  moveDown(e){
    const { group, id } = e.currentTarget.dataset;
    const arr = (this.data.sideState[group] || []).slice();
    const i = arr.indexOf(id);
    if (i >= 0 && i < arr.length - 1){ const t = arr[i+1]; arr[i+1] = arr[i]; arr[i] = t; }
    this.setData({ ["sideState." + group]: arr });
    this.refreshGroups(Object.assign({}, this.data.sideState, { [group]: arr }));
  },

  onSubmit(){
    const result = decideAll(this.data.answers, this.data.sideState);
    this.setData({ result });
    wx.pageScrollTo({ selector: ".result", duration: 300 });
  },

  onReset(){
    const sideState = emptySideState();
    this.setData({ answers: {}, sideState, result: null });
    this.refreshGroups(sideState);
  },

  onSample(){
    const s = SAMPLES[this.data.sampleIdx % SAMPLES.length];
    const sideState = emptySideState();
    for (const gid in s.side) sideState[gid] = (s.side[gid] || []).slice();
    const nextIdx = this.data.sampleIdx + 1;
    this.setData({
      answers: Object.assign({}, s.picks),
      sideState,
      sampleIdx: nextIdx,
    });
    this.refreshGroups(sideState);
    setTimeout(() => this.onSubmit(), 50);
  },
});

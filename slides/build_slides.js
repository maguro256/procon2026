// プロコン発表スライド（12 枚）の生成スクリプト
//
// 使い方:  node slides/build_slides.js   → slides/gemmba_presentation.pptx
//   初回のみ: cd slides && npm install
//   ※ 生成後は PowerPoint で直接手直しして構いません（このスクリプトを再実行すると上書きされます）
//
// 手直しのしやすさのために:
//   - 色はテーマの配色（デザイン → バリエーション → 配色）、フォントはテーマのフォントにまとめてある
//   - タイトル・ページ番号・フッターはスライドマスターのレイアウト（「表紙」「本文」）側にある
//   - 図形にはすべて名前を付けている（ホーム → 選択 → オブジェクトの選択と表示 で確認できる）
//   - 各スライドのノートに話す内容の下書きを入れてある

const path = require("path");
const pptxgen = require("pptxgenjs");
const JSZip = require("jszip");
const fs = require("fs");

const ROOT = path.join(__dirname, "..");
const IMG = (f) => path.join(ROOT, "resume", f);
const OUT = path.join(__dirname, "gemmba_presentation.pptx");

// ---------------------------------------------------------------- テーマ（ポスターと同じ配色）
const FONT = "BIZ UDPGothic";
const THEME = {
  name: "Gemmba",
  colors: {
    dk1: "1B1B1A", // 本文
    lt1: "FFFFFF", // 背景
    dk2: "1F3A5F", // 紺：見出し・強調
    lt2: "EEF1F5", // 薄い灰青：カードの地
    accent1: "2A78D6", // 青：提案・強調
    accent2: "EB6834", // 橙：注意・対比
    accent3: "8A8984", // 灰：比較手法
    accent4: "52514E", // 濃い灰：補足文字
    accent5: "DCE8F7", // 薄い青：強調カードの地
    accent6: "3C8A5A", // 緑（予備）
    hlink: "2A78D6",
    folHlink: "1F3A5F",
  },
};

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.33 × 7.5 インチ
pres.title = "Gemmba プロコン発表";
pres.theme = { headFontFace: FONT, bodyFontFace: FONT };
const C = pres.SchemeColor;
const NAVY = C.text2, ACCENT = C.accent1, ORANGE = C.accent2, GRAY = C.accent3,
  SUB = C.accent4, PAPER = C.background2, SOFT = C.accent5, WHITE = C.background1, INK = C.text1;

const W = 13.333, MX = 0.6; // スライド幅・左右余白
const CW = W - MX * 2; // 本文の幅

// ---------------------------------------------------------------- レイアウト
pres.defineSlideMaster({
  title: "表紙",
  background: { color: NAVY },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: MX, y: 2.1, w: CW, h: 1.9,
      fontSize: 40, bold: true, color: WHITE, align: "left", valign: "bottom", margin: 0 }, text: "タイトル" } },
    { placeholder: { options: { name: "subtitle", type: "body", x: MX, y: 4.2, w: CW, h: 0.8,
      fontSize: 22, color: SOFT, align: "left", valign: "top", margin: 0 }, text: "サブタイトル" } },
    { placeholder: { options: { name: "presenter", type: "body", x: MX, y: 6.2, w: CW, h: 0.6,
      fontSize: 18, color: WHITE, align: "left", valign: "top", margin: 0 }, text: "チーム名・発表者名" } },
  ],
});

pres.defineSlideMaster({
  title: "本文",
  background: { color: WHITE },
  margin: [0.4, MX, 0.6, MX],
  objects: [
    { placeholder: { options: { name: "section", type: "body", x: MX, y: 0.3, w: CW, h: 0.35,
      fontSize: 14, bold: true, color: ACCENT, align: "left", valign: "middle", margin: 0 }, text: "セクション名" } },
    { placeholder: { options: { name: "title", type: "title", x: MX, y: 0.65, w: CW, h: 0.8,
      fontSize: 32, bold: true, color: NAVY, align: "left", valign: "middle", margin: 0 }, text: "スライドのタイトル" } },
    { text: { text: "Gemmba", options: { x: MX, y: 7.0, w: 3, h: 0.3, fontSize: 11, color: GRAY, margin: 0 } } },
  ],
  slideNumber: { x: W - MX - 0.8, y: 7.0, w: 0.8, h: 0.3, fontSize: 11, color: GRAY, align: "right", margin: 0 },
});

// ---------------------------------------------------------------- 部品
const T = { isTextBox: true, lang: "ja-JP" };
const BODY_Y = 1.6; // 本文の開始位置

// 本文スライドを追加する。セクション（スライド一覧の区切り）と左上の小見出しを同時に付ける
const SECTIONS = [
  "1. 現状の問題点", "2. 本システムの目的", "3. システムの概要", "4. システム導入のメリット",
  "5. ユースケース", "6. AI の妥当性", "7. まとめと今後の展望",
];
let currentSection = null;
function page(no) {
  const sec = SECTIONS[no - 1];
  if (sec !== currentSection) { pres.addSection({ title: sec }); currentSection = sec; }
  const slide = pres.addSlide({ masterName: "本文", sectionTitle: sec });
  slide.addText(sec, { placeholder: "section", lang: "ja-JP" });
  return slide;
}

function title(slide, text) {
  slide.addText(text, { placeholder: "title", lang: "ja-JP" });
}

// 地色つきのカード（見出し＋本文）
function card(slide, name, x, y, w, h, head, body, opt = {}) {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    objectName: name, x, y, w, h, rectRadius: 0.08,
    fill: { color: opt.fill || PAPER }, line: { color: opt.line || opt.fill || PAPER, width: opt.line ? 1.5 : 0 },
  });
  if (!head && !(Array.isArray(body) ? body.length : body)) return;
  const runs = [{ text: head, options: { bold: true, fontSize: opt.headSize || 20, color: opt.headColor || NAVY, breakLine: true } }];
  const lines = Array.isArray(body) ? body : [body];
  lines.forEach((l, i) => runs.push({ text: l, options: {
    fontSize: opt.bodySize || 16, color: INK, bullet: opt.bullet ? { indent: 16 } : false,
    paraSpaceBefore: i === 0 ? 8 : 4, breakLine: i < lines.length - 1 } }));
  slide.addText(runs, { ...T, objectName: name + "_文字", x: x + 0.25, y: y + 0.2, w: w - 0.5, h: h - 0.4,
    valign: "top", margin: 0 });
}

// 丸数字アイコン
function badge(slide, name, x, y, d, label, color = ACCENT) {
  slide.addShape(pres.shapes.OVAL, { objectName: name, x, y, w: d, h: d, fill: { color }, line: { color, width: 0 } });
  slide.addText(label, { ...T, objectName: name + "_文字", x, y, w: d, h: d, align: "center", valign: "middle",
    fontSize: d * 26, bold: true, color: WHITE, margin: 0 });
}

function arrow(slide, name, x, y, w, h, color = GRAY) {
  slide.addShape(pres.shapes.RIGHT_ARROW, { objectName: name, x, y, w, h, fill: { color }, line: { color, width: 0 } });
}

function box(slide, name, x, y, w, h, head, sub, opt = {}) {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    objectName: name, x, y, w, h, rectRadius: 0.08,
    fill: { color: opt.fill || WHITE }, line: { color: opt.line || NAVY, width: 2 },
  });
  slide.addText([
    { text: head, options: { bold: true, fontSize: 20, color: NAVY, breakLine: true } },
    { text: sub, options: { fontSize: 14, color: SUB, paraSpaceBefore: 4 } },
  ], { ...T, objectName: name + "_文字", x, y, w, h, align: "center", valign: "middle", margin: 0.1 });
}

function note(slide, text) { slide.addNotes(text); }

// ================================================================ 1. 表紙
{
  pres.addSection({ title: "表紙" });
  const s = pres.addSlide({ masterName: "表紙", sectionTitle: "表紙" });
  s.addText("社員証をタッチするだけで、\n次にやる仕事が決まる工場へ", { placeholder: "title", lang: "ja-JP" });
  s.addText("Gemmba ─ NFC タッチ起点のタスク自動割当システム", { placeholder: "subtitle", lang: "ja-JP" });
  s.addText("（チーム名・発表者名を入力）", { placeholder: "presenter", lang: "ja-JP" });
  note(s, "【30 秒】\n" +
    "私たちは、工場の共有機材に社員証をタッチするだけで、その人が今やるべき仕事を自動で割り当てるシステム「Gemmba」を開発しました。\n" +
    "今日は、このシステムを導入すると現場の作業者・管理者・工場全体にどんなメリットがあるのかを中心にお話しします。");
}

// ================================================================ 2. 現状の問題点
{
  const s = page(1);
  title(s, "中小工場の共有機材で起きていること");
  s.addText("レーザー加工機・旋盤・プレス機などを複数人で共有。専任の IT 担当者はいないことが多い", {
    ...T, objectName: "前提", x: MX, y: BODY_Y, w: CW, h: 0.5, fontSize: 18, color: SUB, margin: 0 });
  const items = [
    ["使用状況が見えない", ["誰がどの機材を何に使っているか分からない", "予約したのに来ない「ゴースト予約」"]],
    ["仕事が偏る", ["重要な仕事が特定のベテランに集中", "急ぎの仕事が後回し・忘れられる"]],
    ["探す・聞く手間", ["空いている機材や次の仕事を探して歩く", "そのたびに管理者に聞く・報告する"]],
  ];
  const gw = 0.35, cw = (CW - gw * 2) / 3, cy = 2.4, ch = 3.0;
  items.forEach(([h, b], i) => {
    const x = MX + i * (cw + gw);
    card(s, `課題${i + 1}`, x, cy, cw, ch, "", [], {});
    s.addText([
      { text: h, options: { bold: true, fontSize: 22, color: NAVY, breakLine: true } },
      ...b.map((t, j) => ({ text: t, options: { fontSize: 16, color: INK, bullet: { indent: 16 }, paraSpaceBefore: 10, breakLine: j < b.length - 1 } })),
    ], { ...T, objectName: `課題${i + 1}_文字`, x: x + 0.3, y: cy + 1.1, w: cw - 0.6, h: ch - 1.3, valign: "top", margin: 0 });
  });
  // 番号はカードより手前に
  items.forEach((_, i) => badge(s, `課題${i + 1}_番号`, MX + i * (cw + gw) + 0.3, cy + 0.3, 0.6, String(i + 1), NAVY));
  s.addText("→ 「計画を立てるコスト」と「探すコスト」が現場の時間を奪っている", {
    ...T, objectName: "まとめ", x: MX, y: 5.85, w: CW, h: 0.6, fontSize: 20, bold: true, color: ACCENT, margin: 0 });
  note(s, "【1 分】\n" +
    "対象は、加工機などの機材を何人もで共有している中小工場です。こうした工場には専任の IT 担当者がいないことがほとんどです。\n" +
    "現場では三つの困りごとがあります。①誰が何に使っているか見えない。予約だけして来ない“ゴースト予約”も起きる。②重要な仕事がベテランに偏ったり、忘れられたりする。③空いている機材や次の仕事を探して歩き、管理者に聞きに行く。\n" +
    "つまり、計画を立てる手間と探す手間が現場の時間を奪っています。");
}

// ================================================================ 3. 本システムの目的
{
  const s = page(2);
  title(s, "目的：計画と探す手間をなくし、人と仕事を最適に結び付ける");
  const rows = [
    ["使用状況が見えない", "機材と人の状況を、常に正しく見える化する"],
    ["仕事が偏る・忘れられる", "実績データをもとに、偏りなく最適な人へ割り当てる"],
    ["探す・聞く手間", "タッチした瞬間に、その場でやるべき仕事を示す"],
  ];
  const lw = 3.6, aw = 0.7, rh = 1.05, gy = 0.25, y0 = BODY_Y + 0.15;
  const rx = MX + lw + aw + 0.3, rw = W - MX - rx;
  s.addText("いまの問題", { ...T, objectName: "目的_見出し左", x: MX, y: y0 - 0.05, w: lw, h: 0.35, fontSize: 14, color: SUB, margin: 0 });
  s.addText("Gemmba の目的", { ...T, objectName: "目的_見出し右", x: rx, y: y0 - 0.05, w: 4, h: 0.35, fontSize: 14, color: ACCENT, bold: true, margin: 0 });
  rows.forEach(([a, b], i) => {
    const y = y0 + 0.4 + i * (rh + gy);
    card(s, `目的_問題${i + 1}`, MX, y, lw, rh, "", []);
    s.addText(a, { ...T, objectName: `目的_問題${i + 1}_文字`, x: MX + 0.25, y, w: lw - 0.5, h: rh, fontSize: 18, color: SUB, valign: "middle", margin: 0 });
    arrow(s, `目的_矢印${i + 1}`, MX + lw + 0.2, y + rh / 2 - 0.22, aw + 0.1 - 0.2, 0.44, ACCENT);
    card(s, `目的_目標${i + 1}`, rx, y, rw, rh, "", [], { fill: SOFT });
    s.addText(b, { ...T, objectName: `目的_目標${i + 1}_文字`, x: rx + 0.3, y, w: rw - 0.6, h: rh, fontSize: 20, bold: true, color: NAVY, valign: "middle", margin: 0 });
  });
  s.addText([
    { text: "設計の方針：", options: { bold: true, color: NAVY } },
    { text: "遠隔予約を作らない ／ 操作はタッチとボタンだけ ／ IT 担当者がいなくても導入できる", options: { color: INK } },
  ], { ...T, objectName: "目的_方針", x: MX, y: 6.15, w: CW, h: 0.5, fontSize: 18, margin: 0 });
  note(s, "【1 分】\n" +
    "そこで本システムの目的は、計画を立てる手間と探す手間をなくし、人と仕事を最適に結び付けることです。\n" +
    "先ほどの三つの問題に対応して、①機材と人の状況を常に正しく見える化する、②実績データをもとに偏りなく最適な人へ割り当てる、③タッチした瞬間にその場でやるべき仕事を示す、の三つを目指します。\n" +
    "そのために、遠隔予約は作らない、操作はタッチとボタンだけ、IT 担当者がいなくても導入できる、という方針で設計しました。");
}

// ================================================================ 4. システムの概要：構成
{
  const s = page(3);
  title(s, "システムの構成");
  const by = 1.75, bh = 1.45;
  box(s, "構成_モジュール", MX, by, 2.6, bh, "機材モジュール", "機材ごとに 1 台\nNFC・画面・ボタン・LED");
  arrow(s, "構成_矢印1", MX + 2.7, by + 0.5, 0.7, 0.5);
  s.addText("MQTT", { ...T, objectName: "構成_MQTT", x: MX + 2.6, y: by + 1.05, w: 0.9, h: 0.3, fontSize: 12, color: SUB, align: "center", margin: 0 });
  box(s, "構成_サーバ", MX + 3.5, by, 2.4, bh, "サーバ", "Flask + SQLite\n作業者・タスク・ログ");
  arrow(s, "構成_矢印2", MX + 6.0, by + 0.5, 0.7, 0.5);
  s.addText("ブラウザ", { ...T, objectName: "構成_HTTP", x: MX + 5.9, y: by + 1.05, w: 0.9, h: 0.3, fontSize: 12, color: SUB, align: "center", margin: 0 });
  box(s, "構成_管理画面", MX + 6.8, by, 2.0, bh, "管理画面", "登録・割当\n稼働状況");
  box(s, "構成_AI", MX + 3.5, by + bh + 0.4, 2.4, 1.0, "割当 AI", "実績から学習", { fill: SOFT, line: ACCENT });
  s.addShape(pres.shapes.LINE, { objectName: "構成_AI線", x: MX + 4.7, y: by + bh, w: 0, h: 0.4, line: { color: ACCENT, width: 2.5 } });

  s.addText([
    { text: "すべての操作は社員証のタッチから", options: { bold: true, fontSize: 18, color: NAVY, breakLine: true } },
    { text: "1 回のタッチで機材のロック・作業の開始／完了の報告・所要時間の記録まで済む", options: { fontSize: 16, bullet: { indent: 16 }, paraSpaceBefore: 6, breakLine: true } },
    { text: "遠隔予約はあえて作らない → 画面と現場の状態が常に一致する", options: { fontSize: 16, bullet: { indent: 16 }, paraSpaceBefore: 6, breakLine: true } },
    { text: "部品は安価な汎用品（試作機：Raspberry Pi＋NFC リーダー＋小型画面）", options: { fontSize: 16, bullet: { indent: 16 }, paraSpaceBefore: 6 } },
  ], { ...T, objectName: "構成_説明", x: MX, y: 5.05, w: 8.8, h: 1.75, valign: "top", margin: 0, color: INK });

  // 右：試作機の写真（526×463）
  const pw = 2.95, ph = pw * 463 / 526, px = W - MX - pw;
  s.addImage({ path: IMG("module.png"), x: px, y: by, w: pw, h: ph, objectName: "構成_写真" });
  s.addText("試作した機材モジュール", { ...T, objectName: "構成_写真説明", x: px, y: by + ph + 0.1, w: pw, h: 0.35,
    fontSize: 12, color: SUB, align: "center", margin: 0 });
  note(s, "【1 分】\n" +
    "システムの概要です。構成は三つです。機材ごとに付けるモジュール、サーバ、管理者が使うブラウザの管理画面です。\n" +
    "モジュールとサーバは MQTT という軽い通信でつながっていて、タッチすると即座にサーバに届きます。サーバの中の割当 AI が、誰にどの仕事を任せるかを決めます。\n" +
    "すべての操作は社員証のタッチから始まります。1 回のタッチで、機材のロック、作業の開始や完了の報告、所要時間の記録までが済みます。あえて遠隔予約は作っていないので、画面上の状態と現場の実態がずれません。\n" +
    "右の写真が試作したモジュールです。NFC リーダー、画面、ボタンという安価な汎用部品で作れます。");
}

// ================================================================ 5. システムの概要：管理画面
{
  const s = page(3);
  title(s, "管理者はブラウザで状況を一目で把握");
  const iw = (CW - 0.4) / 2, ih = iw * 400 / 1120, iy = 1.7;
  [["screen_dashboard.png", "ダッシュボード：機材ごとの空き・使用中・担当者"],
    ["screen_tasks.png", "タスク一覧：各タスクの「AI 割当」ボタンと根拠の表示"]].forEach(([f, cap], i) => {
    const x = MX + i * (iw + 0.4);
    s.addImage({ path: IMG(f), x, y: iy, w: iw, h: ih, objectName: `画面${i + 1}`, line: { color: GRAY, width: 1 } });
    s.addShape(pres.shapes.RECTANGLE, { objectName: `画面${i + 1}_枠`, x, y: iy, w: iw, h: ih, fill: { type: "none" }, line: { color: GRAY, width: 1 } });
    s.addText(cap, { ...T, objectName: `画面${i + 1}_説明`, x, y: iy + ih + 0.1, w: iw, h: 0.35, fontSize: 12, color: SUB, margin: 0 });
  });
  const items = [
    ["タスク登録", "重要度・難易度・期限・数量を入力"],
    ["作業者・機材の登録", "社員証・モジュールはタッチで登録"],
    ["稼働状況", "通信が切れたモジュールも分かる"],
  ];
  const gw = 0.35, cw = (CW - gw * 2) / 3, cy = 4.4, ch = 1.6;
  items.forEach(([h, b], i) => card(s, `管理機能${i + 1}`, MX + i * (cw + gw), cy, cw, ch, h, b));
  note(s, "【1 分】\n" +
    "管理者はブラウザで管理画面を開きます。左のダッシュボードで、どの機材が空いていて、誰が何をしているかが一目で分かります。右のタスク一覧では、各タスクの「AI 割当」ボタンを押すと、誰に任せるのがよいかと、その理由が表示されます。\n" +
    "新しい社員証やモジュールは、タッチするだけで管理画面に現れ、そこで名前を付けて登録できます。");
}

// ================================================================ 6. メリット：作業者
{
  const s = page(4);
  title(s, "導入のメリット ①　現場の作業者");
  const items = [
    ["報告に行かなくてよい", "開始・完了はタッチで自動的に記録。管理者のところへ報告しに行く必要がない"],
    ["次の仕事を聞かなくてよい", "タッチした瞬間にその場でやるべき仕事が出る。指示待ちの時間がなくなる"],
    ["機材を探して歩かなくてよい", "より重要な仕事がある機材へはシステムが案内してくれる"],
    ["向いた仕事・新しい仕事が回る", "向いている仕事を任されやすく、新人にも経験を積む機会が回る"],
  ];
  const gw = 0.35, cw = (CW - gw) / 2, ch = 1.9;
  items.forEach(([h, b], i) => {
    const x = MX + (i % 2) * (cw + gw), y = BODY_Y + 0.2 + Math.floor(i / 2) * (ch + 0.35);
    card(s, `作業者メリット${i + 1}`, x, y, cw, ch, "", []);
    badge(s, `作業者メリット${i + 1}_印`, x + 0.3, y + 0.35, 0.55, "✓");
    s.addText([
      { text: h, options: { bold: true, fontSize: 21, color: NAVY, breakLine: true } },
      { text: b, options: { fontSize: 16, color: INK, paraSpaceBefore: 8 } },
    ], { ...T, objectName: `作業者メリット${i + 1}_文字`, x: x + 1.1, y: y + 0.3, w: cw - 1.4, h: ch - 0.5, valign: "top", margin: 0 });
  });
  note(s, "【1 分】\n" +
    "ここからはシステム導入のメリットです。まず現場の作業者にとって。\n" +
    "一つ目、開始や完了の報告に管理者のところへ行く必要がなくなります。二つ目、次に何をやればいいか聞きに行かなくても、タッチすれば出てきます。三つ目、より重要な仕事がある機材にはシステムが案内してくれるので、探して歩く必要がありません。四つ目、自分に向いた仕事が回ってきやすく、新人にも新しい仕事に挑戦する機会が回ってきます。");
}

// ================================================================ 7. メリット：管理者
{
  const s = page(4);
  title(s, "導入のメリット ②　管理者");
  const items = [
    ["今の状況がリアルタイムに分かる", "どの機材を誰が使い、何の仕事をしているかを画面で確認できる"],
    ["得意・不得意がデータで分かる", "作業ログから一人ひとりの向き・不向きが見え、教育や配置の参考になる"],
    ["割当を考える手間が減る", "「AI 割当」ボタンで候補が出る。決めた理由も表示されるので納得して任せられる"],
    ["機材をまとめて管理できる", "機材の登録・状態・通信の切断まで、一つの画面で把握できる"],
  ];
  // 左 2 列 × 2 段、カードは白地＋枠で作業者スライドと見分ける
  const gw = 0.35, cw = (CW - gw) / 2, ch = 1.9;
  items.forEach(([h, b], i) => {
    const x = MX + (i % 2) * (cw + gw), y = BODY_Y + 0.2 + Math.floor(i / 2) * (ch + 0.35);
    card(s, `管理者メリット${i + 1}`, x, y, cw, ch, "", [], { fill: WHITE, line: NAVY });
    badge(s, `管理者メリット${i + 1}_印`, x + 0.3, y + 0.35, 0.55, "✓", NAVY);
    s.addText([
      { text: h, options: { bold: true, fontSize: 21, color: NAVY, breakLine: true } },
      { text: b, options: { fontSize: 16, color: INK, paraSpaceBefore: 8 } },
    ], { ...T, objectName: `管理者メリット${i + 1}_文字`, x: x + 1.1, y: y + 0.3, w: cw - 1.4, h: ch - 0.5, valign: "top", margin: 0 });
  });
  note(s, "【1 分】\n" +
    "次に管理者にとってのメリットです。\n" +
    "誰がどの機材で何をしているかが、現場を見回らなくても画面で分かります。作業ログがたまるので、一人ひとりの得意・不得意がデータで見え、教育や配置の参考にできます。誰に任せるか迷ったときは「AI 割当」ボタンで候補と理由が出ます。そして、機材の状態や通信の切断まで一つの画面でまとめて管理できます。");
}

// ================================================================ 8. メリット：工場全体
{
  const s = page(4);
  title(s, "導入のメリット ③　工場全体");
  // 導入前／導入後の比較表（表なので PowerPoint 上で行の追加・削除がしやすい）
  const hdr = (t, fill) => ({ text: t, options: { bold: true, color: WHITE, fill: { color: fill }, fontSize: 18, align: "center", valign: "middle" } });
  const lab = (t) => ({ text: t, options: { bold: true, color: NAVY, fill: { color: PAPER }, fontSize: 17, valign: "middle" } });
  const cell = (t, strong) => ({ text: t, options: { color: strong ? NAVY : INK, bold: !!strong, fontSize: 16, valign: "middle" } });
  const rows = [
    [hdr("", NAVY), hdr("これまで", GRAY), hdr("Gemmba 導入後", ACCENT)],
    [lab("機材の予約"), cell("予約だけして来ない「ゴースト予約」"), cell("遠隔予約がないので起きない", true)],
    [lab("仕事の割り振り"), cell("ベテランに集中・急ぎの仕事を忘れる"), cell("負荷を見て分散。重要な仕事から提示", true)],
    [lab("全体の作業時間"), cell("一部の人が終わるまで全体が待つ"), cell("シミュレーションで 27〜60% 短縮", true)],
    [lab("技能の継承"), cell("得意・不得意が勘と記憶頼み"), cell("作業ログとして記録が残る", true)],
    [lab("導入・運用"), cell("専任の IT 担当者が必要になりがち"), cell("タッチだけ。既存の機材に後付け", true)],
  ];
  s.addTable(rows, {
    objectName: "比較表", x: MX, y: BODY_Y + 0.1, w: CW, colW: [2.4, 4.4, CW - 6.8], rowH: 0.78,
    fontFace: FONT, border: { type: "solid", color: "D5D9E0", pt: 1 }, margin: [0, 0.15, 0, 0.15],
  });
  note(s, "【1 分】\n" +
    "最後に工場全体で見たメリットを、これまでと比べてまとめました。\n" +
    "遠隔予約をなくしたのでゴースト予約は起きません。仕事は負荷を見て分散されるので、ベテラン一人に頼りきりになりません。シミュレーションでは全体の作業時間が最大 60% 短くなりました。作業ログが残るので、誰が何を得意かという情報が勘や記憶ではなくデータとして残ります。そして、操作はタッチだけで、既存の機材にモジュールを後付けするだけなので、IT 担当者のいない工場でも導入しやすいです。");
}

// ================================================================ 9. ユースケース
{
  const s = page(5);
  title(s, "ユースケース：ある日の現場の流れ");
  // 表なので、PowerPoint 上で行の追加・削除や文言の変更がしやすい
  const who = (t) => ({ text: t, options: { bold: true, color: WHITE, fill: { color: t === "管理者" ? NAVY : ACCENT }, align: "center", valign: "middle", fontSize: 15 } });
  const num = (t) => ({ text: t, options: { bold: true, color: NAVY, align: "center", valign: "middle", fontSize: 18 } });
  const act = (t) => ({ text: t, options: { color: INK, valign: "middle", fontSize: 16, bold: true } });
  const sys = (t) => ({ text: t, options: { color: INK, valign: "middle", fontSize: 15, fill: { color: PAPER } } });
  const hdr = (t) => ({ text: t, options: { bold: true, color: SUB, valign: "middle", fontSize: 14 } });
  const rows = [
    [hdr(""), hdr("誰が"), hdr("すること"), hdr("Gemmba の動き")],
    [num("1"), who("管理者"), act("朝、今日のタスクを管理画面で登録"), sys("重要度・難易度・期限・数量を保存")],
    [num("2"), who("作業者"), act("空いている旋盤で社員証をタッチ"), sys("割当 AI がこの人に向く仕事を選び、画面に表示")],
    [num("3"), who("作業者"), act("ボタンで承認して作業を始める"), sys("旋盤をロック。管理画面に「使用中・担当者」を表示")],
    [num("4"), who("作業者"), act("別の機材への移動を提案される"), sys("本人宛のより重要な仕事が他の機材にあれば案内")],
    [num("5"), who("作業者"), act("終わったら再タッチし、難しさを答える"), sys("所要時間を記録し、AI が得意・不得意を学習")],
    [num("6"), who("管理者"), act("画面で進捗と実績を確認"), sys("偏りや遅れがあれば「AI 割当」で割り振り直し")],
  ];
  s.addTable(rows, {
    objectName: "ユースケース表", x: MX, y: BODY_Y + 0.05, w: CW, colW: [0.6, 1.3, 4.6, CW - 6.5], rowH: [0.45, 0.66, 0.66, 0.66, 0.66, 0.66, 0.66],
    fontFace: FONT, border: { type: "solid", color: "FFFFFF", pt: 3 }, margin: [0, 0.15, 0, 0.15],
  });
  s.addText("作業者は機材の前で「タッチ」と「ボタン」だけ。報告・確認・次の仕事探しのために歩き回らなくてよい", {
    ...T, objectName: "ユースケース_まとめ", x: MX, y: 6.3, w: CW, h: 0.45, fontSize: 18, bold: true, color: ACCENT, margin: 0 });
  note(s, "【1 分 30 秒】\n" +
    "実際の一日の流れで使い方を説明します。\n" +
    "①朝、管理者が今日のタスクを登録します。②作業者が空いている旋盤で社員証をタッチすると、AI がその人に向いた仕事を選んで画面に出します。③ボタンで承認すると作業開始で、旋盤がロックされ、管理画面にも使用中と表示されます。④もし別の機材にその人宛のもっと重要な仕事があれば、移動を提案します。⑤終わったらもう一度タッチし、難しさを 3 段階で答えます。所要時間が記録され、AI の学習に使われます。⑥管理者は画面で進捗を確認し、偏りがあれば AI 割当で割り振り直せます。\n" +
    "（デモがあればここで実演）");
}

// ================================================================ 10. AI の妥当性：仕組みと根拠
{
  const s = page(6);
  title(s, "AI の仕組み：実績から学び、割当の理由を示す");
  // 左：特徴
  const lw = 5.3;
  const feats = [
    ["隠れた得意分野を見つける", "経験年数だけでなく、実際の作業時間から一人ひとりの向き・不向きを学ぶ"],
    ["新人にも機会を回す", "実績の少ない人にも試しに任せ、得意かどうかを確かめる"],
    ["一人に仕事を集中させない", "手持ちの仕事が多い人は点数を下げ、次に向く人へ回す"],
  ];
  feats.forEach(([h, b], i) => card(s, `AI特徴${i + 1}`, MX, BODY_Y + 0.1 + i * 1.6, lw, 1.4, h, b, { headSize: 18, bodySize: 15 }));
  // 右：根拠の表示例（poster.tex と同じ、評価シミュレーションの 52 ラウンド目に実際に出た値）
  const rx = MX + lw + 0.5, rw = W - MX - rx;
  s.addText("割当の根拠の表示例：溶接・難易度 3 のタスク", { ...T, objectName: "根拠_見出し", x: rx, y: BODY_Y + 0.1, w: rw, h: 0.4, fontSize: 18, bold: true, color: NAVY, margin: 0 });
  const h = (t) => ({ text: t, options: { bold: true, color: WHITE, fill: { color: NAVY }, align: "center", valign: "middle", fontSize: 14 } });
  const r = (cells, hl) => cells.map((t, j) => ({ text: t, options: {
    color: hl ? ACCENT : INK, bold: !!hl, fill: { color: hl ? SOFT : WHITE }, align: j === 0 ? "left" : "right", valign: "middle", fontSize: 14 } }));
  const rows = [
    [h("作業者"), h("期待値"), h("手持ち [分]"), h("点数")],
    r(["勤続 8 年", "1.58", "0", "1.61"], true),
    r(["勤続 25 年", "0.78", "0", "1.14"]),
    r(["勤続 30 年", "2.31", "22.9", "0.97"]),
    r(["勤続 3 年", "0.64", "0", "0.47"]),
    r(["勤続 1 年", "0.19", "0", "0.18"]),
  ];
  s.addTable(rows, { objectName: "根拠_表", x: rx, y: BODY_Y + 0.6, w: rw, colW: [rw - 4.2, 1.4, 1.4, 1.4], rowH: 0.42,
    fontFace: FONT, border: { type: "solid", color: "D5D9E0", pt: 1 }, margin: [0, 0.12, 0, 0.12] });
  s.addText([
    { text: "期待値が最も高いのは勤続 30 年の人。ただし手持ちが 23 分あるため点数は 0.97", options: { bullet: { indent: 16 }, breakLine: true } },
    { text: "手持ちがなく、溶接が 2 番目に得意な勤続 8 年の人に任せる", options: { bullet: { indent: 16 }, paraSpaceBefore: 6, breakLine: true } },
    { text: "→ 「なぜこの人か」を管理者が確認できる", options: { bold: true, color: ACCENT, paraSpaceBefore: 6 } },
  ], { ...T, objectName: "根拠_説明", x: rx, y: BODY_Y + 3.3, w: rw, h: 1.4, fontSize: 15, color: INK, valign: "top", margin: 0 });
  s.addText("手法：文脈付きバンディット（ベイズ線形回帰＋Thompson Sampling）。表は評価シミュレーションで実際に出た根拠（抜粋）", {
    ...T, objectName: "AI_手法", x: MX, y: 6.5, w: CW, h: 0.35, fontSize: 12, color: SUB, margin: 0 });
  note(s, "【1 分 30 秒】\n" +
    "ここからは AI の妥当性です。まず仕組みです。割当 AI は、作業ログ、つまり実際にかかった時間と体感の難しさから、作業者一人ひとりの得意・不得意を学習します。\n" +
    "特徴は三つで、経験年数だけでは分からない隠れた得意分野を見つけること、実績の少ない新人にも試しに仕事を回すこと、手持ちの多い人には回さず一人に集中させないことです。\n" +
    "右の表は、AI が実際に出した割当の根拠です。期待値が一番高いのは勤続 30 年の人ですが、手持ちの仕事が 23 分あるので点数が下がり、手持ちがなく溶接が 2 番目に得意な勤続 8 年の人が選ばれました。このように、なぜこの人なのかを管理者が確認できるので、AI の判断をうのみにせず、納得して使えます。\n" +
    "技術的には文脈付きバンディットという手法で、ベイズ線形回帰と Thompson Sampling を組み合わせています。");
}

// ================================================================ 11. AI の妥当性：評価
{
  const s = page(6);
  title(s, "評価：シミュレーションで他の割当方法と比較");
  // resume/poster_summary.csv の span（1 ラウンドの完了時間 [分]）
  const labels = ["経験年数が最長の人", "ランダム", "順番に回す", "提案（Gemmba）", "真の適性を知る割当（理想）"];
  const vals = [63.7, 44.4, 34.7, 25.4, 23.6];
  s.addChart(pres.charts.BAR, [{ name: "完了時間 [分]", labels, values: vals }], {
    objectName: "評価_グラフ", x: MX, y: 1.6, w: 7.6, h: 4.5, barDir: "bar",
    catAxisOrientation: "maxMin", valAxisMinVal: 0, valAxisMaxVal: 70, valAxisMajorUnit: 10,
    chartColors: ["8A8984", "8A8984", "8A8984", "2A78D6", "1F3A5F"],
    invertedColors: ["8A8984", "8A8984", "8A8984", "2A78D6", "1F3A5F"],
    barGapWidthPct: 60,
    showValue: true, dataLabelPosition: "outEnd", dataLabelFormatCode: "0.0", dataLabelFontSize: 14,
    dataLabelColor: "1B1B1A", dataLabelFontFace: "+mn-lt",
    catAxisLabelFontSize: 14, catAxisLabelColor: "1B1B1A", catAxisLabelFontFace: "+mn-lt",
    valAxisLabelFontSize: 12, valAxisLabelColor: "52514E", valAxisLabelFontFace: "+mn-lt",
    valAxisHidden: true, valGridLine: { style: "none" }, catGridLine: { style: "none" },
    showTitle: true, title: "全員の作業が終わるまでの時間 [分]（短いほど良い）", titleFontSize: 14, titleColor: "52514E", titleFontFace: "+mn-lt",
    showLegend: false,
  });
  s.addText("作業者 6 名・仕事 6 件 × 150 ラウンド × 20 試行の平均。学習後（51 ラウンド目以降）で測定", {
    ...T, objectName: "評価_条件", x: MX, y: 6.2, w: 7.6, h: 0.5, fontSize: 12, color: SUB, margin: 0 });
  // 右：数字
  const rx = MX + 8.0, rw = W - MX - rx;
  const stats = [
    ["−60%", "経験年数で選ぶ割当と比べた完了時間", ACCENT],
    ["−27%", "順番に回す割当と比べた完了時間", ACCENT],
    ["8%", "理想（真の適性を知る割当）との差", NAVY],
  ];
  stats.forEach(([v, l, c], i) => {
    const y = 1.6 + i * 1.6;
    s.addText([
      { text: v, options: { fontSize: 44, bold: true, color: c, breakLine: true } },
      { text: l, options: { fontSize: 15, color: SUB } },
    ], { ...T, objectName: `評価_数字${i + 1}`, x: rx, y, w: rw, h: 1.4, valign: "top", margin: 0 });
  });
  note(s, "【1 分 30 秒】\n" +
    "次に、AI の割当が本当に妥当かをシミュレーションで確かめました。作業者 6 人、勤続 1 年から 30 年で、各人に隠れた得意分野があるという設定です。AI には各人の本当の能力は見せていません。\n" +
    "グラフは、全員の作業が終わるまでの時間です。経験年数が一番長い人に任せる方式は、ベテラン 1 人に仕事が集中して一番遅くなりました。私たちの方式は 25.4 分で、経験年数方式より 60%、順番に回す方式より 27% 短くなりました。本当の能力を知っている理想の割当との差は 8% です。\n" +
    "※ 実際の工場のデータではなく、シミュレーションの結果である点に注意。");
}

// ================================================================ 12. まとめと今後の展望
{
  const s = page(7);
  title(s, "まとめと今後の展望");
  card(s, "まとめ_要点", MX, BODY_Y + 0.1, CW, 1.6, "",
    [], { fill: SOFT, line: ACCENT });
  s.addText([
    { text: "社員証をタッチするだけで、人と仕事を AI が結び付ける", options: { bold: true, fontSize: 24, color: NAVY, breakLine: true } },
    { text: "作業者は報告・確認・探す手間から解放され、管理者は状況と適性をデータで把握できる", options: { fontSize: 18, color: INK, paraSpaceBefore: 8 } },
  ], { ...T, objectName: "まとめ_要点_文字", x: MX + 0.35, y: BODY_Y + 0.1, w: CW - 0.7, h: 1.6, valign: "middle", margin: 0 });
  const gw = 0.35, cw = (CW - gw) / 2, cy = 3.65, ch = 2.2;
  card(s, "まとめ_課題", MX, cy, cw, ch, "現時点の課題",
    ["評価はシミュレーションのみ。実際の現場では未検証", "作業者の上達（能力の変化）をまだ考慮していない", "ログが増えると計算が重くなる"], { bullet: true });
  card(s, "まとめ_展望", MX + cw + gw, cy, cw, ch, "今後の展望",
    ["実際の工場で作業ログを集めて評価する", "古いログの重みを下げ、上達を反映する", "物理ボタンによる承認など、モジュールを完成させる"], { bullet: true });
  note(s, "【1 分】\n" +
    "まとめです。Gemmba は、社員証をタッチするだけで、人と仕事を AI が結び付けるシステムです。作業者は報告や確認、探す手間から解放され、管理者は現場の状況と一人ひとりの適性をデータで把握できます。\n" +
    "課題として、評価はまだシミュレーションのみで、実際の現場では検証できていません。今後は実際の工場で作業ログを集めて評価し、作業者の上達も反映できるようにしていきます。\n" +
    "ご清聴ありがとうございました。");
}

// ---------------------------------------------------------------- 書き出し
(async () => {
  await pres.writeFile({ fileName: OUT });
  // pptxgenjs はテーマの配色を書けないので、theme*.xml を直接書き換える。
  // 和文フォント（東アジア言語用）も同じフォントにする
  const zip = await JSZip.loadAsync(fs.readFileSync(OUT));
  const scheme = `<a:clrScheme name="${THEME.name}">` +
    ["dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]
      .map((k) => `<a:${k}><a:srgbClr val="${THEME.colors[k]}"/></a:${k}>`).join("") + "</a:clrScheme>";
  for (const f of Object.keys(zip.files).filter((f) => /^ppt\/theme\/theme\d+\.xml$/.test(f))) {
    let xml = await zip.file(f).async("string");
    xml = xml.replace(/<a:clrScheme[\s\S]*?<\/a:clrScheme>/, scheme);
    xml = xml.replace(/<a:ea typeface="[^"]*"\s*\/>/g, `<a:ea typeface="${FONT}"/>`);
    xml = xml.replace(/<a:font script="Jpan" typeface="[^"]*"\s*\/>/g, `<a:font script="Jpan" typeface="${FONT}"/>`);
    zip.file(f, xml);
  }
  fs.writeFileSync(OUT, await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE" }));
  console.log("wrote", OUT);
})();

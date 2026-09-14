---
name: Gemmba
description: 工場の安全掲示板そのものとして構成した、共有機材の使用状況管理コンソール
colors:
  paper: "#F6F3EA"
  plate: "#FFFFFF"
  ink: "#232323"
  ink-soft: "#6B6558"
  ink-faint: "#6D6757"
  line: "color-mix(in srgb, #232323 18%, transparent)"
  blue: "#00BFFF"
  blue-deep: "#0093C4"
  blue-tint: "#E3F7FF"
  yellow: "#FFDB4F"
  yellow-deep: "#E0BD28"
  yellow-tint: "#FFF6DA"
  good: "#1C7A45"
  good-tint: "#E3F4E9"
  bad: "#B3261E"
  bad-tint: "#FBEAE9"
typography:
  display:
    fontFamily: "Noto Sans JP, Hiragino Kaku Gothic ProN, Yu Gothic UI, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.35
  headline:
    fontFamily: "Noto Sans JP, Hiragino Kaku Gothic ProN, Yu Gothic UI, sans-serif"
    fontSize: "19px"
    fontWeight: 700
    lineHeight: 1.35
  body:
    fontFamily: "Noto Sans JP, Hiragino Kaku Gothic ProN, Yu Gothic UI, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.7
  label:
    fontFamily: "Noto Sans JP, Hiragino Kaku Gothic ProN, Yu Gothic UI, sans-serif"
    fontSize: "12.5px"
    fontWeight: 700
  mono:
    fontFamily: "JetBrains Mono, SF Mono, Consolas, monospace"
    fontSize: "11.5px"
    fontWeight: 400
rounded:
  sm: "4px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  xxl: "40px"
components:
  button-primary:
    backgroundColor: "{colors.blue}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "10px 18px"
  button-primary-hover:
    backgroundColor: "{colors.blue-deep}"
  button-secondary:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "10px 18px"
  button-ai:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "10px 18px"
  badge-idle:
    backgroundColor: "{colors.good-tint}"
    textColor: "{colors.good}"
    rounded: "{rounded.pill}"
    padding: "4px 11px"
  badge-inuse:
    backgroundColor: "{colors.blue-tint}"
    textColor: "{colors.blue-deep}"
    rounded: "{rounded.pill}"
    padding: "4px 11px"
  card:
    backgroundColor: "{colors.plate}"
    rounded: "{rounded.sm}"
    padding: "{spacing.md}"
  input:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
---

# Design System: Gemmba

## Overview

**Creative North Star: "The Factory Nameplate Board"**

Gemmba の管理コンソールは、工場の壁に留められた安全掲示板・機材銘板そのものとしてUIを組み立てる。ソフトウェアらしい柔らかいSaaSカード（大きな角丸＋ドロップシャドウ）の擬態はしない。太い炭黒の枠線と四隅のリベット（留め具）ドットという、現場に実在する掲示物の文法をそのままコンポーネントに翻訳している。

背景は温白の掲示板下地（paper）で、その上に白い銘板（plate）が炭黒の2px枠とリベットドットを伴って載る。キーカラーは2色のみ：`#00BFFF`（稼働・情報を示す信号色）と`#FFDB4F`（注意・要対応を示す信号色）。黄色は面を塗るための色ではなく、枠線としてのみ使う（例：AI提案ボタンの黄枠）。【2026-09-14 追記3: ユーザー指示により、黒×黄の縞模様（ハザードストライプ）は全廃止した。至急タスクの区別は赤いバッジ色のみで示し、AIボタンの区別は黄色い枠線のみで示す】状態は色だけに頼らず、色＋アイコン形状の二重で示す（空き＝輪郭の丸、使用中＝塗りつぶした再生形）。

一覧・フォームは個々を独立した銘板（カード）にしない。分裂して見えるため、機材一覧・タスク一覧・API一覧は「一枚の掲示板」として外枠（2px枠＋四隅リベット＋柔らかい影）を1つだけ持たせ、中の項目は1pxの罫線だけで区切る（`.sheet` / `.sheet-row`）。登録フォームはさらに軽く、箱で囲わず下の罫線1本で次のセクションと分ける（`.form-panel`）。個々の銘板の完全なセット（角丸＋リベット＋影）は、意図的に浮かせて見せたいモーダルダイアログにのみ残す。

主要ユーザーは、専任のIT担当者がいない中小工場の管理者と、NFCタッチのみで機材を操作する現場作業者。画面名・状態名は平易な言葉（使用状況／仕事／人／機械、空き／使用中）にとどめ、モジュールIDやデバイスIDのような専門情報は「詳しい設定」の折りたたみに隔離する。

**Key Characteristics:**
- 一覧・フォームは個々をカード化せず、「一枚の掲示板」として1つの外枠の中に罫線区切りの行を並べる（`.sheet` / `.sheet-row`）
- 炭黒の太枠と四隅のリベットドットは、個々の項目ではなくシート全体・モーダルにのみ持たせる
- キーカラーは稼働／注意の2色に限定し、黄色は枠線としてのみ使う（縞模様は使わない）
- 状態は色＋アイコン形状の二重表示（色弱でも区別できるように）
- 専門用語・技術情報は既定で隠し、`<details>`の折りたたみでのみ開示する
- 影は柔らかいオフセット＋ぼかしのみ。ゼロブラーの硬い影は使わない（ネオブルータリズムではない）

## Colors

温白の掲示板地に炭黒の枠線、稼働・注意の2色の信号色だけを足した、ニュートラル優位の配色。

### Primary
- **Andon Blue** (`#00BFFF`): 「稼働中・情報」を示す信号色。主要ボタン、アクティブなナビゲーション項目、機材の「使用中」バッジ・状態灯に使う。単体では白背景に対してコントラストが低いため、この色の上の文字・アイコンは必ず炭黒（Ink）にする。白文字は使わない。

### Secondary
- **Hazard Amber** (`#FFDB4F`): 「注意・要対応」を示す信号色。AI割当ボタンの枠線にのみ使う。面いっぱいに塗ることはしない —この色が塗り面に出た瞬間、意味が「注意」から薄れる。縞模様（ハザードストライプ）としては使わない（ユーザー指示により廃止）。

### Neutral
- **Bulletin Paper** (`#F6F3EA`): ページ全体の下地。掲示板の紙質を思わせる温白。
- **Nameplate White** (`#FFFFFF`): カード・入力欄などの前面（銘板）の地。Paperよりわずかに白く、層を示す。
- **Nameplate Ink** (`#232323`): 主要な文字色、太枠線、サイドバー背景。純黒ではなく温かみのある炭黒。
- **Soft Ink** (`#6B6558`): 補助的な本文・ラベル文字（見出し以外の説明文など）。
- **Faint Ink** (`#6D6757`): プレースホルダー・タイムスタンプなど、最も軽い実コンテンツの文字。Paper上で約5:1のコントラストを確保している（WCAG AA相当）。

**Status colors**（ブランドカラーとは別軸。稼働状況の意味色）:
- **Available Green** (`#1C7A45` / 背景 `#E3F4E9`): 機材が「空き」であることを示す。
- **Stopped Red** (`#B3261E` / 背景 `#FBEAE9`): エラー表示・フラッシュメッセージに使う。

### Named Rules
**The Ink-On-Signal Rule.** Andon Blue・Hazard Amber・Available Green の上に乗る文字とアイコンは、常に Nameplate Ink（炭黒）にする。白文字はこれらの信号色の上では読みにくく、使わない。

**The Local Amber Rule.** Hazard Amber は枠線以外で面を塗らない。ボタン全体や背景全体を黄色で塗ることは、「注意」という意味を薄めるため禁止する。縞模様（ハザードストライプ）はユーザー指示により廃止済みで、復活させない。

## Typography

**Display / Body Font:** Noto Sans JP（フォールバック: Hiragino Kaku Gothic ProN, Yu Gothic UI, sans-serif）
**Label/Mono Font:** JetBrains Mono（モジュールID・タイムスタンプなど、実際の技術データにのみ使用）

**Character:** 単一の日本語ゴシック体（Noto Sans JP）を、太さの違いだけで見出し・本文・ラベルに使い分ける。装飾的な別書体は入れず、掲示物らしい実直な印象を保つ。

### Hierarchy
- **Display**（700, 30px, line-height 1.35）: ページ見出し（`<h1>`）。
- **Headline**（700, 19px, line-height 1.35）: セクション見出し（`<h3 class="section-title">`）。
- **Body**（400, 15px, line-height 1.7）: 本文全般。
- **Label**（700, 12.5px）: フォームラベル、タイル見出し、バッジ文字。
- **Numeral**（900, 34px, tabular-nums）: 状況サマリー帯の大きな件数表示にのみ使う特別なウェイト。

### Named Rules
**The No-Caps Rule.** 日本語ラベルは全角の大文字化ができないため、状態の強調は太字・枠付きバッジで行い、英字ラベルであっても全角/半角を問わずすべて大文字（caps）にはしない。

## Layout

固定幅サイドバー（256px、掲示板の案内パネル）＋可変メインの2カラム構成。ブレークポイントは900px：これを下回るとサイドバーがメイン上部に積み重なる1カラムになる。余白は4px刻みのスケール（`--space-1`〜`--space-9`：4/8/12/16/20/24/32/40/56px）。カード間の標準ガターは16px、セクション間は32px。横に長い表（機材一覧など）は独自にスクロールし、900px以下では「横にスクロールできます」という一言のヒントを表示する。

## Elevation & Depth

フラットな面に、柔らかくぼかした落ち影をわずかに添えるだけ。ゼロブラーの硬いオフセット影（ネオブルータリズムの記法）は使わない — この世界は「安全掲示板」であって、ネオブルータリズムの世界ではないため。深さの大部分は影ではなく、太い炭黒の枠線と四隅のリベットドットで表現する。

### Shadow Vocabulary
- **Card** (`box-shadow: 0 2px 6px color-mix(in srgb, var(--ink) 14%, transparent)`): すべてのカード・銘板に共通の、壁からわずかに浮いたような柔らかい影。
- **Modal** (`box-shadow: 0 16px 40px color-mix(in srgb, var(--ink) 24%, transparent)`): ポップアップダイアログのみ、より強く柔らかい影。

### Named Rules
**The No Hard Shadow Rule.** `box-shadow` はオフセットとぼかしの両方を持つ柔らかい影のみを使う。ゼロブラーの硬い影（`0 2px 0` のような記法）は、この世界では装飾の借用になるため使わない。

## Shapes

角は4px程度の最小限の面取りのみ（`--radius: 4px`）。SaaSにありがちな大きな丸みは避け、機材銘板・掲示物の面取り角を模す。バッジ・タグ類のみ完全な丸角（`--radius-pill: 999px`）にする。枠線は太め（2px、`--line-strong`）でシート・モーダル・入力欄・表の主要な区切りに使い、シート内部の項目どうしはより弱い1pxの罫線（`--line`）だけで区切る（個々を太枠で囲わない）。リベットドット（四隅の小さな円）は、シートまたはモーダルの外枠にひと組だけ背景画像として配置し、「壁に留められた掲示板」であることを示す。個々の行に対しては付けない。縞模様（ハザードストライプ）は使わない（ユーザー指示により廃止済み）。注意を要する状態は、色（赤・黄枠）とアイコン形状だけで示す。

## Components

### Buttons
- **Shape:** 角は4px、枠線は2px。
- **Primary:** 背景 Andon Blue（`#00BFFF`）、文字 Nameplate Ink、枠線 Ink。主要な送信操作（登録・紐付けなど）。
- **Secondary:** 背景 Nameplate White、文字・枠線 Ink。更新・保存など二次的な操作。
- **AI（WariAthena）:** 背景 White、枠線 Yellow-deep、文字 Ink。黄色い枠線だけで「AIによる提案操作」であることを示す。縞模様のバッジは付けない（ユーザー指示により廃止）。全面を黄色で塗らない（The Local Amber Rule）。
- **Danger:** 背景 White、文字・枠線 Stopped Red。削除など破壊的操作の確認導線に使う。
- **Hover / Active:** 背景が一段階濃い/薄いトーンに変わり、押下時は1px下に沈む（`translateY(1px)`）。

### Sheets / Lists（signature component）
一覧・登録済みデータは、個々を独立したカードにせず「一枚の掲示板」として続ける。機材一覧・タスク一覧・API一覧・表はすべてこのパターン。
- **Corner Style:** 4px（外枠のみ）。
- **Background:** Nameplate White、Paper地の上に配置。
- **Shadow Strategy:** Elevation & Depth の Card 影を、外枠1つにだけ使用。
- **Border:** 外枠のみ2px、Nameplate Ink。内部の行は1pxの罫線（`--line`）で区切り、最後の行には罫線を付けない。
- **Signature detail:** 四隅のリベットドット（`radial-gradient`による背景画像）を外枠にひと組だけ配置。
- **Internal Padding:** 各行 16–20px（`--space-4`〜`--space-5`）。

### Forms
登録フォームは箱で囲わない。見出し＋フィールドを直接ページに流し込み、下端に2pxの罫線を1本引いて次のセクションと分ける（`.form-panel`）。未登録タグ・未登録モジュールの通知だけは、黄色い背景タイント＋2pxの黄枠で目立たせる（`.callout`）。リベットや影は付けない — 色だけで注意を引く。

### Cards / Containers（モーダルのみ）
浮かせて見せたいモーダルダイアログにだけ、個々の銘板の完全なセットを残す。
- **Corner Style:** 4px（面取り角）。
- **Background:** Nameplate White。
- **Shadow Strategy:** Elevation & Depth の Modal 影を使用。
- **Border:** なし（背景の暗いオーバーレイの上に浮かせる）。
- **Internal Padding:** 24px（`--space-6`）。

### Inputs / Fields
- **Style:** 白背景、2px の半透明枠線（`--line`）、角4px。
- **Focus:** 枠線が Ink に変わり、外側に Andon Blue の4pxリング（`--focus-ring`）が付く。
- **Disabled:** 背景が Paper 色になり、文字は Faint Ink。

### Status Badges（signature component）
機材・タスクの状態を示す、色＋アイコン形状の二重表示バッジ。空きは輪郭だけの丸アイコン＋緑、使用中は塗りつぶした再生形アイコン＋青。優先度・接続状態にも同じ「色つきの縁取りピル＋小アイコン」の語彙を使い回す。単一のアイコンや色だけで状態を伝えることはしない。

### Navigation
サイドバーは Nameplate Ink の背景に、白文字のナビ項目を縦に並べる。アクティブな項目だけ Andon Blue の塗りつぶし背景＋Ink文字になり、ホバー時はわずかに白が透けた背景になる。各項目には24×24のアウトラインアイコンが付く。画面名は平易な短い言葉（使用状況／仕事／人／機械）にとどめ、正式名称はページ見出しにのみ表示する。

## Do's and Don'ts

### Do:
- **Do** 状態を色だけで伝えず、必ずアイコンの形（輪郭の丸 vs 塗りつぶした再生形）を併用する。
- **Do** Hazard Amber は枠線としてのみ使う（AI提案ボタンなど）。縞模様は使わない。
- **Do** モジュールID・デバイスID・タイムスタンプなど専門的な情報は `<details class="eq-tech">` / `.tech-toggle` の折りたたみに隔離し、既定では隠す。
- **Do** 一覧は `.sheet` / `.sheet-row` で「一枚の掲示板」として続け、外枠と四隅のリベットはシート全体に1組だけ持たせる。
- **Do** 影は必ずオフセットとぼかしの両方を持たせる。

### Don't:
- **Don't** Andon Blue・Hazard Amber・Available Green の上に白文字を置かない（コントラスト不足）。
- **Don't** Hazard Amber でボタンや背景全体を塗りつぶさない。
- **Don't** ゼロブラーの硬いオフセット影（ネオブルータリズム風）を使わない。
- **Don't** 日本語ラベルを模した全角の全角caps表現や、状態を示すための装飾的なグラデーションを使わない。
- **Don't** 機材の状態区分を増やさない（空き／使用中の2種類に固定する。要件が変わらない限り、停止・メンテ中などの中間状態を復活させない）。
- **Don't** 一覧やフォームを個々の独立したカード（角丸＋枠＋影＋リベットのフルセット）に戻さない。分裂して見える、というユーザーの明示的なフィードバックで廃止した。個々の銘板の完全なセットはモーダルダイアログにのみ許可する。
- **Don't** 黒×黄の縞模様（ハザードストライプ）を復活させない。ユーザー指示により全廃止した。注意を要する状態は、色（赤バッジ・黄枠）とアイコン形状だけで示す。

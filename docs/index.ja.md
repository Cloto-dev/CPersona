<!-- i18n-source: docs/index.md@blob:3e00402a359b0c9f92957d1226279cd7c88498bf -->

# CPersona ドキュメント

CPersona は [MCP](https://modelcontextprotocol.io/) サーバーです。Claude を
はじめとする MCP 対応エージェントに、**セッションをまたいで残る記憶**を与えます。

記憶はローカルの SQLite ファイル 1 つに保存されます。recall はそれを 3 通り
(vector・FTS5・keyword) で検索し、ランクまたは相対スコアで融合します。

サーバーは **LLM に一切依存しません**: 生成モデルを呼びません。ただし、これは
次の 2 つを意味しません。

- **常に無料とは限りません。** `EMBEDDING_MODE=api` では、埋め込みリクエストごとに
  課金されます (既定のエンドポイントは OpenAI)。自前の埋め込みサーバーを指す
  `http` モードでは課金されません。
- **どの環境でも同じとは限りません。** recall は品質ゲートが較正済みであれば
  決定論的です。較正はコーパスをランダムに標本して行うため、同じデータを持つ
  2 つの環境が別のゲートに落ち着くことがあります。

> **対象: CPersona {{ version_line }}。** このサイトが正式ドキュメントです。README や
> 同梱 skill の記述とこのサイトが食い違う場合はサイトが優先で、その食い違い自体が
> [報告に値するバグ](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
> です。
>
> **翻訳について**: 正本は英語版です。日本語版が古い場合や存在しないページは
> 英語版を参照してください (未翻訳ページは自動的に英語で表示されます)。

> **2.6 の pre-release へ移行するなら:** [2.5 から 2.6 への移行](https://cloto-dev.github.io/CPersona/2.6/ja/upgrading-to-2.6/)が、2.5.x のストアを一度に移行する手順をまとめています。

## 目的別ガイド { #where-to-go }

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **はじめに**

    ---

    インストールし、MCP クライアントに登録し、接続を検証します。

    [:octicons-arrow-right-24: はじめに](getting-started.md)

-   :material-sitemap:{ .lg .middle } **アーキテクチャ**

    ---

    記憶がどこに保存され、3 つの検索器がどう探し、融合 → ゲート → 反転の
    パイプラインが何をするか。図付きです。

    [:octicons-arrow-right-24: アーキテクチャ](architecture.md)

-   :material-toolbox:{ .lg .middle } **ツール一覧**

    ---

    全ツールを目的別にまとめています。名前から想像できない挙動をするツールは、
    その理由を説明する契約へリンクしています。

    [:octicons-arrow-right-24: ツール一覧](tools.md)

-   :material-handshake:{ .lg .middle } **挙動契約**

    ---

    依存してよい挙動です。ここを変えることは、好みの変更ではなくバグとして扱います。

    [:octicons-arrow-right-24: 挙動契約](behavior-contracts.md)

-   :material-cog:{ .lg .middle } **設定リファレンス**

    ---

    全環境変数と既定値、そして HTTP トランスポートが応答するための要件。

    [:octicons-arrow-right-24: 設定リファレンス](configuration.md)

-   :material-lifebuoy:{ .lg .middle } **運用 Runbook**

    ---

    バックアップ、劣化検知、recall を調整する順序、日本語コーパス、保守の周期。

    [:octicons-arrow-right-24: 運用 Runbook](operations.md)

-   :material-help-circle:{ .lg .middle } **FAQ**

    ---

    運用者からよく来る質問への短い回答。それぞれ詳細のあるページへリンクします。

    [:octicons-arrow-right-24: FAQ](faq.md)

-   :material-shield-check:{ .lg .middle } **品質保証**

    ---

    リリースがどう検査されるか: 監査ラウンド、バグ登録簿、構造ゲート、変異証明。

    [:octicons-arrow-right-24: 品質保証](quality-assurance.md)

</div>

## 設計ノートと標準 { #design-notes-and-standards }

ガイドの下には 2 種類のページがあります。それぞれ別の問いに答えます。

**プロジェクト標準** は、リリース・監査報告・生成されるポリシーブロックが
どうあるべきかを定めます。他のプロジェクトでも採用できます。

- [リリースライフサイクル標準](RELEASE_LIFECYCLE_STANDARD.md) — ティア定義
  (Stable / Current)、リスク駆動の pre-release ladder、サポート期間。本リポジトリが
  実際に運用している内容は
  [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) にあります。
- [SuperAuditor 標準](SUPERAUDITOR_STANDARD.md) — クライアントがサーバーから
  findings を取得する方法。深刻度の語彙と、上限の意味。何を検出すべきかについては
  意図的に何も定めていません。
- [ポリシーブロック標準](CLAUDE_MD_POLICY_STANDARD.md) — プロジェクトの skill が、
  エージェントが毎セッション読み込むファイル (`CLAUDE.md`、`AGENTS.md`、…) へ
  マーカー付きポリシーブロックを書き込む方法と、skill だけではブロックの存在を
  保証できない理由。

**これからの方向。** [ロードマップ](roadmap.md) は、各リリースラインが何のためにあり、
何を破ってよく、計画中の機能がどの実測された問題に答えるかを記録します。軸は 3 つ
(リリースライン・ランタイムとスケール・サポート tier) です。これは意図の記述であって
納期の約束ではありません。実際に出荷されたものはリリースノートと SUPPORT.md にあります。

2.6 系には専用ページ [Reliable Recall](https://cloto-dev.github.io/CPersona/2.6/ja/RELIABLE_RECALL_2_6/) があります。1 回の
呼び出しの中で反復する想起ループ、手がかりの契約、prior 関数、再構成の出口とその
件数の窓、失敗の分類、そして何をもって完了とするか。

**設計ノート** は、ある挙動がどう決まったかを、却下された経路も含めて記録します。
ある時点の記録です。ノートとガイドが食い違う場合はガイドが優先します。

- [クライアント別の権限 (ACL)](ACL_DESIGN.md) — 名前付きベアラトークン、
  エージェント単位の読み書き権限、既定拒否。
- [OAuth 対応](OAUTH_DESIGN.md) — リソースサーバーのメタデータ、トークン検証、
  比較した 3 経路、subject 単位の境界がどこに引かれるか。
- [サーバー供給の運用コンテキスト](OPERATING_CONTEXT_DESIGN.md) — 接続中の全 MCP
  クライアントへ運用者の指示を配布する仕組み。
- [申告型セッション同一性](SESSION_IDENTITY_DESIGN.md) — streamable HTTP では
  1 プロセスが 1 セッションにならない理由と、`session_key` がプロセス全体のどの状態を
  分割し直すか。
- [アクセス元の記録](MEMORY_ORIGIN_DESIGN.md) — `agent_id` が誰も名指さない経路の
  ために、観測した呼び出し元を各行へ記録します。
- [recall プレビュー階層](RECALL_PREVIEW_TIER_DESIGN.md) — プレビューの切り詰めと
  `get_contents` による展開経路。
- [埋め込み索引の連続配置](CONTIGUOUS_INDEX_DESIGN.md) — ベクトル走査の読み出しを
  SQLite の行から連続配置の sidecar ファイルへ移します。答えは変わりません。
- [走査窓の到達範囲と新しさの優遇](SCAN_WINDOW_REACH_DESIGN.md) — ベクトル走査窓を
  広げると最近の答えを失う理由と、新しさの優遇を捨てずに窓を広げるための 2 本目の
  順位付きリスト。
- [到達範囲・新しさ・far の票](https://cloto-dev.github.io/CPersona/2.6/ja/REACH_AND_RECENCY_PLAN/) — 3 つの計測を 1 つに
  まとめ、それぞれ何を確立したか、そして 2.6 系で far の票に値段を付ける計画。
- [適応的融合](https://cloto-dev.github.io/CPersona/2.6/ja/ADAPTIVE_FUSION_DESIGN/) — 2.6 系の計画。各検索器にプールの取り分を確保すること、
  プールサイズ gate から順位カットを外すこと、そして「いま計測済みの lexical 重み」と
  「後の条件付き証拠の融合モード」のどちらを採るかを決める事前登録済みの比較。
- [埋め込み劣化の通知](DEGRADED_ADVISORY_DESIGN.md) — 埋め込み層が死んだとき、
  静かに質を落とすのではなく recall がそれを報告する仕組み。

## 研究ノート { #research-notes }

2.6 系の設計ページが拠って立つもの (導出・実測・反証) を、信じるのではなく検算できる
形で置いた記録です。ノートは 2.6 系のドキュメントに載っており、その
[概要](https://cloto-dev.github.io/CPersona/2.6/ja/research/) に status の語彙があります。

- [適応的融合の導出](https://cloto-dev.github.io/CPersona/2.6/ja/research/adaptive-fusion-derivation/) — 各検索アームを、
  そのスコアが偶然で超えられる確率を通じて結合します。この規則は行ごとの影響度を
  閉じた形で持ち、現行の reciprocal rank fusion はその極限にあたります。
- [較正と admission floor](https://cloto-dev.github.io/CPersona/2.6/ja/research/calibration-admission-floor-2026-09/) —
  recall が負けていた 7 タスクを 3 つの較正法で測りました。床は原因ではありません。
  null は誤ったペア母集団から取られていました。小さなコーパスでは dense アームが
  飢餓します。
- [損失はどこにあるか、凍結段リプレイ](https://cloto-dev.github.io/CPersona/2.6/ja/research/frozen-stage-replay-2026-09/) —
  Track B 経路の全段を、凍結した埋め込みの上で 3 モデル分採点し、稼働中の pipeline に
  固定しました。純 ranking のタスクは融合段で、Gorilla は admission で、EPBench は
  gate で負けています。QASPER の利得は補充によるものです。
- [2 本のアーム、1 つの決定](https://cloto-dev.github.io/CPersona/2.6/ja/research/adaptive-fusion-identifiability/) — その損失を
  避けるために融合則が知らねばならないもの。アームごとの較正ではなく、dense スコアを
  **与えたうえで** lexical がどれだけ情報を足すか、です。同時密度比はそれを、重みを
  当てはめずに供給します。飢餓・gate の全滅・予約の閉形式も導いています。

## 3 つの記憶タイプ { #the-three-memory-types }

- **宣言的記憶** — 個別の事実・決定・ルール (`store` / `recall`)。
- **エピソード記憶** — セッション要約 (`archive_episode`)。
  [エピソード境界ペナルティ](behavior-contracts.md#3-episode-boundary-penalty)
  の駆動源でもあります。
- **プロフィール** — 蓄積されたユーザー/プロジェクト属性 (`update_profile`)。
  [スコアリング上の注意](behavior-contracts.md#7-profile-rows-carry-no-score)
  があります。

## AI エージェント向け { #for-ai-agents-reading-this-site }

このサイトの機械可読索引を [`llms.txt`](llms.txt) で公開しています。同梱の
[`cpersona-memory` skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
はエージェントに store / recall / archive の日常ワークフローを教え、正確な詳細は
このサイトへリンクで戻します。

## :material-gift-outline: 支援について { #sponsorship }

CPersona は MIT ライセンスで、この先も変わりません。役に立って、この作業が
続いてほしいと思っていただけたなら、[支援のページ](sponsorship.md)をご覧ください。
支援が買うもの・買わないものと、お金のかからない助け方を書いています。

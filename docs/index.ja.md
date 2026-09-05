<!-- i18n-source: docs/index.md@blob:865be50fd1061a72c0ce1c3391fdf3de14dc76ce -->

# CPersona ドキュメント

CPersona は、Claude をはじめとする MCP 対応エージェントに**セッションをまたぐ
永続記憶**を与える [MCP](https://modelcontextprotocol.io/) サーバーです。記憶は
ローカルの SQLite ファイル 1 つに保存され、3 層ハイブリッド検索 (vector + FTS5 +
keyword をランクまたは相対スコアで融合) で想起されます。サーバーは **LLM に一切
依存しません**: 生成モデルを呼びません。ただしそれが何を保証するかには 2 つの但し書き
があります — 埋め込みには依然としてコストがかかりうること (`EMBEDDING_MODE=api` は
既定で OpenAI を指すエンドポイントに対してリクエストごとに課金されます。ローカル
サーバーに対する `http` モードは課金されません)、そして recall は較正済みのゲートを
前提にすれば決定論的ですが、そのゲート自体はコーパスをランダムに標本して測るため、
同一データの 2 つの環境が別々の動作点に落ち着きうることです。

> **対象: CPersona 2.5.x。** このサイトが正式ドキュメントです — README や同梱
> skill の記述とこのサイトが食い違う場合はサイトが優先で、その食い違い自体が
> [報告に値するバグ](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
> です。
>
> **翻訳について**: 正本は英語版です。日本語版が古い場合や存在しないページは
> 英語版を参照してください (未翻訳ページは自動的に英語で表示されます)。

## 目的別ガイド { #where-to-go }

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **はじめに**

    ---

    インストールし、MCP クライアントに登録し、接続を端から端まで検証します。

    [:octicons-arrow-right-24: はじめに](getting-started.md)

-   :material-sitemap:{ .lg .middle } **アーキテクチャ**

    ---

    ストレージ構成、3 つの検索器、そして融合 → ゲート → 反転のパイプラインを図で。

    [:octicons-arrow-right-24: アーキテクチャ](architecture.md)

-   :material-toolbox:{ .lg .middle } **ツール一覧**

    ---

    すべてのツールを目的別にまとめ、各ツールから「驚きうる契約」へリンクしています。

    [:octicons-arrow-right-24: ツール一覧](tools.md)

-   :material-handshake:{ .lg .middle } **挙動契約**

    ---

    依存してよい挙動。ここを変えることは好みの問題ではなくバグである、という形で
    書かれています。

    [:octicons-arrow-right-24: 挙動契約](behavior-contracts.md)

-   :material-cog:{ .lg .middle } **設定リファレンス**

    ---

    全環境変数と既定値、そして HTTP トランスポートが応答を始めるための要件。

    [:octicons-arrow-right-24: 設定リファレンス](configuration.md)

-   :material-lifebuoy:{ .lg .middle } **運用 Runbook**

    ---

    バックアップ、劣化検知、recall を調整する順序、日本語コーパス、保守の周期。

    [:octicons-arrow-right-24: 運用 Runbook](operations.md)

-   :material-help-circle:{ .lg .middle } **FAQ**

    ---

    運用者が実際に訊く質問への短い回答。それぞれ詳細を持つページへ送ります。

    [:octicons-arrow-right-24: FAQ](faq.md)

-   :material-shield-check:{ .lg .middle } **品質保証**

    ---

    リリースがどう検査されるか: 監査ラウンド、バグ登録簿、構造ゲート、変異証明。

    [:octicons-arrow-right-24: 品質保証](quality-assurance.md)

</div>

## 設計ノートと標準 { #design-notes-and-standards }

ガイドの下には 2 種類のページがあり、それぞれ別の問いに答えます。

**プロジェクト標準** は、リリース・監査報告・生成されるポリシーブロックが
どうあるべきかを定めます。このプロジェクト以外にも採用されうる形で書かれています。

- [リリースライフサイクル標準](RELEASE_LIFECYCLE_STANDARD.md) — ティア定義
  (Stable / Current)、リスク駆動の pre-release ladder、サポート期間。本リポジトリが
  運用している実体は
  [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) です。
- [SuperAuditor 標準](SUPERAUDITOR_STANDARD.md) — findings を報告するための pull
  契約。深刻度の語彙、上限の意味論、そして「何を検出するか」については意図的に
  何も定めないこと。
- [ポリシーブロック標準](CLAUDE_MD_POLICY_STANDARD.md) — プロジェクトの skill が、
  エージェントが毎セッション読み込むファイル (`CLAUDE.md`、`AGENTS.md`、…) へ
  マーカー付きポリシーブロックをどう書き込むか、そして skill だけではその保証を
  担えない理由。

**これからの方向。** [ロードマップ](roadmap.md) は、各リリースラインが何のためにあり、
何を破ってよく、計画中の各機能がどの実測された問題に答えるかを、3 つの軸 (リリース
ライン・ランタイムとスケール・サポート tier) で記録します。記述であって納期の約束では
ありません。進捗はリリースノートと SUPPORT.md にあります。

**設計ノート** は、ある挙動がどう決まったかを、却下された経路も含めて記録した
ものです。ある時点の記録なので、上のガイドと食い違う場合はガイドが優先します。

- [クライアント別の権限 (ACL)](ACL_DESIGN.md) — 名前付きベアラトークン、
  エージェント単位の読み書き権限、既定拒否。
- [OAuth 対応](OAUTH_DESIGN.md) — リソースサーバーのメタデータとトークン検証、
  比較した 3 経路、subject 単位の境界。
- [サーバー供給の運用コンテキスト](OPERATING_CONTEXT_DESIGN.md) — 接続中の全 MCP
  クライアントへ運用者の指示を配布する仕組み。
- [申告型セッション同一性](SESSION_IDENTITY_DESIGN.md) — streamable-HTTP では
  1 プロセスが 1 セッションではない理由と、`session_key` がどのプロセス全体の状態を
  分割し直すか。
- [アクセス元の記録](MEMORY_ORIGIN_DESIGN.md) — `agent_id` が誰も名指さない経路の
  ために、観測した呼び出し元を各行へ記録する。
- [recall プレビュー階層](RECALL_PREVIEW_TIER_DESIGN.md) — プレビューの切り詰めと
  `get_contents` による展開経路。
- [埋め込み索引の連続配置](CONTIGUOUS_INDEX_DESIGN.md) — ベクトル走査の読み出しを
  SQLite の行から連続配置の sidecar へ移す。答えはビット単位で同一。
- [走査窓の到達範囲と新しさの優遇](SCAN_WINDOW_REACH_DESIGN.md) — ベクトル走査窓を
  広げると最近の答えを失う理由と、新しさの優遇を奪わずに到達範囲を動かすための
  2 本目の順位付きリスト。
- [到達範囲・新しさ・far の票](REACH_AND_RECENCY_PLAN.md) — 3 つの計測を 1 つの
  記述にまとめ、確立したことと、2.6 系で far の票に値段を付ける計画。
- [埋め込み劣化の通知](DEGRADED_ADVISORY_DESIGN.md) — 埋め込み層が死んだとき、
  静かに質を落とすのではなく recall がそれを報告する仕組み。

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

CPersona は MIT ライセンスで、それはこの先も変わりません。役に立って、
この作業が続いてほしいと思っていただけたなら、[支援のページ](sponsorship.md)に
「支援が買うもの・買わないもの」と、この規模のプロジェクトではより重要な
「お金がかからない助け方」を書いています。

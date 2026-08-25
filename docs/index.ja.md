<!-- i18n-source: docs/index.md@blob:2c44790e91029d41a5b0df9f3079079012bfb624 -->

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

## 目的別ガイド

| したいこと | 読むページ |
|---|---|
| インストール・セットアップ | [はじめに (Getting Started)](getting-started.md) |
| 依存してよい挙動を知る | [挙動契約 (Behavior Contracts)](behavior-contracts.md) |
| 29 個のツールが何をするか見る | [ツール一覧](tools.md) |
| 検索と保存の仕組みを理解する | [アーキテクチャ](architecture.md) |
| 運用: バックアップ・チューニング・劣化検知・コーパス索引 | [運用 Runbook](operations.md) |
| 環境変数を調べる | [設定リファレンス](configuration.md) |
| よくある質問への短い回答 | [FAQ](faq.md) |
| サブシステムの設計を理解する | 設計ドキュメント (サイドバー) |
| リリースティアとサポート期間 | [リリースライフサイクル](RELEASE_LIFECYCLE_STANDARD.md) + [SUPPORT.md](https://github.com/Cloto-dev/cpersona/blob/master/SUPPORT.md) |

## 3 つの記憶タイプ

- **宣言的記憶** — 個別の事実・決定・ルール (`store` / `recall`)。
- **エピソード記憶** — セッション要約 (`archive_episode`)。
  [エピソード境界ペナルティ](behavior-contracts.md#3-episode-boundary-penalty)
  の駆動源でもあります。
- **プロフィール** — 蓄積されたユーザー/プロジェクト属性 (`update_profile`)。
  [スコアリング上の注意](behavior-contracts.md#7-profile-rows-carry-no-score)
  があります。

## AI エージェント向け

このサイトの機械可読索引を [`llms.txt`](llms.txt) で公開しています。同梱の
[`cpersona-memory` skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
はエージェントに store / recall / archive の日常ワークフローを教え、正確な詳細は
このサイトへリンクで戻します。

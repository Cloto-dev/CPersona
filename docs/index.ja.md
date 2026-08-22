<!-- i18n-source: docs/index.md@d4d42f9a5a306819a1fb98f5b2d88c200ee527a2 -->

# CPersona ドキュメント

CPersona は、Claude をはじめとする MCP 対応エージェントに**セッションをまたぐ
永続記憶**を与える [MCP](https://modelcontextprotocol.io/) サーバーです。記憶は
ローカルの SQLite ファイル 1 つに保存され、3 層ハイブリッド検索 (vector + FTS5 +
keyword をランクまたは相対スコアで融合) で想起されます。サーバーは **LLM に一切
依存しません**: 生成モデルを呼ばないため、記憶による API コストはゼロで、動作は
決定論的です。

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
| インストール・セットアップ | [README Quick Start](https://github.com/Cloto-dev/cpersona#quick-start) (PyPI ページと同内容) |
| 依存してよい挙動を知る | [挙動契約 (Behavior Contracts)](behavior-contracts.md) |
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

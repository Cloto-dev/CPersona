<!-- i18n-source: docs/index.md@blob:1bfa89a190aba83cca763ba9e36abbcd76adfe17 -->

# CPersona ドキュメント

CPersona は [MCP](https://modelcontextprotocol.io/) サーバーです。Claude Code、Codex をはじめとする MCP クライアントに、**セッションをまたいで残る記憶**を与えます。

> **対象: CPersona {{ version_line }}**。このサイトが正式なドキュメントです。README や
> 同梱の skill の記述とこのサイトが食い違う場合はサイトが優先で、その食い違い自体が
> [報告に値するバグ](https://github.com/Cloto-dev/cpersona/issues/new?template=bug_report.yml)
> です。
>
> **翻訳について**: 原文は英語版です。日本語版が古いページと日本語版のないページは、
> 英語版を参照してください (未翻訳のページは自動的に英語で表示されます)。

## 目的別ガイド { #where-to-go }

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **はじめに**

    ---

    インストールし、MCP クライアントに登録し、接続を検証します。

    [:octicons-arrow-right-24: はじめに](getting-started.md)

-   :material-sitemap:{ .lg .middle } **アーキテクチャ**

    ---

    記憶の保存先、3 つの検索器の探し方、融合 → ゲート → 反転のパイプラインの
    働きを、図で説明します。

    [:octicons-arrow-right-24: アーキテクチャ](architecture.md)

-   :material-toolbox:{ .lg .middle } **ツール一覧**

    ---

    全ツールを目的別にまとめています。名前から想像できない挙動をするツールは、
    その理由を説明する挙動契約へリンクしています。

    [:octicons-arrow-right-24: ツール一覧](tools.md)

-   :material-handshake:{ .lg .middle } **挙動契約**

    ---

    依存してよい挙動です。ここを変えることは、好みの変更ではなくバグとして扱います。

    [:octicons-arrow-right-24: 挙動契約](behavior-contracts.md)

-   :material-cog:{ .lg .middle } **設定リファレンス**

    ---

    全環境変数と既定値、HTTP トランスポートが応答するための要件をまとめています。

    [:octicons-arrow-right-24: 設定リファレンス](configuration.md)

-   :material-lifebuoy:{ .lg .middle } **運用 Runbook**

    ---

    バックアップ、劣化の検知、recall を調整する順序、日本語のコーパス、保守の周期を扱います。

    [:octicons-arrow-right-24: 運用 Runbook](operations.md)

-   :material-database-arrow-up:{ .lg .middle } **2.6 への移行**

    ---

    2.5.x のストアを 2.6.0 へ一度に移行する手順です。バックアップ、最初の起動で
    移行されるもの、後で作るもの、戻し方を扱います。

    [:octicons-arrow-right-24: 2.6 への移行](upgrading-to-2.6.md)

-   :material-help-circle:{ .lg .middle } **FAQ**

    ---

    運用者からよく来る質問に短く答え、それぞれ詳しいページへリンクします。

    [:octicons-arrow-right-24: FAQ](faq.md)

-   :material-shield-check:{ .lg .middle } **品質保証**

    ---

    リリースの検査の仕方を説明します。監査ラウンド、バグ登録簿、構造ゲート、変異証明を扱います。

    [:octicons-arrow-right-24: 品質保証](quality-assurance.md)

-   :material-flask-outline:{ .lg .middle } **設計と研究**

    ---

    各挙動が決まった経緯、ロードマップ、ほかのプロジェクトでも採用できる標準、それらの元になった実測を集めています。CPersona を使うだけなら読む必要はありません。

    [:octicons-arrow-right-24: 設計と研究](design/index.md)

</div>

## 仕組み { #how-it-works }

記憶はローカルの SQLite ファイル 1 つに保存されます。recall はそのファイルを 3 通り
(vector・FTS5・keyword) で検索し、ランクまたは相対スコアで融合します。

サーバーは **LLM に一切依存しません**。生成モデルを呼びません。ただし、
次の 2 つまでは意味しません。

- **常に無料とは限りません**。`EMBEDDING_MODE=api` では、埋め込みリクエストごとに
  課金されます (既定のエンドポイントは OpenAI)。自前の埋め込みサーバーを指す
  `http` モードでは課金されません。
- **どの環境でも同じとは限りません**。recall は品質ゲートが較正済みであれば
  決定論的です。較正はコーパスから無作為に標本を取って行うため、同じデータを持つ
  2 つの環境で、較正されたゲートが異なることがあります。

## 3 つの記憶タイプ { #the-three-memory-types }

- 宣言的記憶: 個別の事実・決定・ルール (`store` / `recall`)。
- エピソード記憶: セッションの要約 (`archive_episode`)。
  有効にした場合は、
  [エピソード境界ペナルティ](behavior-contracts.md#3-episode-boundary-penalty)
  もこの記憶をもとに働きます。
- プロフィール: 蓄積されたユーザーやプロジェクトの属性 (`update_profile`)。
  スコアの扱いには
  [注意点](behavior-contracts.md#7-profile-rows-carry-no-score)
  があります。

## AI エージェント向け { #for-ai-agents-reading-this-site }

このサイトの機械可読の索引を [`llms.txt`](llms.txt) で公開しています。同梱の
[`cpersona-memory` skill](https://github.com/Cloto-dev/cpersona/tree/master/skills/cpersona-memory)
は、store / recall / archive の日々の使い方をエージェントに教え、正確な詳細は
このサイトへのリンクで案内します。

## :material-gift-outline: 支援 { #sponsorship }

CPersona は MIT ライセンスで、この先も変わりません。役に立って、この作業が
続いてほしいと思っていただけたなら、[支援のページ](sponsorship.md)をご覧ください。
支援で得られるもの・得られないものと、お金のかからない助け方を書いています。

<!-- i18n-source: docs/configuration.md@blob:f606c2923a82995f5634bcadb908de455c8629ee -->

# 設定リファレンス

> **対象: CPersona 2.5.x。** 設定はすべて環境変数で、妥当な既定値を持ちます。
> このページが正本で、README は quick start に必要な部分集合だけを持ちます。
>
> **翻訳について**: 正本は英語版です。変数名・既定値・型は原文のまま保持して
> います (訳すと設定できなくなるため)。

## 基本設定 { #core-settings }

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_DB_PATH` | `data/cpersona.db` | SQLite データベースのパス。**クライアントの作業ディレクトリからの相対**なので、セッションをまたいで 1 つの記憶を保つには絶対パスを指定してください |
| `CPERSONA_EMBEDDING_MODE` | `none` | 埋め込みモード (`http` または `none`) |
| `CPERSONA_EMBEDDING_URL` | *(未設定)* | 埋め込みサーバーの URL。例: `http://127.0.0.1:8401/embed` |
| `CPERSONA_VECTOR_SEARCH_MODE` | `local` | ベクトル検索の実行場所 (`local` = プロセス内コサイン、`remote` = 外部委譲) |
| `CPERSONA_RECALL_MODE` | `rrf` | recall の融合戦略 (`rrf` / `rsf` / `cascade`) — 後述 |
| `CPERSONA_RECALL_PREVIEW_CHARS` | `500` | プレビュー階層: recall 系ツールが返す本文の最大文字数。`full_content=true` は 1 応答あたり 200,000 文字の予算内で全文を返します (bug-211): 超過分は行がプレビュー階層に戻り — 関連度の高い行から全文で残し (bug-214) — 応答に `full_content_budget_chars` が付きます。残りは `get_contents` が自身の 40,000 文字予算で取得します。`0` はプレビュー階層のみを無効化し、予算は無効化しません |
| `CPERSONA_RRF_K` | `60` | RRF の平滑化パラメータ |
| `CPERSONA_MAX_CONTENT_LENGTH` | `16000` | 記憶 1 件・エピソード 1 件あたりの最大文字数。超過分は切り詰められ、`check_health(fix=true)` は既存行も上限で切るため、**下げると保存済みデータが短くなります**。2.5.4a2 で `2000` から引き上げ。埋め込みウィンドウを超えた本文も、行全体を索引するキーワードチャネル経由では検索できます |
| `CPERSONA_MAX_PROFILE_LENGTH` | `2000` | プロフィール 1 行あたりの最大文字数 (記憶とは別枠)。プロフィールは全 recall 応答に注入され、プレビュー切り詰めの対象外なので、この上限だけが唯一の歯止めです |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | confidence メタデータを結果に含める — かつ**それをランキングキーにする**: 結果集合はこのスコアで並べ直され、品質ゲートもこれを見ます。有効時、`CPERSONA_RECALL_MODE` は返却順を決めなくなります ([契約 §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)) |
| `CPERSONA_AUTO_CALIBRATE` | `false` | 起動時に自動較正する |
| `CPERSONA_TASK_QUEUE_ENABLED` | `true` | バックグラウンドタスクキュー (DB 永続・クラッシュ復帰可能) |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | 直近に想起された記憶へのペナルティ |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | 上記ペナルティの対象時間窓 (分) |
| `CPERSONA_MAX_MEMORIES` | `10000` | ベクトル検索の**走査ウィンドウ** (保存件数の上限ではありません) — 大きなコーパスでは引き上げてください ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_AUTOCUT_MIN_RESULTS` | `3` | この件数未満の結果集合は autocut されません。意味を持つのは confidence 有効時のみ — autocut は `rsf`/`rrf` では意図的に不活性です ([契約 §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals)) |
| `CPERSONA_FUSED_GATE_ENABLED` | `true` | 融合後の品質ゲート。無効化は最終手段です — 混入が素通りします |
| `CPERSONA_DEGRADED_ADVISORY` | `true` | 埋め込みが利用不能な間、recall 応答に `advisory` を付ける ([runbook](operations.md#detecting-a-dead-embedding-server)) |
| `CPERSONA_EPISODE_PENALTY_ENABLED` | `true` | エピソード境界ペナルティ ([契約 §3](behavior-contracts.md#3-episode-boundary-penalty)) |
| `CPERSONA_EPISODE_DECAY_RATE` | `0.01` | 境界より前の記憶に対する 1 時間あたりの減衰率 |
| `CPERSONA_EPISODE_DECAY_FLOOR` | `0.5` | ペナルティの下限 (古い記憶でも最大で半分まで) |

汎用エイリアス `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` / `EMBEDDING_MODEL` も
受理されます (両方設定されている場合は `CPERSONA_` 接頭辞つきが優先)。
マーケットプレイスのカタログと Quick Start は汎用名を使っています。

## リモート (HTTP) トランスポート { #remote-http-transport }

既定は stdio で、MCP クライアントがプロセスを所有し、ネットワークは介在しません。
`CPERSONA_TRANSPORT=streamable-http` を設定すると HTTP で配信します — 1 つの
サーバーを複数クライアントで共有し、ネットワーク越しに到達できます。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_TRANSPORT` | `stdio` | `stdio`、または HTTP 配信の `streamable-http` |
| `CPERSONA_HTTP_HOST` | `127.0.0.1` | バインドアドレス |
| `CPERSONA_HTTP_PORT` | `8402` | バインドポート |
| `CPERSONA_AUTH_TOKEN` | *(未設定)* | 全リクエストに要求する Bearer トークン |
| `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP` | `false` | HTTP トランスポートを認証なしで動かす |
| `CPERSONA_ACL_FILE` | *(未設定)* | クライアント別ケーパビリティ: 名前つき Bearer トークンにエージェント別の read/write 権限を与え、既定は拒否 ([ACL 設計](ACL_DESIGN.md)) |

**ループバックへの bind はセキュリティ境界ではありません。** トンネル
(cloudflared / ngrok)、リバースプロキシ、`kubectl port-forward`、公開された
コンテナポートは、いずれも `127.0.0.1` に転送します。つまり「ループバックに
bind した」ことは、誰が到達できるかについて何も語りません。到達できる者には
全ツールが露出します — `delete_agent_data` も、ファイルを読み書きする
`export_memories` / `import_memories` も含めてです。あなただけが話しかけられる
とは言えないプロセスなら、`CPERSONA_AUTH_TOKEN` を設定してください。

v2.5.3 以降、サーバーはこれを強制します: `CPERSONA_TRANSPORT=streamable-http`
かつ `CPERSONA_AUTH_TOKEN` 未設定なら**起動を拒否**します。**2.5.2 以前から
アップグレードし、トークンなしで HTTP トランスポートを動かしている場合、
サーバーは起動しません** — `CPERSONA_AUTH_TOKEN` を設定するか、本当に認証なしで
よいことを `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` で宣言してください
(ローカル開発限定)。それ以前の版は認証なしのループバック bind を許し、
「ループバックのみに bind した」とログに出していました — これは安全宣言のように
読めますが、安全宣言ではありませんでした。

`CPERSONA_ACL_FILE` の設定は、同じ要件を別の方法で満たします: 全リクエストが
名前つきクライアントに解決されなければならないため、単一トークンの検査は
適用されません。このモードでは `CPERSONA_AUTH_TOKEN` は**無視されます**
(起動時に警告)。従来のトークンを使い続けるべきクライアントは、ACL ファイルに
明示的に列挙する必要があります。権限モデル・ファイル形式・ツール分類は
[ACL 設計](ACL_DESIGN.md) を参照してください。

## recall の融合モード (`CPERSONA_RECALL_MODE`) { #recall-fusion-mode-cpersona_recall_mode }

- **`rrf`** (既定) — Reciprocal Rank Fusion: vector と FTS のチャネルを
  **順位のみ**で融合します。頑健でスケール非依存ですが、スコアの大きさは捨てます。
- **`rsf`** — Relative Score Fusion: 各チャネルの生スコア (vector はコサイン、
  keyword は bm25) をクエリ単位で min-max 正規化して加算するため、keyword
  チャネルの bm25 の大きさが融合後も残ります。**話題ドリフトが起きやすい文脈や、
  分かち書きのない言語 (日本語など) で推奨**です — `rrf` が平坦化してしまう
  その大きさこそが、そこでの識別シグナルだからです (≈ Weaviate の
  `relativeScoreFusion`。ClotoCore の `RECALL_CONTAMINATION_AB_2026-06-14`
  レポート §10–12 を参照)。*注意:* min-max 正規化は、`autocut` 有効時に小さく
  スコアが接近した結果集合を切りすぎることがあります — この相互作用が固まるまで
  既定は `rrf` のままです。
- **`cascade`** — チャネルを順番に埋める方式 (レガシー)。

**`CPERSONA_CONFIDENCE_ENABLED=true` のとき、融合モードは返却順を決めません。**
融合は「どの候補が結果集合に入るか」を選び、その後 confidence スコアリングが
集合を並べ直し、品質ゲートも融合スコアではなく confidence を見ます。1,545 件の
コーパスに 394 クエリで実測: confidence 有効時、`rsf` と `rrf` は **394 クエリ
すべてで同じ行を同じ順序**で返しました。無効時は一致が 10% 未満でした。融合モードを
設定してランキングの変化を期待するなら、confidence は無効のままにするか、モードが
効くのは「どの記憶が考慮されるか」であって「返ってくる順序」ではないと理解して
ください。

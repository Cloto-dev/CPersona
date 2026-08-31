<!-- i18n-source: docs/tools.md@blob:1ca8704b8acce303d9639de6c234867df89903bd -->

# ツール一覧

> **対象: CPersona 2.5.x。** 各引数の権威ある説明は、そのツール自身の MCP
> description です — あなたのクライアントがそれを読み、あなたが動かしている版と
> 一緒に配布されます。このページは **30 個のツール**を「何のために手を伸ばすか」で
> グループ分けし、名前から想像できない挙動を持つものは契約へリンクします。
>
> **翻訳について**: 正本は英語版です。日本語版が古い場合は英語版を参照してください。

## 日常の読み書き { #everyday-memory }

| ツール | 何をするか |
|---|---|
| `store` | メッセージ 1 件を記憶に書きます。分岐は `ok` ではなく `result` — `stored` / `skipped` / `rejected` — で行ってください ([重複排除の契約](behavior-contracts.md#5-dedup-semantics-skip-not-upsert)) |
| `recall` | 3 層ハイブリッド検索で記憶を取り出します。**末尾の要素が最良のマッチ**です ([順序の契約](behavior-contracts.md#1-recall-return-order-last-is-best)) |
| `recall_with_context` | 想起 *と同時に*、渡した会話履歴と重複排除しつつ統合します。返るのはスコア順ではなく**時系列**の統合です |
| `get_contents` | recall が返したプレビュー参照 (`mem:<id>` / `ep:<id>`) を全文に展開します ([プレビュー階層の設計](RECALL_PREVIEW_TIER_DESIGN.md)) |
| `archive_episode` | セッション要約を保存します。同時に [エピソード境界](behavior-contracts.md#3-episode-boundary-penalty) を動かし、それ以前に書かれたものを減点します |
| `update_memory` | 既存の記憶の内容を変更します。保存済みの事実を訂正する方法はこれであって、同じ `msg_id` での再 `store` ではありません |

## プロフィールと運用者コンテキスト { #profile-and-operator-context }

| ツール | 何をするか |
|---|---|
| `get_profile` | エージェントに蓄積されたユーザー/プロジェクトのプロフィールを読みます |
| `update_profile` | あなたが計算した要約でプロフィールを置き換えます。CPersona が代わりに書くことはありません — [LLM 非依存](architecture.md#zero-llm-dependency) を参照 |
| `get_operating_context` | 接続中の全クライアントへ配られる運用者所有の指示を読みます。MCP 越しでは読み取り専用で、編集はファイルシステム上で行います ([設計](OPERATING_CONTEXT_DESIGN.md)) |

プロフィール行は recall 応答に注入されますが
[スコアを持ちません](behavior-contracts.md#7-profile-rows-carry-no-score) —
`limit` を絞るときに効いてくる違いです。

## 一覧の閲覧 { #browsing }

| ツール | 何をするか |
|---|---|
| `list_memories` | 直近の記憶を新しい順に — 検索もスコアリングもしません |
| `list_episodes` | アーカイブ済みエピソードを新しい順に |
| `get_queue_status` | バックグラウンドタスクキューの深さと再試行状態 |

## 保護と削除 { #protection-and-deletion }

| ツール | 何をするか |
|---|---|
| `lock_memory` | 記憶の編集と削除を拒否させます。これは**保護であってランキングの押し上げではありません** ([契約](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)) |
| `unlock_memory` | その保護を解除します |
| `delete_memory` | 記憶を 1 件削除します。所有権が強制されるのは **`agent_id` を渡した場合だけ**です — 省略すると走査範囲が絞られず、他エージェントの行も削除できます (ロックはどちらでも拒否) |
| `delete_episode` | エピソードを 1 件削除します。所有権の扱いは上の `delete_memory` と同じ条件付きです |
| `delete_agent_data` | 1 エージェントに属するもの**すべて**を削除します。他のツールと同様にネットワークへ露出するため、[HTTP トランスポート](configuration.md#remote-http-transport) で `CPERSONA_AUTH_TOKEN` を設定すべき十分な理由になります |

## 検索品質 { #retrieval-quality }

| ツール | 何をするか |
|---|---|
| `set_recall_precision` | 主要なゲートのつまみ。エージェントの精度設定を変更し、**融合後の品質ゲート**を再較正します — 生の閾値をいじる前にこちらへ手を伸ばしてください ([調整の順序](operations.md#tuning-recall)) |
| `get_recall_precision` | そのエージェントに効いている精度設定を読みます |
| `calibrate_threshold` | **ベクトル**の閾値をコーパス自身から導出し直します。既定 (`separation`) はランダムペアの null 分布と同一セッション正例が分離する点から求め、`percentile` / `zscore` も選べます。ラベル不要。再埋め込みや大量インポートの後に実行してください |

## 移植と移行 { #portability-and-migration }

| ツール | 何をするか |
|---|---|
| `export_memories` | 記憶・エピソード・プロフィールを JSONL に書き出します。スキーマ版に依存しないため論理バックアップも兼ねます ([バックアップ runbook](operations.md#backup-and-restore)) |
| `import_memories` | その JSONL を読み戻します。冪等ですが鍵は 1 つではありません: 記憶は `msg_id` **と** project/channel スコープ内の同一内容で、エピソードは同一の要約で重複排除されます (エピソードは `msg_id` を持ちません) |
| `merge_memories` | あるエージェントのデータを別のエージェントへ、原子的かつ重複排除つきで移動/複製します |
| `migrate_channel_axis` | ブリッジ種別の記憶を具体的なチャネルへ振り直します。日常運用ではなく一度きりの修復です |

## ヘルスと保守 { #health-and-maintenance }

| ツール | 何をするか |
|---|---|
| `check_health` | レジストリ駆動の検査。重大度つきの検出項目を返し、`fix=true` で自動修復します: 汚染、重複、FTS 整合性、埋め込み次元のずれ、スキーマオブジェクト、滞留タスク、不正データ。設計上あえて報告のみの検査もあり、分離軸の衛生はその 1 つです — どの綴りを正とするかは修復ではなく運用者の判断だからです |
| `deep_check` | 意味的なデータ品質の検査: 匿名ソース、短すぎる内容、古いプロフィール、孤児エピソード |
| `get_session_findings` | 同じ検出結果を、必要な時に引く形で受け取ります — SuperAuditor の pull 契約 ([規格](SUPERAUDITOR_STANDARD.md))。設計上データベース全体が対象 (agent / project で絞りません)、読み取り専用で、`per_kind_limit` を超えた kind は `capped_kinds` に名指しされます。例外を起こした検査は呼び出し全体を失敗させず、kind `check_crashed` の finding として現れます |

`check_health` と `deep_check` は MCP の外から `python -m cpersona.checkup` としても実行でき、CI ではこの
形を使ってください。実行頻度の指針は
[運用 runbook](operations.md#maintenance-cadence) にあります。

## セッション制御 { #session-controls }

| ツール | 何をするか |
|---|---|
| `pause_persistence` | TTL の間、書き込みを no-op にします。応答は `persisted: false` を伴います — id ではなくこれで分岐してください |
| `resume_persistence` | 書き込みを即座に再開します |
| `persistence_status` | 書き込みが停止中か、残り TTL はどれだけかを返します |

ベンチマークや、コーパスに残したくない使い捨ての探索に使ってください。
**影響範囲は `session_key` に従います。** 3 つのツールはいずれもそれを `scope` として
返します。停止と、それが覆うべき書き込み呼び出しに同じキーを宣言すれば、停止はその
キーだけを覆います (`scope: "session"`) — 別のキーを送るセッションは、それによって
黙らされることも、それを解除することもありません。キーは比較されるだけで検証されない
ので、分割されるのは呼び出し元ではなくキーです。同じ文字列を送る者は誰でも同じ停止を
共有します。

キーを省略すると、キーを持たない全呼び出し元が共有するバケットを止めます
(`scope: "process"`)。クライアントが自分のプロセスを所有する stdio では、その
バケットがセッションそのものです。streamable-HTTP の配備では 1 プロセスが全
クライアントに応じるため、キーなしの停止は他のキーなしセッション全員の書き込みを
黙らせます — しかもそれらのセッションには何も伝わりません。

`persisted: false` の形に収まらない経路が 2 つあります: `check_health` と
`deep_check` はブロックされず `fix=false` に降格し、`migrate_channel_axis` は
dry-run を強制されて `repairs_skipped` を返し、`persisted` キー自体を持ちません。

## 分離のための引数 { #isolation-arguments }

3 つの分離軸は一様には提供されていません。`agent_id` は 30 個のツールのうち
22 個が受け取り、`project_id` は 6 個、`channel` はちょうど 4 個 — `store` /
`recall` / `recall_with_context` / `archive_episode` — だけです。これらは入れ子の
階層ではなく独立した 3 軸であり、読み取り時に「空の値」と「省略」は異なる意味を
持ちます — [分離軸](architecture.md#isolation-axes) を参照してください。

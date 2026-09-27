<!-- i18n-source: docs/faq.md@blob:8125f8b4c20cbf3cca5622b6285443c85f1001bf -->

# FAQ

> **対象: CPersona {{ version_line }}。** 実運用オペレーターから実際に寄せられた質問
> (匿名化済み) を種にしています。ここには短い回答だけを置きます。正確な詳細は
> [挙動契約](behavior-contracts.md) と [運用 Runbook](operations.md)
> (いずれも英語が正本) にあります。

---

### なぜ `recall` は最良のマッチを*最後*に返すのですか？ { #why-does-recall-return-the-best-match-last }

意図的な契約です。結果はスコア昇順で並び、最強の記憶が注入コンテキストの
末尾、つまり LLM が最も強く注意を向ける位置 ("lost in the middle") に来ます。
hit@k を評価するときは**末尾から**数えてください。先頭から数えると数値が
反転します。`recall_with_context` は別契約で、時系列マージを返します。
→ [契約 §1](behavior-contracts.md#1-recall-return-order-last-is-best)

### 最新の決定が古い決定に負け続けます。新しさを勝たせるには？ { #my-newest-decisions-keep-losing-to-older-ones-how-do-i-make-recency-win }

優先順に:

1. **負けたら実害が出る事実を recall に賭けない。** 現行の決定は決定的に
   注入される面 (`CLAUDE.md` / system prompt) に置き、記憶は「問われたときに
   見つかるべきもの」に使います。
2. **追記でなく上書き。** 置き換えられた決定は `update_memory` で書き換えます。
   検索空間に存在しない古い決定は、勝ちようがありません。
3. その上で必要なら、[事前分布](PRIOR_FUNCTION_DESIGN.md) の年齢の重み
   `CPERSONA_PRIOR_AGE_RATE` を設定します。品質ゲートが通した行の中で新しい記憶を
   上に来させ、古い記憶を除くことはありません。率を選ぶ計測が済むまで既定は無効です。
   2.6.0a7 からは `CPERSONA_CONFIDENCE_ENABLED=true` はランキングに時間を混ぜず、
   各行の横に `confidence` の値を返すだけです。

→ [recall に頼らないという選択](operations.md#when-not-to-rely-on-recall)

### `CPERSONA_CONFIDENCE_ENABLED=false` は「一時的に無効化された」機能ですか？ { #is-cpersona_confidence_enabledfalse-a-temporarily-disabled-feature }

いいえ。壊れているから無効なのではなく、保守的な出荷既定です。confidence は
ランキングの意味論を変えるため、opt-in で出荷しています。本番でも使われています
(メンテナ自身のインスタンスは `rsf` + confidence on で運用)。2.6.0a7 からは、
有効にしても各行に `confidence` の値が加わるだけです。
`CPERSONA_CONFIDENCE_ORDERING=legacy` で、以前のリリースの再ソートと confidence による
ゲートに戻ります。
→ [契約 §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)

### Markdown ファイル群の索引を CPersona と同期し続けるには？ { #how-do-i-keep-an-index-of-markdown-files-in-sync-with-cpersona }

ファイル監視も upsert も組み込まれていません。CPersona は受動サーバーで、
投入は常に呼び出し側が駆動します。サポートされるパターンは 2 つです。(A) 索引
専用の `agent_id` を切り、変更時に丸ごと再構築する方法 (まず推奨。差分ロジックが
不要で、常に正本と一致していることを証明できます)。(B) 呼び出し側で content-hash
台帳を持ち、変更チャンクを `update_memory` で更新する方法。唯一の罠は、*変更
された*内容を同じ `msg_id` で再 store しても、**何も告げられず skip** され、
更新されないことです。
→ [コーパス索引パターン](operations.md#corpus-indexing-and-sync-patterns)

### 日本語 (CJK) コーパスでは何を設定すべきですか？ { #what-should-i-tune-for-a-japanese-or-other-cjk-corpus }

`CPERSONA_RECALL_MODE=rsf` を設定します。それだけです。rsf モードは FTS5 の弱い
CJK トークナイズを補うために存在します。既定の埋め込みモデルは、クエリと記憶が
固有名詞・識別子のアンカーを共有すると強く、語彙が重ならない純粋な概念一致には
弱い性質があります。具体的なアンカー語を入れてクエリを書くのは、正しい適応です。
→ [日本語 / CJK コーパス](operations.md#japanese-and-cjk-corpora)

### recall の結果が少なすぎます。実際に効くつまみはどれですか？ { #recall-returns-too-few-results-which-knob-actually-widens-the-gate }

`set_recall_precision(agent_id, "lenient")` です。既定の fusion mode では実質
*唯一*のポリシーつまみになります。`CPERSONA_AUTOCUT_MIN_RESULTS` は `rsf` /
`rrf` では何もしません (autocut はランク融合スコアでは意図的に不発です)。
fused gate 全体の無効化は、最後の手段です。
→ [recall のチューニング](operations.md#tuning-recall)

### コーパスが `CPERSONA_MAX_MEMORIES` を超えたらどうなりますか？ { #what-happens-when-the-corpus-grows-past-cpersona_max_memories }

何も削除されず、何も壊れません。この定数は*ベクトル走査窓*であって、保存上限では
ありません。窓より古い行も FTS・keyword 経路からは届きます。大規模コーパスでは
env で窓を上げてください。それが想定された使い方で、アーカイブ運用は不要です。
→ [契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)

### `archive_episode` はどの頻度で？過去分の一括投入は害がありますか？ { #how-often-should-archive_episode-run-and-does-bulk-backfill-hurt }

想定頻度は、セッション終了ごとに 1 件です。

既定の設定なら、過去分を一括投入しても害はありません。問題になるのは、
エピソード境界ペナルティ (2.6.0a7 から既定で無効) を有効にしている場合だけです。
このペナルティは現セッションの記憶を穏やかに優先します (境界より古い記憶は下限で
半減)。境界は単に最新エピソードのタイムスタンプなので、ペナルティが有効だと
**過去の会話を一括投入すると境界が投入時刻に動き、それより古い全記憶が
ペナルティ対象になります**。エピソードの backfill はしないか、する間は
ペナルティを無効のまま (`CPERSONA_EPISODE_PENALTY_ENABLED=false`) にしてください。
→ [契約 §3](behavior-contracts.md#3-episode-boundary-penalty)

### `lock_memory` で記憶の順位は上がりますか？ { #does-lock_memory-make-a-memory-rank-higher }

いいえ。lock は削除・編集からの保護です。ランキングには影響せず、locked でも
recall で負けることはあります。「失われては困る」なら lock、「常にコンテキストに
あってほしい」なら決定的注入です。

プロフィール (`update_profile`) が確実に浮上する経路になるのは、confidence
scoring が on のときだけです。off (既定) ではプロフィール行はスコアを持たず、
コーパスが埋まっていると `limit` で切られます。
→ [契約 §7](behavior-contracts.md#7-profile-rows-carry-no-score) /
[§9](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)

### operating context は設定が必要ですか？ { #do-i-need-to-configure-the-operating-context }

単一クライアント・単一エージェント運用なら不要です。未設定が正しい状態であって、
欠落ではありません。`operating-context.toml` は、1 つのサーバーに*複数の* MCP
クライアントを接続し、共通の運用指示と project-id レジストリを全クライアントに
配りたいオペレーターのための道具です。
→ [OPERATING_CONTEXT_DESIGN](OPERATING_CONTEXT_DESIGN.md)

### データベースを安全にバックアップするには？ { #how-do-i-back-up-the-database-safely }

サーバー稼働中の素朴な `cp` は不可です (WAL のため)。`sqlite3 ... ".backup ..."`
か `VACUUM INTO` を使うか、サーバーを止めて `.db` を `-wal` / `-shm` ごとコピー
してください。月次の `export_memories` JSONL を併用し、稼働中の DB は
クラウド同期フォルダの外に置きます。
→ [バックアップとリストア](operations.md#backup-and-restore)

### 埋め込みサーバーが死んだことに気づくには？ { #how-do-i-notice-the-embedding-server-died }

自分で見張る必要はありません。劣化中の recall には `advisory` フィールドが付き
(エージェントに表示させてください)、行を書いた `store` は `embedded: true|false`
を報告し、`check_health(fix=true)` が停止中に書かれた行を修復します。

ただし `embedded` だけを見張らないでください。`skipped` や `rejected` の store は
このキーを持たないため、コーパスに既にある内容を再 store しても、encoder に
ついては何も分かりません。また `check_health` が緑なだけでは、エンドポイントの
生存証明になりません。
→ [埋め込みサーバー停止の検知](operations.md#detecting-a-dead-embedding-server)

### CPersona が LLM で記憶を統合・要約する日は来ますか？ { #will-cpersona-ever-merge-or-summarize-memories-with-an-llm }

来ません。*サーバーは生成モデルを呼ばない*ことは不変の核ドクトリンです。
モデルとの通信は embedding だけなので、記憶自体に API コストがなく、決定論的に
動きます。

将来ラインで計画されている想起側の機能も、決定論的な SQL と純関数の範囲に留まり、
生成文ではなく出典を追跡できる結果を返し、元の記憶を改変も置換もしません。
意味的な要約は従来どおり呼び出し側エージェントの仕事で、その結果の置き場が
`archive_episode` です。

### 支援しないと CPersona は使えませんか？ { #do-i-have-to-sponsor-anything-to-use-cpersona }

いいえ。MIT ライセンスであり、支援しない人に対して何かを出し惜しみすることは
ありません。有料ティアも、支援者限定ビルドも無く、issue の扱いにも影響しません。
[支援について](sponsorship.md)に「買うもの・買わないもの」と、お金がかからない
助け方を書いています。
<!-- i18n-source: docs/faq.md@d4d42f9a5a306819a1fb98f5b2d88c200ee527a2 -->

# FAQ

> **対象: CPersona 2.5.x。** 実運用オペレーターから実際に寄せられた質問
> (匿名化済み) を種にしています。ここには短い回答だけを置き、正確な詳細は
> [挙動契約](behavior-contracts.md) と [運用 Runbook](operations.md)
> (いずれも英語が正本) にあります。

---

### なぜ `recall` は最良のマッチを*最後*に返すのですか？

意図的な契約です: 結果はスコア昇順で並び、最強の記憶が注入コンテキストの
末尾 — LLM が最も強く注意を向ける位置 ("lost in the middle") — に来ます。
hit@k を評価するときは**末尾から**数えてください。先頭から数えると数値が
反転します。`recall_with_context` は別契約で、時系列マージを返します。
→ [契約 §1](behavior-contracts.md#1-recall-return-order-last-is-best)

### 最新の決定が古い決定に負け続けます。新しさを勝たせるには？

優先順に:

1. **負けたら実害が出る事実を recall に賭けない** — 現行の決定は決定的に
   注入される面 (`CLAUDE.md` / system prompt) に置き、記憶は「問われたときに
   見つかるべきもの」に使う。
2. **追記でなく上書き**: 置き換えられた決定は `update_memory` で書き換える。
   検索空間に存在しない古い決定は勝ちようがない。
3. その上で必要なら `CPERSONA_CONFIDENCE_ENABLED=true` — 時間減衰がランキングに
   混ざります。ただし順序と品質ゲートが fusion mode から confidence に切り替わる
   点に注意し、切り替え後に `calibrate_threshold` を一度実行してください。
   細粒度の新しさ*ランキング* (recency-weighted search) は 2.6 系の計画機能です。

→ [recall に頼らないという選択](operations.md#when-not-to-rely-on-recall)

### `CPERSONA_CONFIDENCE_ENABLED=false` は「一時的に無効化された」機能ですか？

いいえ — 壊れているから無効なのではなく、保守的な出荷既定です。confidence は
ランキングの意味論を変えるため opt-in で出荷しています。本番でも使われています
(メンテナ自身のインスタンスは `rsf` + confidence on で運用)。有効化する場合は、
結果の再ソートと品質ゲートの切り替えが起きることを理解してください —
→ [契約 §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)

### Markdown ファイル群の索引を CPersona と同期し続けるには？

ファイル監視も upsert も組み込まれていません — CPersona は受動サーバーで、
投入は常に呼び出し側駆動です。サポートされるパターンは 2 つ: (A) 索引専用の
`agent_id` を切り、変更時に丸ごと再構築 (まず推奨 — 差分ロジック不要で、常に
正本と一致していることを証明できる)、(B) 呼び出し側で content-hash 台帳を持ち、
変更チャンクは `update_memory` で更新。唯一の罠: *変更された*内容を同じ
`msg_id` で再 store しても**黙って skip** され、更新されません。
→ [コーパス索引パターン](operations.md#corpus-indexing-and-sync-patterns)

### 日本語 (CJK) コーパスでは何を設定すべきですか？

`CPERSONA_RECALL_MODE=rsf` — 以上です。rsf モードは FTS5 の弱い CJK
トークナイズを補うために存在します。既定の埋め込みモデルは、クエリと記憶が
固有名詞・識別子のアンカーを共有すると強く、語彙が重ならない純粋な概念一致には
弱い性質があります。具体的なアンカー語を入れてクエリを書くのは正しい適応です。
→ [日本語 / CJK コーパス](operations.md#japanese-and-cjk-corpora)

### recall の結果が少なすぎます。実際に効くつまみはどれですか？

`set_recall_precision(agent_id, "lenient")` — 既定の fusion mode では実質
*唯一*のポリシーつまみです。`CPERSONA_AUTOCUT_MIN_RESULTS` は `rsf`/`rrf` では
何もしません (autocut はランク融合スコアでは意図的に不発)。fused gate 全体の
無効化は最後の手段です。
→ [recall のチューニング](operations.md#tuning-recall)

### コーパスが `CPERSONA_MAX_MEMORIES` を超えたらどうなりますか？

何も削除されず、何も壊れません: この定数は*ベクトル走査窓*であって保存上限では
ありません。窓より古い行も FTS・keyword 経路からは届きます。大規模コーパスでは
env で窓を上げてください — それが想定された使い方で、アーカイブ運用は不要です。
→ [契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)

### `archive_episode` はどの頻度で？過去分の一括投入は害がありますか？

想定頻度はセッション終了ごとに 1 件です。エピソード境界ペナルティは
現セッションの記憶を穏やかに優先します (境界より古い記憶は下限で半減) — そして
境界は単に最新エピソードのタイムスタンプなので、**過去の会話を一括投入すると
境界が投入時刻に動き、それより古い全記憶がペナルティ対象になります**。
エピソードの backfill はしないか、する間だけペナルティを無効化
(`CPERSONA_EPISODE_PENALTY_ENABLED=false`) してください。
→ [契約 §3](behavior-contracts.md#3-episode-boundary-penalty)

### `lock_memory` で記憶の順位は上がりますか？

いいえ。lock は削除・編集からの保護で、ランキングには影響せず、locked でも
recall で負けることはあります。「失われては困る」→ lock。「常にコンテキストに
あってほしい」→ 決定的注入。プロフィール (`update_profile`) が確実に浮上する
経路になるのは confidence scoring が on のときだけです — off (既定) では
プロフィール行はスコアを持たず、コーパスが埋まっていると `limit` で切られます。
→ [契約 §7](behavior-contracts.md#7-profile-rows-carry-no-score) /
[§9](behavior-contracts.md#9-lock_memory-protects-it-does-not-boost)

### operating context は設定が必要ですか？

単一クライアント・単一エージェント運用なら不要です — 未設定が正しい状態で、
欠落ではありません。`operating-context.toml` は、1 つのサーバーに*複数の* MCP
クライアントを接続し、共通の運用指示と project-id レジストリを全クライアントに
配りたいオペレーターのための道具です。
→ [OPERATING_CONTEXT_DESIGN](OPERATING_CONTEXT_DESIGN.md)

### データベースを安全にバックアップするには？

サーバー稼働中の素朴な `cp` は不可です (WAL)。`sqlite3 ... ".backup ..."` か
`VACUUM INTO` を使うか、サーバーを止めて `.db` を `-wal`/`-shm` ごとコピー
してください。月次の `export_memories` JSONL を併用し、稼働中の DB は
クラウド同期フォルダの外に置きます。
→ [バックアップとリストア](operations.md#backup-and-restore)

### 埋め込みサーバーが死んだことに気づくには？

自分で見張る必要はありません: 劣化中の recall には `advisory` フィールドが付き
(エージェントに表示させてください)、すべての `store` は `embedded: true|false`
を報告し、`check_health(fix=true)` が停止中に書かれた行を修復します。なお
`check_health` が緑なだけではエンドポイントの生存証明にならない点に注意。
→ [埋め込みサーバー停止の検知](operations.md#detecting-a-dead-embedding-server)

### CPersona が LLM で記憶を統合・要約する日は来ますか？

来ません。*サーバーは生成モデルを呼ばない*ことは不変の核ドクトリンです —
モデルとの通信は embedding だけで、だから記憶自体に API コストがなく決定論的に
動きます。将来ラインで計画されている想起側の機能も、決定論的な SQL + 純関数の
範囲に留まり、生成文ではなく出典を追跡できる結果を返し、元の記憶を改変・置換
しません。意味的な要約は従来どおり呼び出し側エージェントの仕事です
(その結果の置き場が `archive_episode` です)。

<!-- i18n-source: docs/configuration.md@blob:2e0526257fa0f1a27856b7b58157be8743e057fc -->

# 設定リファレンス

> **対象: CPersona {{ version_line }}。** 設定はすべて環境変数で、妥当な既定値を持ちます。
> このページが正本で、README は quick start に必要な部分集合だけを持ちます。
>
> **翻訳について**: 正本は英語版です。変数名・既定値・型は原文のまま保持して
> います (訳すと設定できなくなるため)。

## 基本設定 { #core-settings }

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_DB_PATH` | `data/cpersona.db` | SQLite データベースのパス。**クライアントの作業ディレクトリからの相対**なので、セッションをまたいで 1 つの記憶を保つには絶対パスを指定してください |
| `CPERSONA_EMBEDDING_MODE` | `none` | 埋め込みモード: `http` (ローカルの埋め込みサーバー)、`api` (OpenAI 互換エンドポイント — `CPERSONA_EMBEDDING_API_URL` の既定は OpenAI なので、このモードはリクエストごとに課金されます)、`none` のいずれか |
| `CPERSONA_EMBEDDING_URL` | *(未設定)* | 埋め込みサーバーの URL。例: `http://127.0.0.1:8401/embed` |
| `CPERSONA_VECTOR_SEARCH_MODE` | `local` | ベクトル検索の実行場所 (`local` = プロセス内コサイン、`remote` = 外部委譲) |
| `CPERSONA_RECALL_MODE` | `rrf` | recall の融合戦略 (`rrf` / `rsf` / `cascade`) — 後述 |
| `CPERSONA_RECALL_PREVIEW_CHARS` | `500` | プレビュー階層: recall 系ツールが返す本文の最大文字数。`full_content=true` は 1 応答あたり 200,000 文字の予算内で全文を返します (bug-211): 超過分は行がプレビュー階層に戻り — 関連度の高い行から全文で残し (bug-214) — 応答に `full_content_budget_chars` が付きます。残りは `get_contents` が自身の 40,000 文字予算で取得します。`0` はプレビュー階層**と両方の予算を**無効化します — 無効な階層への降格は本文を無言で落とすことになるため、切り詰めをやめる選択はどこでも切り詰めないという選択になります |
| `CPERSONA_RRF_K` | `60` | RRF の平滑化パラメータ |
| `CPERSONA_MAX_CONTENT_LENGTH` | `16000` | 記憶 1 件・エピソード 1 件あたりの最大文字数。超過分は切り詰められ、`check_health(fix=true)` は既存行も上限で切るため、**下げると保存済みデータが短くなります**。2.5.4a2 で `2000` から引き上げ。埋め込みウィンドウを超えた本文も、行全体を索引するキーワードチャネル経由では検索できます |
| `CPERSONA_MAX_PROFILE_LENGTH` | `2000` | プロフィール 1 行あたりの最大文字数 (記憶とは別枠)。プロフィールはプレビュー切り詰めの対象外なので、この上限だけが唯一の歯止めです。ただし*全*応答に注入されるわけではありません: プールが 50 行未満の間は品質ゲートがプロフィール行を落とし、スコア付きの結果で埋まっている場合は `limit` が落とします ([契約 §7](behavior-contracts.md#7-profile-rows-carry-no-score)) |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | confidence メタデータを結果に含める — かつ**それをランキングキーにする**: 結果集合はこのスコアで並べ直され、品質ゲートもこれを見ます。有効時、`CPERSONA_RECALL_MODE` は返却順を決めなくなります ([契約 §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)) |
| `CPERSONA_AUTO_CALIBRATE` | `false` | 起動時に自動較正する |
| `CPERSONA_BLOCK_BUILD_ENABLED` | `false` | 各レコードを節に相当する Block へ分け、Block ごとに符号量子化ベクトルを 1 本保存する ([Block による到達](BLOCK_REACH_DESIGN.md))。off は「作るが読まない」ではなく、埋め込み呼び出しも行もキューの仕事も無いという意味。まだ Block を読む経路は無いので、今 on にして得られるのは索引だけで挙動は変わらない |
| `CPERSONA_TASK_QUEUE_ENABLED` | `true` | バックグラウンドタスクキュー (DB 永続・クラッシュ復帰可能) |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | 直近に想起された記憶へのペナルティ |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | 上記ペナルティの対象時間窓 (分) |
| `CPERSONA_MAX_MEMORIES` | `10000` | ベクトル検索の**走査ウィンドウ** (保存件数の上限ではありません) — 大きなコーパスでは引き上げてください ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_REACH` | `0` | ベクトル検索が走査ウィンドウの先をどこまで見てよいか (行数)。効果を持たせるには **`CPERSONA_MAX_MEMORIES` より大きくする必要があります**: 同値以下 (既定の `0` を含む) では遠方リストは存在せず、追加の処理は一切走りません。大きくすると、2 つの数値の間にある行が**第 2 のリスト**としてランク付けされ、第 1 のリストと並んで融合されます。つまりウィンドウは新しさの事前分布として働き続けたまま、到達距離だけを独立に伸ばせます。ローカルのベクトル検索と `rrf`/`rsf` の融合モードでのみ有効です ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_FAR_LIMIT` | `0` | その第 2 のリストのうち何行を融合層に渡すか。`0` (既定) は**応答の `limit` と同じ**という意味で、この設定なしで構築される第 2 のリストそのものです。正の値を与えると `min(limit, N)` 行に切り詰められます。これは候補件数の上限であり、行のスコア計算は一切変わりません。したがって残るのは、フル長のリストが先頭に並べていた行そのものです。`CPERSONA_VECTOR_REACH` が `CPERSONA_MAX_MEMORIES` より大きくない限り無関係で、第 1 のリスト側の打ち切りは `limit` のままです ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_RECALL_DEPTH_FLOOR` | `0` | 想起深度: 応答の `limit` にかかわらず、各検索経路が融合に渡す候補数の下限です。深度は `max(limit, この値)` で、`CPERSONA_RECALL_LIBRARY_MAX_LIMIT` が上限です。`0` では `limit` と等しく、2.5 系の結合を維持します。設定しない限り順位は変わりません。`limit` を超える場合、応答の `depth` で候補の深さを確認できます。融合モードのみ有効です。`cascade` は段階的に `limit` 件を埋めるため対象外です ([設計](RELIABLE_RECALL_2_6.md#4-depth-is-not-count)) |
| `CPERSONA_AUTOCUT_MIN_RESULTS` | `3` | この件数未満の結果集合は autocut されません。autocut は類似度スケールのシグナル — confidence スコアリング下、あるいは `cascade` が作る生 cosine だけの均質なリスト — に対して発火し、`rsf`/`rrf` では意図的に不活性です ([契約 §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals))。したがってこのつまみが働くかどうかを決めるのは融合モードです |
| `CPERSONA_FUSED_GATE_ENABLED` | `true` | 融合後の品質ゲート。無効化は最終手段です: フィルタはプール規模のヒューリスティックにフォールバックし、粗くはなりますが弱い一致は依然として弾かれます — 失うのはこのコーパスに対して測定された動作点です |
| `CPERSONA_DEGRADED_ADVISORY` | `true` | 埋め込みが利用不能な間、recall 応答に `advisory` を付ける ([runbook](operations.md#detecting-a-dead-embedding-server)) |
| `CPERSONA_UPDATE_CHECK` | `true` | プロセス起動ごとに 1 回 pypi.org を参照し、このサーバーの新しい — あるいは撤回された — リリースを検出して `recall` / `check_health` / `check_update` で報告する ([何を送るか](architecture.md#transports))。`false` で機能全体を無効化します: リクエストもキャッシュファイルも通知もありません。どちらの設定でも更新が自動で行われることはありません |
| `CPERSONA_UPDATE_CHECK_INTERVAL_SECONDS` | `86400` | その判定が有効な期間。データベースの隣の `update-check.json` にキャッシュされ、この時間内の再起動ではリクエストが発生しません |
| `CPERSONA_EPISODE_PENALTY_ENABLED` | `true` | エピソード境界ペナルティ ([契約 §3](behavior-contracts.md#3-episode-boundary-penalty)) |
| `CPERSONA_EPISODE_DECAY_RATE` | `0.01` | 境界より前の記憶に対する 1 時間あたりの減衰率 |
| `CPERSONA_EPISODE_DECAY_FLOOR` | `0.5` | ペナルティの下限 (古い記憶でも最大で半分まで) |

汎用エイリアス `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` / `EMBEDDING_MODEL` も
受理されます (両方設定されている場合は `CPERSONA_` 接頭辞つきが優先)。
マーケットプレイスのカタログと Quick Start は汎用名を使っています。

## 再構成の件数 { #reconstruction-count }

`reconstruct` の呼び出しごとに `count` を宣言します。上限の対象は**束ねた想起単位**であり、
保存行数や検索深度ではありません。設定と呼び出し引数のどちらにも指定がなければ、上限は **1** です。

| 変数 | 既定 | 説明 |
|----------|---------|-------------|
| `CPERSONA_RECONSTRUCT_DEFAULT_COUNT` | `1` | 呼び出し側が `count` を省略した時の上限 |
| `CPERSONA_RECONSTRUCT_FORCED_COUNT` | *(未設定)* | 全呼び出しで要求値と既定値を上書きします。充足目標ではなく上限です |
| `CPERSONA_RECONSTRUCT_MAX_COUNT` | `10` | 絶対上限。超える要求は切り詰めて報告します。実験的な制限であり、実測で最適とされた件数ではありません |
| `CPERSONA_RECONSTRUCT_DEFAULT_BUDGET` | `4000` | 呼び出し側が `budget` を省略した時のペイロード予算 (引用本文の文字数)。件数と予算の掃引で選ぶまでの暫定値です |
| `CPERSONA_RECONSTRUCT_FORCED_BUDGET` | *(未設定)* | 全呼び出しで要求値と既定値を上書きします |
| `CPERSONA_RECONSTRUCT_MAX_BUDGET` | `20000` | 予算の絶対上限。超える要求は切り詰めて報告します。既定値・強制値がこれを超える場合、または設定値がプレビュー層の抜粋 1 つ分に満たない場合は起動時に停止します |

優先順位は `forced ?? requested ?? default` で、最大値で制限します。
既定値や強制値が最大値を超える設定は、起動時エラーになります。
`count=5` に対して有効な item が 2 件なら、`requested_count=5`、`effective_count=5`、
`returned_count=2` と `shortfall_reason` を返します。件数を埋めるために item を複製したり、
クラスタを分割したり、選択基準を緩めたりしません。強制値を設定しても、この規則は変わりません。
束ねには確定的な出典キーを使い、任意の文章間の意味的同一性は判定しません。

## コーパス規模の上限 { #corpus-scale-caps }

以下は、コーパスの規模に比例して増える処理 (インデックス保守・health の修復・
較正のサンプリング) を抑える上限です。

いずれも絶対的な行数で、およそ 10,000 行のコーパスを想定して決められています。
その規模では全体を覆いますが、150,000 行のコーパスに対しては同じ数値が「標本」に
なります。上限に当たってもエラーにはならず、単に小さい答えが返ります。症状が出るのを
待つのではなく、意図的に引き上げてください。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` | `10000` | ベクトルインデックスが*穴 (hole)* として名前を記録できる行数 — ファイルに載せられなかった行 (`created_at` が標準形でない) と、ビルド時点で埋め込みを持たなかった行の合計。これらは毎クエリで実テーブルから id 指定で読まれます。この数を超えるとインデックスのビルド自体を見送り、recall は (正しいが遅い) 全走査に留まります — 一括インポート後、埋め込みのバックログが捌けるまでの状態がこれです。既定値は 150,000 行コーパスの 6.7% を覆います。最悪ケース (記録済みの穴すべてが後から埋め込みを得た場合) でもクエリあたり約 65 ms で、次回リビルドが穴を吸収すれば解消します |
| `CPERSONA_REEMBED_ROW_CAP` | `5000` | 1 回の `check_health(fix=true)` が再埋め込みする「埋め込みなし」行数。報告される `repairable` の上限でもあります。埋め込みは書き込みロックを取る前に行われるため、この値が縛るのは prefetch の実時間とロック中の `UPDATE` 件数です。大きなバックログを少ない回数で捌きたい場合は引き上げてください: 旧既定値 500 では 50,000 行のバックログに 100 回の実行が必要で、しかもバックログが `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` を超えている間はインデックスもビルドできません |
| `CPERSONA_NEAR_DUPLICATE_ROW_CAP` | `5000` | `deep_near_duplicate` が比較する埋め込み済み行数。この比較はメモリが O(n²) です: 1024 次元ベクトルでの実測で、5,000 行はピーク 266 MB / 約 100 ms、10,000 行は 982 MB — 既定値が大きなコーパスを覆わず標本に留めているのはこのためです |
| `CPERSONA_INVALID_SOURCE_CLASSIFY_CAP` | `10000` | 1 回の `check_invalid_source_type` が分類する不正な `source` 行数。コストは行ごとの JSON パース (マイクロ秒オーダー) なので、上の 2 つより大きく取れます。上限を超えると標本が不完全になり、チェックは自身の severity を下げることを見送ります — 失われるのは判定であって正しさではありません |
| `CPERSONA_NORMALIZATION_SCAN_CAP` | `10000` | `deep_unnormalized_content` が 1 回の実行で読む行数。Unicode 正規化は SQL の述語にできない (SQLite に NFC 関数が無い) ので、これは本文を読まないと答えられない唯一の check であり、この上限が「本文を読む」を「コーパス全体を読む」にしないための線です。上限を超えた実行は `complete: false` を返し、下限値を総数と読ませません |
| `CPERSONA_CALIBRATE_MAX_SAMPLE` | `5000` | 呼び出し側が何を要求しても効く `calibrate_threshold` の `sample_size` の上限。near-duplicate と同じ O(n²) の行列を扱い、無制限な値が接続を共有する全エージェントごとメモリを食い潰すのを防ぐために存在します。引き上げはマシンが抱えられる範囲まで (上記の実測値を参照) |

## リモート (HTTP) トランスポート { #remote-http-transport }

既定は stdio で、MCP クライアントがプロセスを所有し、ネットワークは介在しません。
`CPERSONA_TRANSPORT=streamable-http` を設定すると HTTP で配信します。1 つの
サーバーを複数クライアントで共有し、ネットワーク越しに到達できます。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_TRANSPORT` | `stdio` | `stdio`、または HTTP 配信の `streamable-http` |
| `CPERSONA_HTTP_HOST` | `127.0.0.1` | バインドアドレス |
| `CPERSONA_HTTP_PORT` | `8402` | バインドポート |
| `CPERSONA_AUTH_TOKEN` | *(未設定)* | 全リクエストに要求する Bearer トークン |
| `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP` | `false` | HTTP トランスポートを認証なしで動かす |
| `CPERSONA_ACL_FILE` | *(未設定)* | クライアント別ケーパビリティ: 名前つき Bearer トークンにエージェント別の read/write 権限を与え、既定は拒否 ([ACL 設計](ACL_DESIGN.md)) |
| `CPERSONA_OAUTH_RESOURCE` | *(未設定)* | RFC 9728 metadata で公開し、クライアントから返されることを期待する正規のリソース識別子。空の間は discovery は無効のまま ([OAuth 設計](OAUTH_DESIGN.md)) |
| `CPERSONA_OAUTH_AUTHORIZATION_SERVERS` | *(未設定)* | クライアントが認証しに行くべき issuer URL。空白またはカンマ区切り。1 つも列挙されていない間は discovery は無効のまま |
| `CPERSONA_OAUTH_SCOPES` | *(未設定)* | 401 と `scopes_supported` で広告する scope。クライアントは要求されたとおりの scope を返してきて、authorization server は自分が定義しない scope を `invalid_scope` で拒否する — issuer が定義する scope だけを広告すること |
| `CPERSONA_OAUTH_JWKS_URI` | *(未設定)* | このサーバーが metadata を読めない provider のための、issuer の署名鍵の所在。通常は issuer 自身の metadata から解決される。authorization server がちょうど 1 つのときだけ有効 |
| `CPERSONA_ALIAS_LEDGER_FILE` | DB と同じ場所の `alias_ledger.json` | subject 別 alias 台帳の置き場所 — `"per_subject": true` の行の背後にある、サーバーが書く `(issuer, subject) → alias` の対応表 ([OAuth 設計 §12](OAUTH_DESIGN.md))。運用者が所有する ACL ファイルと違い、これはサーバーが書くため既定でデータベースの隣に置かれます |
| `CPERSONA_HTTP_MAX_BODY_BYTES` | `4194304` | リクエストボディ 1 本あたりの予算 (バイト)。`Content-Length` を読むのではなく、到着したバイトを数えます |
| `CPERSONA_HTTP_BODY_LIMIT_MODE` | `warn` | 予算を超えたときの扱い: `warn` は記録した上でそのまま応答し、`reject` は 413 を返して読み取りを止め、`off` は計測自体を無効にします |
| `CPERSONA_EXTERNAL_CONTEXT_MODE` | `warn` | `recall_with_context` のエントリで、宣言されたフィールドが文字列でなかったときの扱い: `warn` はそのフィールドを「無かったもの」として読み、該当エントリを `context_field_issues` に載せます。`reject` は呼び出し自体を拒否し、`off` は安全な読みは保ったまま報告だけを外します |
| `CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS` | `300` | 呼び出し側が指定した `timestamp` が、このサーバーの時計よりどれだけ先を指してよいか。これを超えたものを書き込み seam が「誤り」と判定します |
| `CPERSONA_FUTURE_TIMESTAMP_MODE` | `warn` | 許容量を超えた stamp の扱い: `warn` は行をそのまま格納したうえで、ログと書き込み自身の応答に報告します。`reject` は書き込みを拒否し、`off` は黙って格納します |

**ボディ予算は「測る」ものであって、まだ「拒否する」ものではありません。**
このサーバーの他の上限 (`CPERSONA_MAX_CONTENT_LENGTH` など) は、すべてツール
ハンドラが適用します。ハンドラはボディを全部受信して parse し終えた後に動くので、
それらが縛るのは「何が保存されるか」だけです。「到着させるのにいくらかかるか」は
縛っていません。

`CPERSONA_HTTP_MAX_BODY_BYTES` はバイトが現れる場所、すなわちサーバーが実際に
受け取ったチャンクの合計で数えます。`Content-Length` を持たない分割送信も、
`Content-Length` が実際より小さいボディも、どちらも到着した実量で測られます。
既定の 4 MiB は、このサーバーが受理しうる最大の `store` のおよそ 29 倍、200 ターンの
会話を載せた `recall_with_context` のおよそ 10 倍なので、通常のトラフィックが
近づくことはありません。

既定のモードが `warn` なのは意図的です。リクエストはそのまま完全に処理され、
超過だけがログに出ます (1 回目・10 回目・100 回目に出るので、埋もれることも消える
こともありません)。

あなたのペイロードが実際にどんな分布なのかを、このプロジェクト側は知りません。
誰も測っていないうちに拒否する上限は、当て推量で決めた上限です。まず既定のまま
動かしてログを読み、その数値が自分のトラフィックに合うと確認できてから
`CPERSONA_HTTP_BODY_LIMIT_MODE=reject` を設定してください。どちらの経路も
テスト済みです。強制の有効化は設定の変更であって、コード経路の追加ではありません。

**コンテキストのエントリは、自分の形を宣言するようになりました。**
`recall_with_context` の `external_context` の各エントリは、文字列のフィールドを
5 つ (`role` / `content` / `name` / `user_id` / `timestamp`) 宣言します。2.5.12 まで
schema が名前を挙げていたのは最初の 2 つだけでした。

残る 3 つはずっと読まれていたので、schema を見て実装した呼び出し側には `timestamp`
が参照されていること自体を知る手立てがありませんでした。`timestamp` を付けずに
送られたエントリは、日付を持つすべてのメッセージより前に並ぶ「日付なしグループ」に
入っていました。

値はあるが文字列でないフィールドは、そのフィールドが意味しうる何かを指していない
ので、「無かったもの」として読み、エントリはそのフィールド抜きで併合されます。
応答には `context_field_issues` が付き、該当エントリの index とフィールド名を
挙げるので、黙って吸収されることはありません。

そういう呼び出しを拒否したい場合は `CPERSONA_EXTERNAL_CONTEXT_MODE=reject` を
設定してください。既定を `warn` のままにしてあるのは、規則を初めて明文化した
リリースで、今日動いているペイロードが動かなくなるべきではないからです。schema が
**宣言していない**フィールドは、これまでどおり受理して無視します。独自の管理情報を
一緒に載せている呼び出し側は、そのまま動き続けます。

**時計より先の timestamp は、小さな誤差ではなく 1 つの主張です。** `store` は
呼び出し側が指定した `timestamp` をそのまま受け取り、confidence の曲線は
`max(0, now - timestamp)` を読みます。つまり未来の stamp を持つ行は「たった今
書かれた行」として採点され、しかも翌日もまだ未来なので**減衰しません**。

より大きいのは周囲への影響です。コーパスの span が減衰*率*を scale するため、
2099 と刻まれた 1 行だけで 3 週間分のコーパスが 70 年幅に広がり、その scope の
他のすべての行について時間軸が平坦化します。

許容量があるのは、呼び出し側の時計がこのサーバーの時計ではないからです。stamp は
別のホストで生成され、ネットワークを 1 往復してから届くので、正しいクライアントでも
サーバーが読む瞬間より少し先の時刻を名乗り得ます。
`CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS` は、それと「単に間違っている stamp」とを
分ける線です。ちょうど `now + 許容量` は受理され、それを超えたものだけが
報告されます。

既定が `warn` なのは、ボディ予算と同じ理由です。行はこれまでどおり格納され、
書き込みの応答が `timestamp_ahead_of_clock` を伴い、ログが変更すべき設定名を
示します。書き込み自体を拒否させたい場合は `CPERSONA_FUTURE_TIMESTAMP_MODE=reject`
を設定してください。どちらの経路もテスト済みです。強制の有効化は設定の変更であって、
コード経路の追加ではありません。

この設定の外に置いてあるものが 2 つあります。

復元 (`import_memories`) は、どのモードでも該当行を**報告したうえで取り込みます**。
export したものはそのまま戻せなければならず、ここで拒否すると、まさに点検したい行に
ついてラウンドトリップが不可逆になるからです。

読めない stamp は、この finding の担当ではありません。その行は `invalid_timestamp`
が持ちます。

すでに格納済みの行については `check_health` が `future_timestamp` を報告し、
`fix=true` が各行を自身の `created_at` (その行が正直に保持している挿入時刻) から
復元します。locked な行には触れません。

**discovery は明示的に有効化するまで無効です。** OAuth に対応したクライアントは
RFC 9728 の metadata を探し、見つからなければ人間に client id を手で入力させる段まで
落ちます。発見すべきものを与えられなかったクライアントとしては正しい挙動ですが、
資格情報が壊れていると誤読されがちです。

`CPERSONA_OAUTH_RESOURCE` **と** `CPERSONA_OAUTH_AUTHORIZATION_SERVERS` に最低 1 件を
設定すると metadata が公開され、401 に `resource_metadata` と `scope` が乗ります。
どちらかが未設定の間は、この機能を持たないビルドとバイト単位で同一の応答になります。
つまり有効化は意図的な操作であって、アップグレードの副作用では起きません。

**同じ 2 つの設定がトークンの受理も有効にし、そちらには `CPERSONA_ACL_FILE` が
要ります。** 列挙した issuer が署名し、設定した resource ちょうど宛てに発行した
トークンは、client 識別子 `oauth:<issuer>:<client_id>` へ解決されます。grant を書く
相手はこれです。別の resource 宛てのトークンは拒否されます。これは MCP SDK が
resource server 側に残している検査です。

検証に ACL モードが要るのは、grant テーブルの無い検証済み identity が、すべての
ツールへ届いてしまうからです。ACL ファイルが無い場合、サーバーは検証を off のままに
するとログに書き、discovery は提供し続けるので、クライアントは issuer を見つけた
うえで拒否されます。

grant は client ごとです。誰かが行を足すまで、新しく接続したクライアントは認証を
通り、スコープを持つツールはすべて拒否し、grant テーブルにその行が無いことを `detail` に示します。

**ループバックへの bind はセキュリティ境界ではありません。** トンネル
(cloudflared / ngrok)、リバースプロキシ、`kubectl port-forward`、公開された
コンテナポートは、いずれも `127.0.0.1` に転送します。つまり「ループバックに
bind した」ことは、誰が到達できるかについて何も語りません。到達できる者には
全ツールが露出します。`delete_agent_data` も、ファイルを読み書きする
`export_memories` / `import_memories` も含めてです。あなただけが話しかけられる
とは言えないプロセスなら、`CPERSONA_AUTH_TOKEN` を設定してください。

v2.5.3 以降、サーバーはこれを強制します。`CPERSONA_TRANSPORT=streamable-http`
かつ `CPERSONA_AUTH_TOKEN` 未設定なら、**起動を拒否**します。

**2.5.2 以前からアップグレードし、トークンなしで HTTP トランスポートを動かして
いる場合、サーバーは起動しません。** `CPERSONA_AUTH_TOKEN` を設定するか、本当に
認証なしでよいことを `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` で宣言してください
(ローカル開発限定)。それ以前の版は認証なしのループバック bind を許し、
「ループバックのみに bind した」とログに出していました。これは安全宣言のように
読めますが、安全宣言ではありませんでした。

`CPERSONA_ACL_FILE` の設定は、同じ要件を別の方法で満たします。全リクエストが
名前つきクライアントに解決されなければならないため、単一トークンの検査は適用
されません。

このモードでは `CPERSONA_AUTH_TOKEN` は**無視されます** (起動時に警告)。従来の
トークンを使い続けるべきクライアントは、ACL ファイルに明示的に列挙する必要が
あります。権限モデル・ファイル形式・ツール分類は [ACL 設計](ACL_DESIGN.md) を
参照してください。

## recall の融合モード (`CPERSONA_RECALL_MODE`) { #recall-fusion-mode-cpersona_recall_mode }

- **`rrf`** (既定) — Reciprocal Rank Fusion。vector と FTS のチャネルを
  **順位のみ**で融合します。頑健でスケール非依存ですが、スコアの大きさは捨てます。
- **`rsf`** — Relative Score Fusion。各チャネルの生スコア (vector はコサイン、
  keyword は bm25) をクエリ単位で min-max 正規化して加算するため、keyword
  チャネルの bm25 の大きさが融合後も残ります。**話題ドリフトが起きやすい文脈や、
  分かち書きのない言語 (日本語など) で推奨**です。`rrf` が平坦化してしまう
  その大きさこそが、そこでの識別シグナルだからです (≈ Weaviate の
  `relativeScoreFusion`。ClotoCore の `RECALL_CONTAMINATION_AB_2026-06-14`
  レポート §10–12 を参照)。

  この正規化の代償に注意してください。各チャネルの最下行は 0.0 に固定され、
  候補が 1 件しかないチャネルではその行が 1.0 に固定されます。つまり融合スコアが
  表すのは「クエリへの類似度」ではなく「一緒に retrieve された候補の中での位置」
  です。

  autocut はこの固定に対しては働きません。類似度スケールのシグナルにしか発火
  しないためです
  ([契約 §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals))。
  ただし品質ゲートは、依然として融合スコアをコサインスケールの閾値と比較します。
  したがって `CPERSONA_CONFIDENCE_ENABLED=false` (既定であり、
  [CJK の指針](operations.md#japanese-and-cjk-corpora) が前提とする構成) のとき、
  強く一致している行が「強い集合の中で最弱だった」という理由で落ち、逆に弱い単独
  一致が通ることがあります。confidence を有効にするとゲートは confidence スコア
  側に移り、この問題は避けられますが、その代償はすぐ下に書いたとおりです。
  既定は `rrf` のままです。
- **`cascade`** — チャネルを順番に埋める方式 (レガシー)。

**`CPERSONA_CONFIDENCE_ENABLED=true` のとき、融合モードは返却順を決めません。**
融合は「どの候補が結果集合に入るか」を選びます。その後 confidence スコアリングが
集合を並べ直し、品質ゲートも融合スコアではなく confidence を見ます。

1,545 件のコーパスに 394 クエリで実測しました。confidence 有効時、`rsf` と `rrf` は
**394 クエリすべてで同じ行を同じ順序**で返しました。無効時は一致が 10% 未満でした。

融合モードを設定してランキングの変化を期待するなら、confidence は無効のままにするか、
モードが効くのは「どの記憶が考慮されるか」であって「返ってくる順序」ではないと
理解してください。
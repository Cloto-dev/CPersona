<!-- i18n-source: docs/configuration.md@blob:07e1d7c44f7ead9c80cdbede7d5af38f14c3ef04 -->

# 設定リファレンス

> **対象: CPersona {{ version_line }}**。設定はすべて環境変数で、妥当な既定値を持ちます。
> このページが正式な一覧で、サーバーが読む `CPERSONA_*` 変数をすべて載せています。README には
> quick start に必要な部分だけを載せています。
> on/off の設定は `true`・`1`・`yes`・`on` と `false`・`0`・`no`・`off` を大文字小文字を
> 問わず受け付けます。空の値は未設定として扱い、それ以外の値は文書の既定値のまま警告を記録します
> (`CPERSONA_UPDATE_CHECK` だけは、そのような値で off になります)。
>
> **翻訳について**: 原文は英語版です。変数名・既定値・型は原文のまま保持して
> います (訳すと設定できなくなるため)。

## 基本設定 { #core-settings }

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_DB_PATH` | `data/cpersona.db` | SQLite データベースのパス。**クライアントの作業ディレクトリからの相対**なので、セッションをまたいで 1 つの記憶を保つには絶対パスを指定してください |
| `CPERSONA_EMBEDDING_MODE` | `none` | 埋め込みモード: `http` (ローカルの埋め込みサーバー)、`api` (OpenAI 互換エンドポイント。`CPERSONA_EMBEDDING_API_URL` の既定は OpenAI なので、このモードはリクエストごとに課金されます)、`none` のいずれか |
| `CPERSONA_EMBEDDING_URL` | *(未設定)* | 埋め込みサーバーの URL。例: `http://127.0.0.1:8401/embed` |
| `CPERSONA_EMBEDDING_API_URL` | `https://api.openai.com/v1/embeddings` | `api` モードが送信するエンドポイント。OpenAI 互換の埋め込みエンドポイントであれば何でも使えます |
| `CPERSONA_EMBEDDING_API_KEY` | *(未設定)* | `api` モードがベアラートークンとして送るキー |
| `CPERSONA_EMBEDDING_MODEL` | `text-embedding-3-small` | `api` モードが要求するモデル。`http` モードはモデル名を送らず、バックエンドが選ぶため、そこでのこの値は保存したものに付けるラベルにすぎず、バックエンドがフィンガープリントを返さないときに使われます (`CPERSONA_EMBEDDING_MODEL_MODE` を参照) |
| `CPERSONA_EMBEDDING_TIMEOUT_SECS` | `30` | 埋め込みリクエスト 1 回に許す秒数 |
| `CPERSONA_EMBEDDING_CACHE_SIZE` | `256` | メモリに保持するクエリの埋め込みの数。同じクエリ文を再び埋め込まずに済みます。キャッシュするのは 1 件だけのリクエストです。負の値は `0` (キャッシュしない) として扱います |
| `CPERSONA_EMBEDDING_CACHE_TTL` | `300` | キャッシュしたクエリの埋め込みを使える秒数 |
| `CPERSONA_EMBEDDING_MODEL_MODE` | `warn` | 保存されたベクトルに、現在と違うモデルのラベルが付いていたときの扱い。ベクトルは、それを作ったものを示すラベル付きで書き込まれます。ラベルは、バックエンドが `GET /capabilities` で返すフィンガープリント (CEmbedding 0.9.0 以降)、それが無ければ `api` トランスポートが送るモデル名か設定したモデル名で、どれも無ければ空です。ラベルを比べるのは、現在の識別子があるとき (フィンガープリントが返ってきたとき、または `api` トランスポートのモデル) だけです。`warn` は別のモデルが書いたベクトルを `check_health` (`embedding_model`) で報告し、recall の結果は何も変えません。`reject` はそのベクトルを問いと比べなくなり、該当する行はキーワードでだけ見つかり、ベクトルの票なしで採点されます。`off` は報告を外します。2.6.3 より前に保存したベクトルにはラベルが無く、数えられません |
| `CPERSONA_VECTOR_SEARCH_MODE` | `local` | ベクトル検索の実行場所 (`local` = プロセス内コサイン、`remote` = 外部委譲)。`remote` は `/index`・`/search`・`/remove`・`/purge` を `CPERSONA_EMBEDDING_URL` と同じ階層のルートへクエリ文字列ごと送るため、その URL は `/embed` で終わる必要があります。それ以外の URL ではリモート呼び出しを行わず、その旨の警告を 1 度だけ出します |
| `CPERSONA_STORE_BLOB` | `true` | `remote` のとき、各埋め込みをデータベースの行にも保持する (ローカル検索では常に保持します)。`false` ではベクトルはリモートの索引にしか無く、`/search` が止まると recall はキーワード経路だけになり、データベースのバックアップにもベクトルが入りません ([バックアップ](operations.md#backup-and-restore)) |
| `CPERSONA_REMOTE_SEARCH_TIMEOUT_SECS` | `5.0` | リモートの `/search` 呼び出しに許す秒数。超えると recall はローカル検索に切り替わります |
| `CPERSONA_REMOTE_INDEX_TIMEOUT_SECS` | `10.0` | 書き込み時のリモート `/index` 送信に許す秒数。送信に失敗しても書き込みは失敗しません |
| `CPERSONA_FTS_ENABLED` | `true` | 起動時に全文 (キーワード) 索引を作る。`false` では索引を持たないデータベースに索引を作らないため、その上の recall にはキーワード経路がありません。`true` に戻すと、次の起動で索引を作り、保存済みの行から埋めます。すでにある索引を消すことはありません |
| `CPERSONA_LOGICAL_DIRECTORY` | `false` | 2.6 系での試験。`true` にすると、`list_memories` が `path` を受け取り、記録を読み取り専用のフォルダの階層として 1 段ずつ返す。階層は エージェント、project、channel、記録の時刻の UTC の年と月の順で、各フォルダに記憶とエピソードの件数を、月のフォルダに ref を出す。保存も移動も絞り込みもせず、想起は変わらない。`false` ではツールの定義も応答も今までどおり |
| `CPERSONA_DECLARED_FOLDERS` | `false` | `true` にすると、エージェントが project の下にフォルダを宣言し、各記憶とエピソードをそのどれか 1 つに入れられる。`store` が `folder` を受け取り、`organize_folders` ツールがフォルダの作成・名前の変更・移動・削除と、記録の出し入れをする。`list_memories` は `view: "folders"` で、`path` ([エージェント, project, フォルダ, ...]) のフォルダを開く。`recall` と `reconstruct` は `include_folders: true` で各記録のフォルダを返す。フォルダは答えを切り詰めた後で足し、mode の上限の外に置く (`folder_tokens`)。フォルダは分類するだけで絞り込まない。どの検索もフォルダを読まず、記録の行は書き換えないので、ロックした記録も入れられる。`check_health` に `folder_unfiled` (info) と `folder_integrity` が加わる。`false` ではどのツールの定義も応答も今までどおりで、2 つのテーブルはどちらでも作られ、空のまま |
| `CPERSONA_DASHBOARD` | `true` | ツールとともに非推奨で、ツールは 2.7 で外す (`cpersona dashboard` なら、エージェントなしでダッシュボードを開ける)。`open_dashboard` ツールを登録する。エージェントはこのツールで、このサーバーが使っているデータベースの読み取り専用の[ダッシュボード](operations.md#the-dashboard)を開き、人にアドレスを渡す。既定では stdio のトランスポートの時だけ登録する。HTTP ではページがサーバー自身のループバックで開き、人の手が届かないので、`CPERSONA_DASHBOARD_REMOTE` が on の時だけ登録する。`false` でツールの定義を消す。`cpersona dashboard --db <path>` はどちらでも使える |
| `CPERSONA_DASHBOARD_REMOTE` | `false` | `true` で、HTTP のトランスポートが `/dashboard/` の下で[ダッシュボード](operations.md#the-dashboard)を配る。ページは、`open_dashboard` が認証を通ったクライアントに返す 1 回限りのリンクで開き、そのクライアントが読めるエージェントの分だけを見せる。`CPERSONA_DASHBOARD_PUBLIC_URL` が要る。stdio では効かない |
| `CPERSONA_DASHBOARD_SESSION_KEY` | *(未設定)* | リモートのダッシュボードのセッションの Cookie に署名する秘密の値。未設定なら、初めて使う時に、サーバーがデータベースの横のファイル (`<名前>-dashboard.key`、所有者だけが読める) に作る。この値を変えるか、そのファイルを消すと、すべてのセッションが終わる |
| `CPERSONA_DASHBOARD_PUBLIC_URL` | *(未設定)* | 人がこのサーバーに届くアドレス。リモートのダッシュボードのリンクはこのアドレスで始まる (例: `https://memory.example.com`)。ダッシュボードのセッションの Cookie は Secure なので、https か、localhost の http でなければならない |
| `CPERSONA_DASHBOARD_STORES_PATH` | `~/.cpersona/` の `stores.json` | サーバーが起動した時に、開いたデータベースの場所を書き足す一覧。端末やショートカットから実行した `cpersona dashboard` は、ここから最後に使われたデータベースを開く。書くのはパスと時刻だけで、本人だけが読める。`~` は展開されます |
| `CPERSONA_FOLDER_MAX_DEPTH` | `6` | project の下に置けるフォルダの段の数 (1〜16)。下げても、それより深いフォルダは読めるまま残る。上限より下への新しいフォルダと記録の出し入れは拒まれ、`folder_integrity` が上限を超えたフォルダを挙げる |
| `CPERSONA_RECALL_MODE` | `rrf` | recall の融合戦略 (`rrf` / `rsf` / `cascade`)。後述 |
| `CPERSONA_RECALL_PREVIEW_CHARS` | `500` | プレビュー階層: recall 系ツールが返す本文の最大文字数。`full_content=true` は 1 応答あたり 200,000 文字の予算内で全文を返します (bug-211)。超えた分の行はプレビュー階層に戻り (関連度の高い行から全文で残します、bug-214)、応答に `full_content_budget_chars` が付きます。残りは `get_contents` が自身の 40,000 文字予算で取得します。`0` はプレビュー階層**と両方の予算を**無効にします。無効な階層へ降格すると本文が知らないうちに落ちるため、切り詰めをやめる選択は、どこでも切り詰めないという選択になります。`CPERSONA_RECONSTRUCT_DEFAULT_BUDGET` と `CPERSONA_RECONSTRUCT_MAX_BUDGET` は、未設定ならこの大きさの抜粋 1 つ分まで引き上がります |
| `CPERSONA_RECALL_EXCERPT_CHARS` | `800` | プレビューが本文を切ったとき、recall の行がプレビューの傍らに持つクエリに関連する抜粋。一致したレコードの部分を、ランキング順にこの文字数まで詰め、本文中の順序で並べて示します ([設計](RECALL_PREVIEW_TIER_DESIGN.md#excerpt-26))。全文が示される行 (`full_content=true` で応答の予算内に収まる行を含む) には付きません。`full_content=true` の行のうち予算でプレビューへ切り戻された行には付きます。`0` で無効化され、プレビュー階層が無効なときは常に off です |
| `CPERSONA_RRF_K` | `60` | RRF の平滑化パラメータ |
| `CPERSONA_RECALL_LIBRARY_MAX_LIMIT` | `10000` | ライブラリ層での `limit` と想起深度の上限。recall ツールのスキーマは `limit` を 100 までに制限するので、これが縛るのは、全件順位付けのベンチマークのようにライブラリを直接呼ぶ側です。この上限で下げた `limit` は警告としてログに残ります |
| `CPERSONA_MAX_CONTENT_LENGTH` | `16000` | 記憶 1 件・エピソード 1 件あたりの最大文字数。超過分は切り詰められ、`check_health(fix=true)` は既存行も上限で切るため、**下げると保存済みデータが短くなります**。2.5.4a2 で `2000` から引き上げ。埋め込みウィンドウを超えた本文も、行全体を索引するキーワードチャネル経由では検索できます |
| `CPERSONA_MAX_METADATA_LENGTH` | `8000` | 書き込みの `source` と `metadata` それぞれの、JSON に直列化した最大文字数。超えた書き込みは切り詰めずに拒否します。JSON 文書の一部は JSON 文書ではないためです |
| `CPERSONA_MAX_PROFILE_LENGTH` | `2000` | プロフィール 1 行あたりの最大文字数 (記憶とは別枠)。プロフィールはプレビュー切り詰めの対象外なので、この上限が唯一の歯止めです。ただし、*すべての*応答に注入されるわけではありません。プールが 50 行未満の間は品質ゲートがプロフィール行を落とし、スコア付きの結果で埋まっている場合は `limit` が落とします ([契約 §7](behavior-contracts.md#7-profile-rows-carry-no-score)) |
| `CPERSONA_CONFIDENCE_ENABLED` | `false` | 返す各行に `confidence` の値を含めます。2.6.0a7 からは、`CPERSONA_CONFIDENCE_ORDERING=legacy` でない限り、結果の順序も品質ゲートも決めません ([契約 §2](behavior-contracts.md#2-confidence-scoring-overrides-the-fusion-mode)) |
| `CPERSONA_CONFIDENCE_ORDERING` | `fusion` | `fusion`: confidence は各行の横に返されるだけで、他には何もしません。`legacy`: 2.6.0a7 より前の挙動。confidence on のとき結果を confidence スコアで並べ直し、品質ゲートもそれを見るので、`CPERSONA_RECALL_MODE` は返却順を決めなくなります |
| `CPERSONA_AUTO_CALIBRATE` | `false` | 起動時に自動較正する |
| `CPERSONA_BLOCK_BUILD_ENABLED` | `true` | 各レコードを節に相当する Block へ分け、Block ごとに符号量子化ベクトルを 1 本保存する ([Block による到達](BLOCK_REACH_DESIGN.md))。2.6.0 から既定で on で、on の間は、すでに保存されているレコードに対する上限つきの backfill が Block を作る。`false` は「作るが読まない」ではなく、埋め込み呼び出しも行もキューの仕事も無いという意味で、下の読む側を明示していなければ、それも一緒に off にする |
| `CPERSONA_BLOCK_INDEX` | `true` | Block のアームの行を、SQLite でなくデータベースの横のファイルから読む。答えは同じ ([約束 §3](BLOCK_CANDIDATES_CONTRACT.md#3-decisions)、[運用](operations.md#the-block-index-file))。2.6.7 から既定で on。実行前に登録した計測で、想起が速くなり答えがすべて変わらなかったため ([結果](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-block-index-file.md))。on の間、サーバーは Block が変わった記録の記録を付け、キューが起動時と Block を書いた後にファイルを作る。想起は変わった記録を SQLite から、それ以外の記録をファイルから読み、ファイルが正確に答えられない状態では今までどおり SQLite を読む。off なら、サーバーは起動時にその記録を止め、そのための書き込みをしない。`CPERSONA_BLOCK_RETRIEVAL_ENABLED` が必要。タスクキューが無い時は `python -m cpersona.block_index build` でファイルを作る |
| `CPERSONA_BLOCK_RETRIEVAL_ENABLED` | `CPERSONA_BLOCK_BUILD_ENABLED` に従う | recall のときに Block 索引を読む ([Block による到達](BLOCK_REACH_DESIGN.md))。読むのは Block のアームと、`reconstruct` が返す引用の両方。引用は一致した Block から取られ、それを支配する連続文脈を伴うか、不完全であると報告される。到達したレコードは**予約**で通す。予約は品質 gate の後に確保された少数の別枠で、その別枠について gate は参照されず、他のどの位置の gate も変わらない。したがって応答は要求された `limit` を**超えて**その別枠の行数まで行を運び、直前のリリースが返した行はすべてそのまま返る。`CPERSONA_BLOCK_BUILD_ENABLED=true` が必要。何も入っていない索引を読む設定は、何もせずに動き続けるのではなく起動時エラーになる。ベクトル検索が remote の構成では効かない (このアームはローカル検索が埋め込んだクエリベクトルで順位付けするが、remote 検索はそれを作らない) |
| `CPERSONA_TASK_QUEUE_ENABLED` | `true` | バックグラウンドタスクキュー (DB 永続・クラッシュ復帰可能) |
| `CPERSONA_TASK_MAX_RETRIES` | `3` | バックグラウンドタスクの試行回数。この回数だけ失敗したタスクは破棄されます |
| `CPERSONA_TASK_RETRY_DELAY` | `30` | 失敗したタスクを再試行するまでキューが待つ秒数。ただし索引や Block の構築が失敗し、他の仕事が待っているときは、その仕事を止めないよう構築をキューの末尾に回します |
| `CPERSONA_SCOPE_STATS_CACHE` | `true` | recall が読むスコープごとの集計 (品質ゲートのプールの件数と、confidence が on のときはコーパスの時間幅) をキャッシュする。このプロセス自身の書き込みは正確に無効化します。`false` では recall のたびに計算し直します |
| `CPERSONA_SCOPE_STATS_TTL_SECONDS` | `60.0` | 同じデータベースファイルへの別プロセスの書き込みの後も、キャッシュした集計を使い続けてよい時間。2 つ目の書き手が動いている共有ファイルでは下げてください。`0` で再利用しません |
| `CPERSONA_RECENT_RECALL_PENALTY` | `0.7` | 直近に想起された記憶へのペナルティ |
| `CPERSONA_RECENT_RECALL_WINDOW_MIN` | `5` | 上記ペナルティの対象時間窓 (分) |
| `CPERSONA_MAX_MEMORIES` | `10000` | ベクトル検索の**走査ウィンドウ** (保存件数の上限ではありません)。ベクトル検索が順位付けする最新の行で、そのため新しさの事前分布でもあります。古いレコードに意味で届かせたいときは、これを広げずに `CPERSONA_VECTOR_REACH` か窓の外の席を設定してください。実測では、10,000 から 200,000 に広げると、窓の内側に答えがある問いで NDCG@10 を 20 ポイント失い、外側で 5 ポイント得ました ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories))。1 未満の値はウィンドウにならないため、`10000` に戻して警告を記録します |
| `CPERSONA_VECTOR_SCAN_CHUNK_ROWS` | `512` | 索引ファイルでなくデータベースを読むベクトル走査が、一度に取る行数。走査のメモリを抑える値で、速さは変わりません。小さい値はスコアの最下位ビットを動かしうるので、既定値のままにしてください |
| `CPERSONA_VECTOR_REACH` | `0` | ベクトル検索が走査ウィンドウの先をどこまで見てよいか (行数)。効果を持たせるには **`CPERSONA_MAX_MEMORIES` より大きくする必要があります**。同値以下 (既定の `0` を含む) では遠方リストは存在せず、追加の処理は一切走りません。大きくすると、2 つの数値の間にある行が**第 2 のリスト**としてランク付けされ、第 1 のリストと並んで融合されます。つまりウィンドウは新しさの事前分布として働き続けたまま、到達範囲だけを独立に伸ばせます。ローカルのベクトル検索と `rrf`/`rsf` の融合モードでのみ有効です ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_VECTOR_FAR_LIMIT` | `0` | その第 2 のリストのうち何行を融合層に渡すか。`0` (既定) は**応答の `limit` と同じ**という意味で、この設定なしで構築される第 2 のリストそのものです。正の値を与えると `min(limit, N)` 行に切り詰められます。これは候補件数の上限であり、行のスコア計算は一切変わりません。したがって残るのは、フル長のリストが先頭に並べていた行そのものです。`CPERSONA_VECTOR_REACH` が `CPERSONA_MAX_MEMORIES` より大きくない限り無関係で、第 1 のリスト側の打ち切りは `limit` のままです ([契約 §4](behavior-contracts.md#4-the-vector-scan-window-cpersona_max_memories)) |
| `CPERSONA_FAR_SEATS_ENABLED` | `false` | 窓の外の席 ([binary coarse search](BINARY_COARSE_SEARCH_DESIGN.md#5-far-seats))。**走査の窓より外**にあり、1 ビットの粗探索が意味で見つけた記録のために、block の予約の後ろに 2 席を確保します。探索は `CPERSONA_MAX_MEMORIES` と `CPERSONA_VECTOR_REACH` が終わる位置から始まり、上位の候補は保存されたベクトルの cosine で並べ直され、ベクトルのアームの類似度の下限を満たすものだけが残ります。block の予約と同じく、この席は何も押し出しません。席がない時に返していた行はすべて同じ順で返り、応答は `limit` を**超えて**最大 2 行を運びます。席に入った行は `match_reason.signal` = `far` を報告し、その記録の recall count には加算されません。`python -m cpersona.vector_index build` が連続索引の隣に書く粗探索の索引は、探索を安くするだけで結果を変えません。索引がなければ保存されたベクトルを読みます。off では探索は走りません。ベクトル検索がリモートの構成では効果がなく、エピソードは探索しません |
| `CPERSONA_CUE_COARSE_ENABLED` | 未設定 (`auto`) | 時間の手がかりの残り ([binary coarse search](BINARY_COARSE_SEARCH_DESIGN.md#6-the-cue-arms-remainder)): 時間の手がかりのベクトル側は、期間内のレコードのうち最も新しく保存された `CPERSONA_MAX_MEMORIES` 件までしか並べません。残りを探す時は、**期間の残り**を窓の外の席と同じ 1 ビットの粗探索で探し、保存済みベクトルの cosine で並べ直し、同じ類似度の下限を当てて、上限内のリストと cosine で併合します。動き方は 3 つです ([設定](BINARY_COARSE_SEARCH_DESIGN.md#7-settings))。未設定・空・`auto` (2.6.4 からの既定): 答えられる粗探索の索引がある時だけ探します。無ければ探さず、recall は `false` の時と同じになり、応答の `time_cue.remainder` が期間を全部は探していないことを伝えます。`true`: 索引で探し、無ければ窓より後ろの保存済みベクトルを全部読みます。見つかるレコードは同じで、費用がストアの大きさに比例して増えます。`false` とそれ以外の値: 探索は走りません。上限以下のレコードしか持たない期間では、どの動き方でも答えは同じです。変わるのは手がかりが見つけられるレコードであって、手がかりが行を動かす量、席の埋め方、広げ方は変わりません。ベクトル検索がリモートの構成では効果がなく、episode は探しません |
| `CPERSONA_RECALL_DEPTH_FLOOR` | `0` | 想起深度: 応答の `limit` にかかわらず、各検索経路が融合に渡す候補数の下限です。深度は `max(limit, この値)` で、`CPERSONA_RECALL_LIBRARY_MAX_LIMIT` が上限です。`0` では `limit` と等しく、2.5 系の結合を維持します。設定しない限り順位は変わりません。`limit` を超える場合、応答の `depth` で候補の深さを確認できます。融合モードのみ有効です。`cascade` は段階的に `limit` 件を埋めるため対象外です ([設計](RELIABLE_RECALL_2_6.md#4-depth-is-not-count)) |
| `CPERSONA_RECALL_PROPAGATION_SEAT` | `false` | 関連の別枠: `recall` の答えの後ろに 1 行ぶんの場所を保持し、同じ問いを想起深度 100 でもう一度順位付けしたとき、その深い順での位置と答えの 1 行目への近さで最もよい行を置きます。何も押し出さないため、`recall` は `limit` より 1 行多く返ることがあり、その行には `match_reason.signal: "propagation"` が付きます。別枠を使う recall ごとに順位付けがもう 1 回加わります。`recall` のみ (`recall_with_context` と `reconstruct` は対象外)、融合モードのみ、空の問いは対象外です ([設計](RELIABLE_RECALL_2_6.md#the-propagation-seat)) |
| `CPERSONA_RECALL_CUE_TIME_LIMIT_MS` | `1000` | 時期の手がかりの広げ直し (`time_cue` を渡した `recall` / `reconstruct`): 手がかりの期間に何も無いとき、期間を 1 回だけ広げて探し直します。ただし recall が既にこのミリ秒数かかっていれば行いません。上限で止まったことは想起の記録に残ります ([設計](RECALL_PROCESS_DESIGN.md#25-one-revision)) |
| `CPERSONA_AUTOCUT_ENABLED` | `true` | autocut: 結果のリストを、スコアの差が最も大きいところで切る。発火できるかどうかは融合モードで決まります (`CPERSONA_AUTOCUT_MIN_RESULTS` を参照) |
| `CPERSONA_AUTOCUT_MIN_GAP_RATIO` | `0.15` | autocut が切れ目とみなす最小の差 (先頭スコアに対する割合)。どの差もこれに届かなければ、リストはそのまま残ります |
| `CPERSONA_AUTOCUT_MIN_RESULTS` | `3` | この件数未満の結果集合は autocut されません。autocut が発火するのは類似度スケールのシグナル、つまり confidence による並べ替えの下 (`CPERSONA_CONFIDENCE_ORDERING=legacy`) か、`cascade` が作る生 cosine だけの均質なリストに対してで、`rsf`/`rrf` では意図して働かせていません ([契約 §6](behavior-contracts.md#6-autocut-fires-only-on-similarity-scale-signals))。したがってこのつまみが働くかどうかを決めるのは融合モードです |
| `CPERSONA_FUSED_GATE_ENABLED` | `true` | 融合後の品質ゲート。無効にするのは最終手段です。フィルタはプール規模のヒューリスティックにフォールバックし、粗くはなりますが弱い一致は依然として弾かれます。失うのは、このコーパスに対して測定された動作点です |
| `CPERSONA_DEGRADED_ADVISORY` | `true` | 埋め込みが利用不能な間、recall 応答に `advisory` を付ける ([runbook](operations.md#detecting-a-dead-embedding-server)) |
| `CPERSONA_UPDATE_CHECK` | `true` | プロセス起動ごとに 1 回 pypi.org を参照し、このサーバーの新しいリリース、または撤回されたリリースを検出して `recall` / `check_health` / `check_update` で報告する ([何を送るか](architecture.md#transports))。`false` で機能全体を無効にします。リクエストもキャッシュファイルも通知もありません。on とも off とも読めない値も、他の on/off の設定のように既定値を取らず、警告を記録して無効化します。どちらの設定でも更新が自動で行われることはありません |
| `CPERSONA_UPDATE_CHECK_INTERVAL_SECONDS` | `86400` | その判定が有効な期間。データベースの隣の `update-check.json` にキャッシュされ、この時間内の再起動ではリクエストが発生しません |
| `CPERSONA_EPISODE_PENALTY_ENABLED` | `false` | エピソード境界ペナルティ ([契約 §3](behavior-contracts.md#3-episode-boundary-penalty)) |
| `CPERSONA_EPISODE_DECAY_RATE` | `0.01` | 境界より前の記憶に対する 1 時間あたりの減衰率 |
| `CPERSONA_EPISODE_DECAY_FLOOR` | `0.5` | ペナルティの下限 (古い記憶でも最大で半分まで) |
| `CPERSONA_PRIOR_FAR_WEIGHT` | `1.0` | far リストの票の重み (`0`〜`1`)。両方の融合に効きます ([一本化した事前分布](PRIOR_FUNCTION_DESIGN.md#2-the-prior))。`CPERSONA_VECTOR_REACH` が窓より大きく設定されている時だけ意味を持ちます。`1` は割り引かない far の票、`0` は両方の融合から far リストを外し、窓と reach の間の領域を走査しません。窓の外の席の開始位置は動かず、reach が終わる位置から始まるままです。席を有効にしている時に reach を切るには `CPERSONA_VECTOR_REACH` を `0` にしてください |
| `CPERSONA_PRIOR_AGE_RATE` | `0` | 経過時間の重み `max(floor, 1 / (1 + age_hours × rate))` の率。品質ゲートが通した行の順序だけを入れ替え、行を通しも除きもしません。`0` で無効 |
| `CPERSONA_PRIOR_AGE_FLOOR` | `0.3` | 経過時間の重みの下限 |
| `CPERSONA_PRIOR_AGE_ANCHOR` | `newest` | 経過時間をどこから測るか。想起のスコープで最も新しい記憶 (`newest`。放置したストアも最後に使った時と同じ順位になる) か、現在時刻 (`now`) |

汎用エイリアス `EMBEDDING_MODE` / `EMBEDDING_HTTP_URL` / `EMBEDDING_MODEL` /
`EMBEDDING_API_URL` / `EMBEDDING_API_KEY` も
受理されます (両方設定されている場合は `CPERSONA_` 接頭辞つきが優先)。
マーケットプレイスのカタログと Quick Start は汎用名を使っています。

## 再構成の件数 { #reconstruction-count }

`reconstruct` の呼び出しごとに `count` を宣言します。上限の対象は**束ねた想起単位**であり、
保存行数や検索深度ではありません。設定と呼び出し引数のどちらにも指定がなければ、上限は **10** です (設定した最大値がそれより小さければその値)。

| 変数 | 既定 | 説明 |
|----------|---------|-------------|
| `CPERSONA_RECONSTRUCT_DEFAULT_COUNT` | `10`。`CPERSONA_RECONSTRUCT_MAX_COUNT` がそれより小さければその値 | 呼び出し側が `count` を省略した時の上限。2.6.0 から 10 で、推奨の構成を計測した件数です。明示的に上限より大きく設定すると起動時のエラーになります |
| `CPERSONA_RECONSTRUCT_FORCED_COUNT` | *(未設定)* | 全呼び出しで要求値と既定値を上書きします。充足目標ではなく上限です |
| `CPERSONA_RECONSTRUCT_MAX_COUNT` | `10` | 絶対上限。超える要求は切り詰めて報告します。`mode: "pro"` では上限は少なくとも 15 で、これは Pro 自身の窓です (Pro はペイロードをトークン数の上限で抑えます)。強制した件数はそのまま効きます。実験的な制限であり、実測で最適とされた件数ではありません |
| `CPERSONA_RECONSTRUCT_ADJACENCY_SECONDS` | `60` | 同じ送り手の候補を、同じ project と channel の中で何秒以内なら 1 つの item に束ねるか。最初の行から測るので、短い間隔が続いても何時間もの会話は 1 つになりません。`0` でこの束ねを無効にします |
| `CPERSONA_RECONSTRUCT_QUOTE_CHARS` | `800` | item の先頭引用: そのレコードのうち一致した部分を、ランキング順にこの文字数まで詰め、本文中の順序で示します。recall の抜粋と同じ詰め方です。これ以下の長さのレコードは全体が引用されます。`0` にすると、2.6 より前と同様に、支配する 1 節をプレビュー階層で切って引用します |
| `CPERSONA_RECONSTRUCT_FULL_QUOTES` | `5` | 先頭から何件の item に、上の先頭引用の大きさをそのまま使うか。2.6.4 から |
| `CPERSONA_RECONSTRUCT_TAIL_QUOTE_CHARS` | `400` | それより後の item の先頭引用の大きさ。未設定なら、より小さい `CPERSONA_RECONSTRUCT_QUOTE_CHARS` に合わせて下がります。明示的に設定した値は 0 より大きく `CPERSONA_RECONSTRUCT_QUOTE_CHARS` 以下でないと起動時に止まります。同じ値にすると、2.6.4 より前と同じくどの item も同じ大きさで引用します。2.6.4 から |
| `CPERSONA_RECONSTRUCT_SEQUENCE` | `items` | 予算が切る引用の列。`items` は各 item の先頭引用を item の順に並べ、そのあと各 item の抜粋を順に回す列です。`evidence` は item の先頭レコードの引用候補をすべて、レコードをまたいだ 1 つの順序に並べて切ります。item の先頭引用はそのレコードのうち切り取りに入った部分 (本文の順に表示) になり、1 つも入らなかった item は返しません ([証拠の配分](EVIDENCE_ALLOCATION_DESIGN.md))。`whole` は同じ順序に長さで引いた床を付けたものです。引用の長さ (`CPERSONA_RECONSTRUCT_QUOTE_CHARS`) 以下の先頭レコードを丸ごと item の順に先に並べ、それより長いレコードの引用候補を `evidence` の順序で続けます。予算を上げて引用が消えることはありません。`CPERSONA_RECONSTRUCT_QUOTE_CHARS` が 0 より大きい時だけ読みます。実験的な設定です。2.6.5a1 から (`whole` は 2.6.5a3 から) |
| `CPERSONA_RECONSTRUCT_DEFAULT_BUDGET` | `4000` | 呼び出し側が `budget` を省略した時のペイロード予算 (引用本文の文字数)。ウィンドウの item の先頭引用の大きさを合計した方が大きければそちらになります。未設定ならプレビュー層の抜粋 1 つ分まで引き上がり、より小さい `CPERSONA_RECONSTRUCT_MAX_BUDGET` に合わせて下がります。件数と予算の掃引で選ぶまでの暫定値です |
| `CPERSONA_RECONSTRUCT_FORCED_BUDGET` | *(未設定)* | 全呼び出しで要求値と既定値を上書きします |
| `CPERSONA_RECONSTRUCT_MAX_BUDGET` | `20000` | 予算の絶対上限。超える要求は切り詰めて報告します。未設定なら、`CPERSONA_RECALL_PREVIEW_CHARS` の方が大きい時にプレビュー層の抜粋 1 つ分まで引き上がります。設定した既定値・強制値がこれを超える場合、または設定した値がプレビュー層の抜粋 1 つ分に満たない場合は起動時に停止します |
| `CPERSONA_RECONSTRUCT_TOP_K` | `20` | 呼び出しが `top_k` を省略したとき、`reconstruct` の下の recall が束ねに渡す候補数。設計上 `count` とは独立で、`CPERSONA_RECALL_LIBRARY_MAX_LIMIT` が上限です |
| `CPERSONA_RECONSTRUCT_MAX_HOPS` | `2` | 呼び出しが `max_hops` を省略したとき、関係をたどる段数。5 を超えると 5 に下げ、応答がそれを伝えます |
| `CPERSONA_RECONSTRUCT_MAX_EVIDENCE` | `40` | 呼び出しが `max_evidence` を省略したとき、item ごとに残す行数。100 を超えると 100 に下げ、応答がそれを伝えます |

優先順位は `forced ?? requested ?? default` で、最大値で制限します。
既定値や強制値が最大値を超える設定は、起動時エラーになります。
`count=5` に対して有効な item が 2 件なら、`effective_count=5`、`returned_count=2` と
`shortfall_reason` を返します。`requested_count` と `count_policy` は、サーバーが件数を
切り詰めた時か強制した時だけ加わり、要求どおりに処理した件数は繰り返しません。件数を埋めるために item を複製したり、
クラスタを分割したり、選択基準を緩めたりしません。強制値を設定しても、この規則は変わりません。
束ねには確定的な出典キーを使い、任意の文章間の意味的同一性は判定しません。

## 較正と品質ゲート { #calibration-and-the-quality-gate }

`calibrate_threshold` は保存されたベクトルを測り、ベクトルのアームのコサイン下限と、
融合後の品質ゲート (`CPERSONA_FUSED_GATE_ENABLED`) の 2 つを設定します。
起動時の較正と `set_recall_precision` も、何も指定しない呼び出しと同じ既定値を使います。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_VECTOR_MIN_SIMILARITY` | `0.3` | 自分用の較正済みしきい値を持たないエージェントに使う、ベクトルのアームのコサイン下限。`agent_id` なしの較正がこれを置き換えます |
| `CPERSONA_RRF_THRESHOLD_FACTOR` | `0.5` | `rrf` と `rsf` では、融合とゲートが後で決めるため、ベクトルのアームは下限にこの値を掛けたところまで行を通します。`cascade` は下限そのものを使います |
| `CPERSONA_CALIBRATE_SAMPLE_SIZE` | `200` | 呼び出しが `sample_size` を指定しないときに抽出する記憶の数。`CPERSONA_CALIBRATE_MAX_SAMPLE` が上限です |
| `CPERSONA_CALIBRATE_METHOD` | `separation` | 呼び出しが `method` を指定しないときのベクトル下限の決め方。`separation` は、無作為な組と、時間的に近く保存された記憶の組を最もよく分けるコサインを選びます。`percentile` は無作為な組の類似度の分位点を、`zscore` はその平均に `CPERSONA_CALIBRATE_Z_FACTOR` 個ぶんの標準偏差を足した値を取ります |
| `CPERSONA_CALIBRATE_PERCENTILE` | `0.95` | 呼び出しが指定しないときに `percentile` が使う分位点。1 を超える値はパーセントとして読み、(0, 100] の外の値は警告を出して `0.95` に戻します |
| `CPERSONA_CALIBRATE_Z_FACTOR` | `1.0` | 呼び出しが指定しないときに `zscore` が使う `z`。±10 に収めます |
| `CPERSONA_CALIBRATE_FLOOR` | `0.05` | 較正が設定してよい最小の値。ベクトル下限にもゲートにも同じく効きます |
| `CPERSONA_CALIBRATE_TEMPORAL_WINDOW_MIN` | `30` | 2 つの記憶を関連ありとみなす間隔 (分)。`separation` とゲートの両方が使います。そうした組が 10 未満なら、ベクトルの較正は抽出した各記憶の最近傍で代えます |
| `CPERSONA_CALIBRATE_ON_MODEL_CHANGE` | `true` | 保存された較正が使えないとき (較正が無い、コーパスの埋め込み次元が変わった、採点のバージョンが変わった) に起動時に較正し直す。これと `CPERSONA_AUTO_CALIBRATE` の両方が off なら、古い較正は適用されず、`calibrate_threshold` を実行するまで `deep_check` がそれを報告します |
| `CPERSONA_RECALL_PRECISION` | `balanced` | 較正した曲線のどこにゲートを置くか: `strict` (無関係な行が減り、取りこぼしが増える)、`balanced`、`lenient` (取りこぼしが減り、無関係な行が増える)。特異度の重みはそれぞれ 2.0・1.0・0.5 です。`set_recall_precision` がエージェントごとに上書きします |
| `CPERSONA_FUSED_GATE_BETA` | `CPERSONA_RECALL_PRECISION` に従う | 名前付きの水準の代わりに使う、特異度の重みの生の値 |
| `CPERSONA_FUSED_GATE_SAMPLE_QUERIES` | `40` | ゲートの較正 1 回の試行で疑似クエリとして使う保存済みの記憶の数。それぞれが recall を 1 回実行します |
| `CPERSONA_FUSED_GATE_CALIBRATION_DRAWS` | `5` | ゲートの較正 1 回あたりの独立な試行数。適用するゲートはその中央値なので、較正 1 回で 試行数 × 疑似クエリ数 の recall を実行します |

## confidence スコア { #confidence-score }

`CPERSONA_CONFIDENCE_ENABLED=true` のとき、返す各行に `confidence` の値が付きます。
`CPERSONA_CONFIDENCE_ORDERING=legacy` では、それが結果の順序と品質ゲートにも使われます。
以下の設定はその値を決めるだけで、それ以外には何も影響しません。

スコアは `sqrt(c × t) × r × p`、コサインを持たない行では `sqrt(t) × r × p` です。
`c` は下の下限と上限の間で正規化したコサイン、`t` は時間減衰、`r` は解決済みの係数、
`p` は直近の想起へのペナルティ (`CPERSONA_RECENT_RECALL_WINDOW_MIN` 以内なら
`CPERSONA_RECENT_RECALL_PENALTY`) です。`deep=true` では `t`・`r`・`p` が 1 になります。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_COSINE_FLOOR` | `0.2` | `c = 0` に対応するコサイン。それより低い値も 0 です |
| `CPERSONA_COSINE_CEIL` | `0.75` | `c = 1` に対応するコサイン。それより高い値も 1 です |
| `CPERSONA_DECAY_RATE` | `0.005` | `t = 1 / (1 + age_hours × rate)`。減衰の下限を下回りません。コーパスの時間幅が `CPERSONA_REFERENCE_HOURS` より長いときは、率を 時間幅 / 基準時間 で割るので、数か月にわたるストアは 1 週間ぶんのストアよりゆっくり減衰します |
| `CPERSONA_DECAY_FLOOR` | `0.3` | 一度も想起されていない行の `t` の最小値 |
| `CPERSONA_DECAY_CEIL` | `0.5` | 想起によってその下限を引き上げられる最大の値 |
| `CPERSONA_RECALL_BOOST` | `0.02` | 想起が下限を引き上げる量: `ln(1 + recall_count) × この値`。最後の想起からの時間とともに薄れます |
| `CPERSONA_BOOST_DECAY_RATE` | `0.002` | その薄れ方: 引き上げ量に `1 / (1 + hours_since_last_recall × rate)` を掛けます |
| `CPERSONA_REFERENCE_HOURS` | `168` | 減衰率を設定どおりに使うコーパスの時間幅 |
| `CPERSONA_MIN_TIME_RANGE_HOURS` | `24` | 率の調整が仮定するコーパスの最短の時間幅 |
| `CPERSONA_RESOLVED_DECAY_FACTOR` | `0.3` | 解決済みとしてアーカイブされたエピソードの `r`。それ以外の行は `r = 1` です |

## コーパス規模の上限 { #corpus-scale-caps }

以下は、コーパスの規模に比例して増える処理 (インデックス保守・health の修復・
較正のサンプリング) を抑える上限です。

いずれも絶対的な行数で、およそ 10,000 行のコーパスを想定して決められています。
その規模では全体を覆いますが、150,000 行のコーパスに対しては同じ数値が「標本」に
なります。上限に当たってもエラーにはならず、単に小さい答えが返ります。症状が出るのを
待つのではなく、意図して引き上げてください。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` | `10000` | ベクトルインデックスが*穴 (hole)* として名前を記録できる行数。ファイルに載せられなかった行 (`created_at` が標準形でない) と、ビルド時点で埋め込みを持たなかった行の合計です。これらは毎クエリで実テーブルから id 指定で読まれます。この数を超えるとインデックスのビルド自体を見送り、recall は (正しいが遅い) 全走査のままです。一括インポートの後、埋め込みの未処理分が片付くまでがこの状態です。既定値は 150,000 行コーパスの 6.7% を覆います。最悪ケース (記録済みの穴すべてが後から埋め込みを得た場合) でもクエリあたり約 65 ms で、次回リビルドが穴を吸収すれば解消します |
| `CPERSONA_REEMBED_ROW_CAP` | `5000` | 1 回の `check_health(fix=true)` が再埋め込みする「埋め込みなし」行数。報告される `repairable` の上限でもあります。埋め込みは書き込みロックを取る前に行われるため、この値が縛るのは prefetch の実時間とロック中の `UPDATE` 件数です。大きな未処理分を少ない回数で片付けたい場合は引き上げてください。旧既定値 500 では 50,000 行のバックログに 100 回の実行が必要で、しかもバックログが `CPERSONA_VECTOR_INDEX_MAX_EXCLUDED_IDS` を超えている間はインデックスもビルドできません |
| `CPERSONA_NEAR_DUPLICATE_ROW_CAP` | `5000` | `deep_near_duplicate` が比較する埋め込み済み行数。この比較はメモリが O(n²) です。1024 次元ベクトルでの実測では、5,000 行でピーク 266 MB / 約 100 ms、10,000 行で 982 MB でした。既定値が大きなコーパス全体を覆わず標本に留めているのは、このためです |
| `CPERSONA_INVALID_SOURCE_CLASSIFY_CAP` | `10000` | 1 回の `check_invalid_source_type` が分類する不正な `source` 行数。コストは行ごとの JSON パース (マイクロ秒オーダー) なので、上の 2 つより大きく取れます。上限を超えると標本が不完全になり、チェックは自身の severity を下げることを見送ります。失われるのは判定であって、正しさではありません |
| `CPERSONA_NORMALIZATION_SCAN_CAP` | `10000` | `deep_unnormalized_content` が 1 回の実行で読む行数。Unicode 正規化は SQL の述語にできない (SQLite に NFC 関数が無い) ので、これは本文を読まないと答えられない唯一の check であり、この上限が「本文を読む」を「コーパス全体を読む」にしないための線です。上限を超えた実行は `complete: false` を返し、下限値を総数と読ませません |
| `CPERSONA_CALIBRATE_MAX_SAMPLE` | `5000` | 呼び出し側が何を要求しても効く `calibrate_threshold` の `sample_size` の上限。near-duplicate と同じ O(n²) の行列を扱い、上限の無い値で、接続を共有するすべてのエージェントの分までメモリを使い切るのを防ぐためにあります。引き上げはマシンが扱える範囲までにとどめます (上記の実測値を参照) |

## エクスポートとインポート { #export-and-import }

`export_memories` と `import_memories` は、サーバーのマシン上のファイルを、
呼び出し側が渡したパスで書き込み・読み込みします。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_EXPORT_DIR` | *(未設定)* | 両ツールのパスが収まらなければならないディレクトリ。設定すると、パスはシンボリックリンクをたどって解決した先がこのディレクトリ自身かその配下のときだけ受け付けられます。名前の先頭がたまたま同じ文字で始まるだけの隣のディレクトリは外側です。未設定のときに拒否されるのは `..` の区切りを含むパスだけで、ACL ファイルがあると両ツールはすべてのエージェントへの権限を求めます ([ACL 設計](ACL_DESIGN.md)) |
| `CPERSONA_MAX_IMPORT_BYTES` | `104857600` | `import_memories` が読むファイルの最大サイズ (バイト、100 MiB)。これより大きいファイルは 1 行も取り込まれる前に拒否され、エラーはこの変数の名前を示します。`export_memories` にはこの上限が無いので、大きなコーパスのエクスポートや `include_embeddings=true` で書いたエクスポートは、インポートが受け付けるより大きくなりえます。そのファイルを復元する前にこの値を引き上げてください |

## 運用コンテキスト { #operating-context }

運用コンテキストは運用者が管理するファイルで、接続時にすべてのクライアントへ送る指示、
有効な `project_id` の一覧、`@auto` の既定値を持ちます ([設計](OPERATING_CONTEXT_DESIGN.md))。
サーバーは必要なときに読むため、編集は再起動なしで反映されます。ファイルが無い、または読めないときは機能は休止し、
解析できないファイルは `check_health` が報告します。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_OPERATING_CONTEXT` | *(未設定)* | `off` (大文字小文字を問わない) で、ファイルの有無にかかわらず機能を止めます。それ以外の値では有効のままです |
| `CPERSONA_OPERATING_CONTEXT_PATH` | `~/.cpersona/` の `operating-context.toml` | 読むファイル。`~` は展開されます |

## リモート (HTTP) トランスポート { #remote-http-transport }

既定は stdio で、MCP クライアントがプロセスを所有し、ネットワークは介在しません。
`CPERSONA_TRANSPORT=streamable-http` を設定すると HTTP で配信します。1 つの
サーバーを複数クライアントで共有し、ネットワーク越しに到達できます。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_TRANSPORT` | `stdio` | `stdio`、または HTTP 配信の `streamable-http`。空の値は未設定とは読まず、起動時に拒否します。stdio はクライアントがつながっていないとエラーを出さずにすぐ終了し、コンテナイメージの既定は `streamable-http` なので、そこで空を渡すとサーバーがエラーを出さずに止まるためです |
| `CPERSONA_HTTP_HOST` | `127.0.0.1` | バインドアドレス |
| `CPERSONA_HTTP_PORT` | `8402` | バインドポート |
| `CPERSONA_AUTH_TOKEN` | *(未設定)* | 全リクエストに要求する Bearer トークン |
| `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP` | `false` | HTTP トランスポートを認証なしで動かす |
| `CPERSONA_ACL_FILE` | *(未設定)* | クライアント別ケーパビリティ: 名前つき Bearer トークンにエージェント別の read/write 権限を与え、既定は拒否 ([ACL 設計](ACL_DESIGN.md)) |
| `CPERSONA_OAUTH_RESOURCE` | *(未設定)* | RFC 9728 metadata で公開し、クライアントから返されることを期待する正規のリソース識別子。空の間は discovery は無効のまま ([OAuth 設計](OAUTH_DESIGN.md)) |
| `CPERSONA_OAUTH_AUTHORIZATION_SERVERS` | *(未設定)* | クライアントが認証しに行くべき issuer URL。空白またはカンマ区切り。1 つも列挙されていない間は discovery は無効のまま |
| `CPERSONA_OAUTH_SCOPES` | *(未設定)* | 401 と `scopes_supported` で広告する scope。クライアントは要求されたとおりの scope を返してきて、authorization server は自分が定義しない scope を `invalid_scope` で拒否する。issuer が定義する scope だけを広告すること |
| `CPERSONA_OAUTH_JWKS_URI` | *(未設定)* | このサーバーが metadata を読めない provider のための、issuer の署名鍵の所在。通常は issuer 自身の metadata から解決される。authorization server がちょうど 1 つのときだけ有効 |
| `CPERSONA_ALIAS_LEDGER_FILE` | DB と同じ場所の `alias_ledger.json` | subject 別の alias 台帳の置き場所。`"per_subject": true` の行の背後にある、サーバーが書く `(issuer, subject) → alias` の対応表 ([OAuth 設計 §12](OAUTH_DESIGN.md))。運用者が所有する ACL ファイルと違い、これはサーバーが書くため既定でデータベースの隣に置かれます |
| `CPERSONA_HTTP_MAX_BODY_BYTES` | `4194304` | リクエストボディ 1 本あたりの予算 (バイト)。`Content-Length` を読むのではなく、到着したバイトを数えます |
| `CPERSONA_HTTP_BODY_LIMIT_MODE` | `warn` | 予算を超えたときの扱い: `warn` は記録した上でそのまま応答し、`reject` は 413 を返して読み取りを止め、`off` は計測自体を無効にします |
| `CPERSONA_EXTERNAL_CONTEXT_MODE` | `warn` | `recall_with_context` のエントリで、宣言されたフィールドが文字列でなかったときの扱い: `warn` はそのフィールドを「無かったもの」として読み、該当エントリを `context_field_issues` に載せます。`reject` は呼び出し自体を拒否し、`off` は安全な読みは保ったまま報告だけを外します |
| `CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS` | `300` | 呼び出し側が指定した `timestamp` が、このサーバーの時計よりどれだけ先を指してよいか。これを超えたものを、書き込みの処理が「誤り」と判定します |
| `CPERSONA_FUTURE_TIMESTAMP_MODE` | `warn` | 許容量を超えた stamp の扱い: `warn` は行をそのまま格納したうえで、ログと書き込み自身の応答に報告します。`reject` は書き込みを拒否し、`off` は報告せずに格納します |

**ボディ予算は「測る」ものであって、まだ「拒否する」ものではありません**。
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
超過だけがログに出ます (1 回目・10 回目・100 回目・1,000 回目と、桁が上がるごとに
出るので、埋もれることも消えることもありません)。

あなたのペイロードが実際にどんな分布なのかを、このプロジェクト側は知りません。
誰も測っていないうちに拒否する上限は、当て推量で決めた上限です。まず既定のまま
動かしてログを読み、その数値が自分のトラフィックに合うと確認できてから
`CPERSONA_HTTP_BODY_LIMIT_MODE=reject` を設定してください。どちらの経路も
テスト済みです。強制の有効化は設定の変更であって、コード経路の追加ではありません。

**コンテキストのエントリは、自分の形を宣言するようになりました**。
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
挙げるので、知らないうちに吸収されることはありません。

そういう呼び出しを拒否したい場合は `CPERSONA_EXTERNAL_CONTEXT_MODE=reject` を
設定してください。既定を `warn` のままにしてあるのは、規則を初めて明文化した
リリースで、今日動いているペイロードが動かなくなるべきではないからです。schema が
**宣言していない**フィールドは、これまでどおり受理して無視します。独自の管理情報を
一緒に載せている呼び出し側は、そのまま動き続けます。

**時計より先の timestamp は、小さな誤差ではなく 1 つの主張です**。`store` は
呼び出し側が指定した `timestamp` をそのまま受け取り、confidence の曲線は
`max(0, now - timestamp)` を読みます。つまり未来の stamp を持つ行は「たった今
書かれた行」として採点され、しかも翌日もまだ未来なので**減衰しません**。

より大きいのは周囲への影響です。コーパスの時間幅に合わせて減衰*率*を調整するため、
2099 と記録された 1 行だけで 3 週間分のコーパスが 70 年の幅に広がり、その scope の
ほかのすべての行について時間軸が平らになります。

許容量があるのは、呼び出し側の時計がこのサーバーの時計ではないからです。stamp は
別のホストで生成され、ネットワークを 1 往復してから届くので、正しいクライアントでも
サーバーが読む瞬間より少し先の時刻を示し得ます。
`CPERSONA_FUTURE_TIMESTAMP_SKEW_SECONDS` は、それと「単に間違っている stamp」とを
分ける線です。ちょうど `now + 許容量` は受理され、それを超えたものだけが
報告されます。

既定が `warn` なのは、ボディ予算と同じ理由です。行はこれまでどおり格納され、
書き込みの応答が `timestamp_ahead_of_clock` を伴い、ログが変更すべき設定名を
示します。書き込み自体を拒否させたい場合は `CPERSONA_FUTURE_TIMESTAMP_MODE=reject`
を設定してください。どちらの経路もテスト済みです。強制の有効化は設定の変更であって、
コード経路の追加ではありません。

`archive_episode` も、エピソードの開始・終了として保持する history の timestamp に
同じ規則を適用します。エピソードはその時刻で採点され、並べられるからです。許容量、
モード、ログは store と同じで、`warn` では応答が `timestamp_ahead_of_clock` を伴い、
`reject` ではアーカイブを拒否します。

この設定の外に置いてあるものが 2 つあります。

復元 (`import_memories`) は、どのモードでも該当行を**報告したうえで取り込みます**。
`future_timestamps` が記憶の件数を、`future_episode_timestamps` が開始か終了が先に
あるエピソードの件数を数えます。export したものはそのまま戻せなければならず、ここで拒否すると、まさに点検したい行に
ついてラウンドトリップが不可逆になるからです。

読めない stamp は、この finding の担当ではありません。その行は `invalid_timestamp`
が持ちます。ここでの「読めない」は、この列を読むどの読み手もその時刻を決められない
という意味です。SQLite の日付関数だけが読める表記 (`2500000` のようなユリウス日、
2 月 30 日、小文字の `z`) は、SQLite の読み方で判定します。`future_timestamp` の
チェックも時刻フィルタもそう読むからです。`2500000` はそれらにとって 2132 年なので、
`reject` はこれを拒否します。そうした表記でも時計より先でないもの (たとえば `now`)
は引き続き `invalid_timestamp` の担当で、`fix=true` がその行の `created_at` に
置き換えます。

すでに格納済みの行については `check_health` が `future_timestamp` を報告し、
`fix=true` が各行を自身の `created_at` (その行が正直に保持している挿入時刻) から
復元します。locked な行には触れません。エピソードは `future_episode_timestamp` の
担当で、`fix=true` は先にある開始・終了を、同じようにそのエピソード自身の
`created_at` から復元します。

**discovery は明示的に有効にするまで無効です**。OAuth に対応したクライアントは
RFC 9728 の metadata を探し、見つからなければ人間に client id を手で入力させる段階に
移ります。発見すべきものを与えられなかったクライアントとしては正しい挙動ですが、
資格情報がおかしいと誤解されがちです。

`CPERSONA_OAUTH_RESOURCE` **と** `CPERSONA_OAUTH_AUTHORIZATION_SERVERS` に最低 1 件を
設定すると metadata が公開され、401 に `resource_metadata` と `scope` が乗ります。
どちらかが未設定の間は、この機能を持たないビルドとバイト単位で同一の応答になります。
つまり有効化は意図的な操作であって、アップグレードの副作用では起きません。

**同じ 2 つの設定がトークンの受理も有効にし、そちらには `CPERSONA_ACL_FILE` が
要ります**。 列挙した issuer が署名し、設定した resource ちょうど宛てに発行した
トークンは、client 識別子 `oauth:<issuer>:<client_id>` へ解決されます。grant を書く
相手はこれです。別の resource 宛てのトークンは拒否されます。これは MCP SDK が
resource server 側に残している検査です。

検証に ACL モードが要るのは、grant テーブルの無い検証済み identity が、すべての
ツールへ届いてしまうからです。ACL ファイルが無い場合、サーバーは検証を off のままに
するとログに書き、discovery は提供し続けるので、クライアントは issuer を見つけた
うえで拒否されます。

grant は client ごとです。誰かが行を足すまで、新しく接続したクライアントは認証を
通り、スコープを持つツールはすべて拒否し、grant テーブルにその行が無いことを `detail` に示します。

**ループバックへの bind はセキュリティ境界ではありません**。 トンネル
(cloudflared / ngrok)、リバースプロキシ、`kubectl port-forward`、公開された
コンテナポートは、いずれも `127.0.0.1` に転送します。つまり「ループバックに
bind した」ことは、誰が到達できるかについて何も示しません。到達できる者には
全ツールが露出します。`delete_agent_data` も、ファイルを読み書きする
`export_memories` / `import_memories` も含めてです。あなただけが話しかけられる
とは言えないプロセスなら、`CPERSONA_AUTH_TOKEN` を設定してください。

v2.5.3 以降、サーバーはこれを強制します。`CPERSONA_TRANSPORT=streamable-http`
かつ `CPERSONA_AUTH_TOKEN` 未設定なら、**起動を拒否**します。

**2.5.2 以前からアップグレードし、トークンなしで HTTP トランスポートを動かして
いる場合、サーバーは起動しません**。`CPERSONA_AUTH_TOKEN` を設定するか、本当に
認証なしでよいことを `CPERSONA_ALLOW_UNAUTHENTICATED_HTTP=true` で宣言してください
(ローカル開発限定)。それ以前のバージョンは認証なしのループバック bind を許し、
「ループバックのみに bind した」とログに出していました。これは安全宣言のように
読めますが、安全宣言ではありませんでした。

`CPERSONA_ACL_FILE` の設定は、同じ要件を別の方法で満たします。全リクエストが
名前つきクライアントに解決されなければならないため、単一トークンの検査は適用
されません。

このモードでは `CPERSONA_AUTH_TOKEN` は**無視されます** (起動時に警告)。従来の
トークンを使い続けるべきクライアントは、ACL ファイルに明示的に列挙する必要が
あります。権限モデル・ファイル形式・ツール分類は [ACL 設計](ACL_DESIGN.md) を
参照してください。

### stdio プロキシ { #the-stdio-proxy }

`python -m cpersona.proxy_stdio` は、stdio しか話せないクライアントから HTTP トランスポートで
動くサーバーを使えるようにします。読んだメッセージをそのサーバーへ転送し、
`CPERSONA_AUTH_TOKEN` をベアラートークンとして送ります。

| 変数 | 既定値 | 説明 |
|----------|---------|-------------|
| `CPERSONA_REMOTE_URL` | `http://localhost:8402/mcp` | サーバーの MCP エンドポイント |
| `CPERSONA_PROXY_MAX_INFLIGHT` | 8 | stdin から読んだがまだ応答していないメッセージの数で、同時リクエスト数の上限も兼ねます。1 未満の値は 1 として扱います |

## recall の融合モード (`CPERSONA_RECALL_MODE`) { #recall-fusion-mode-cpersona_recall_mode }

- **`rrf`** (既定): Reciprocal Rank Fusion。vector と FTS のチャネルを
  **順位のみ**で融合します。頑健でスケール非依存ですが、スコアの大きさは捨てます。
- **`rsf`**: Relative Score Fusion。各チャネルの生スコア (vector はコサイン、
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
  一致が通ることがあります。`CPERSONA_CONFIDENCE_ORDERING=legacy` で confidence を
  有効にするとゲートは confidence スコア側に移り、この問題は避けられますが、その代償は
  すぐ下に書いたとおりです。
  既定は `rrf` のままです。
- **`cascade`**: チャネルを順番に埋める方式 (レガシー)。

**2.6.0a7 からは、`CPERSONA_CONFIDENCE_ENABLED=true` にしても返却順は変わりません**。
順序は融合モードが決め、confidence の値は各行の横に返されます。以前のリリースの挙動である
`CPERSONA_CONFIDENCE_ORDERING=legacy` では、confidence スコアリングが結果集合を並べ直し、
品質ゲートも融合スコアではなく confidence を見ます。

1,545 件のコーパスに 394 クエリで実測しました。その legacy の挙動で confidence を有効にすると、
`rsf` と `rrf` は **394 クエリすべてで同じ行を同じ順序**で返しました。無効時は一致が 10% 未満でした。

したがって `legacy` では、融合モードを設定してランキングの変化を期待するなら、confidence は
無効のままにするか、モードが効くのは「どの記憶が考慮されるか」であって「返ってくる順序」
ではないと理解してください。既定の `fusion` では、confidence の on / off にかかわらず
融合モードが順序を決めます。
<!-- i18n-source: docs/upgrading-to-2.6.md@blob:6624e78a48681933d87040a3e191aed80286c0ca -->

# 2.5 から 2.6 への移行 { #upgrading-from-25-to-26 }

このページは、既存の 2.5.x のストアを **2.6.0** まで一度に移行する手順をまとめたものです。
2.6 の各 pre-release は、リリースノートに自分の段の手順しか書いていません。このページは、
2.5.12 からの手順を 1 か所に集めます。

2.6.0 は 2.6 系の最初の final リリースです ([サポート方針](https://github.com/Cloto-dev/CPersona/blob/master/SUPPORT.md)
では Current)。2.5 系は Candidate になります: どのチャネルからも配られず、版を指定すれば
引き続き導入できます。

## 始める前に { #before-you-start }

1. **データベースと較正ファイルをバックアップする。**
   [バックアップと復元](operations.md#backup-and-restore)に従ってください。稼働中のまま取れる
   `sqlite3 "$CPERSONA_DB_PATH" ".backup 'cpersona-backup.db'"` に加えて、データベースの外にある
   `<CPERSONA_DB_PATH>.calibration.json` も写します。2.5 に戻す手段は、このバックアップだけです
   ([2.5 に戻す](#going-back-to-25)を参照)。
2. **埋め込みサーバーを確かめる。** すでに保存されている記録の溢れ分のノードを作るには、トークン数を
   報告するサーバー (CEmbedding 0.8.0 以降) が必要です。古いサーバーは `count: null` を返し、
   これはゼロではありません。
3. **2.6 系の最新版をインストールする。** 版を指定しない更新でもそれが入ります。2.6 系の中に
   とどめ、どの版を動かしているかが分かるよう、入った版を確かめてください:

   ```sh
   pip install --upgrade 'cpersona>=2.6,<2.7'
   pip show cpersona | grep Version
   ```

## 最初の起動で起きること { #what-the-first-start-does }

2.6 は最初の起動で、データベースをスキーマ版 13 (2.5.x のすべて) からスキーマ版 19 (2.6.3〜2.6.7a1 は 18、2.6.0〜2.6.2 は 17) まで、
1 段ずつ移行します。**保存済みの行は 1 行も書き換えません**: 各段はテーブルとトリガー、または列を 1 つ足すだけです。
失敗した段は完了として記録されないので、次の起動でやり直されます。

| スキーマ版 | 追加された版 | 足すもの | 後で作るものはあるか |
| --- | --- | --- | --- |
| 14 | 2.6.0a3 | `record_nodes`: 溢れ分の tree (埋め込み窓を超えた部分の断片) | **あり**: すでに保存されている長い記録のノード ([下記](#build-the-overflow-nodes)) |
| 15 | 2.6.0a4 | `entities`、`entity_aliases`、`entity_mentions`、`relations`: 宣言された連想 | なし |
| 16 | 2.6.0a5 | `record_blocks`: Block による到達 | **自動で作られます**: すでに保存されている記録の Block。Block による到達を off にした場合を除く ([下記](#block-reach-is-on-by-default)) |
| 17 | 2.6.0a6 | `record_block_vectors`: Block ごとのベクトル | Block と一緒に作られます |
| 18 | 2.6.3a1 | `memories` と `episodes` の `embedding_model`: 各ベクトルを作ったモデルのラベル。保存済みの行は空のラベル (不明) になります | なし: ベクトルは次に書かれるときにラベルが付きます |
| 19 | 2.6.7a2 | `block_log_clock`、`record_block_changes`: どの記録の Block が変わったかの記録。Block の索引ファイルが on の間だけ付けます ([約束 §3.4](BLOCK_CANDIDATES_CONTRACT.md#34-how-the-file-stays-exact)) | **自動で作ります**: キューで作ります。ファイルを off にした場合を除きます ([下](#the-block-index-file-is-on-by-default)) |

2.6.0a1、2.6.0a2、2.6.0a7、2.6.0a8、2.6.0b1、2.6.0b2、2.6.0、2.6.1、2.6.2、2.6.4a1、2.6.4a2、2.6.4、2.6.5a1、2.6.5a2、2.6.5a3、2.6.5、2.6.6a1、2.6.6、2.6.7a1、2.6.7、2.6.8a1 はスキーマを変えていません。

## 最初の起動の後 { #after-the-first-start }

### ゲートの較正がやり直される { #the-gate-is-recalibrated }

2.6.0a7 は採点の版を変えたので、それより前の版が保存した較正は古いものとして扱われます。既定の
`CPERSONA_CALIBRATE_ON_MODEL_CHANGE=true` なら、サーバーは起動時に全体のしきい値を較正し直します。
それと `CPERSONA_AUTO_CALIBRATE` の両方が off の場合、古いゲートは適用されず、`calibrate_threshold`
を実行するまで `deep_check` が `stale_scoring_version` を報告します。

**エージェントごとの較正はやり直されません。** `calibrate_threshold` で特定のエージェント向けに
較正したしきい値やゲートは、古い採点の版とともに破棄されます。そのエージェントは、もう一度
`calibrate_threshold` を実行するまで、全体のしきい値と経験則のゲートに戻ります。
`set_recall_precision` で設定した好みは保たれます。

### 溢れ分のノードを作る { #build-the-overflow-nodes }

移行前に保存された長い記録には、作るまでノードがありません。それまでは記録の先頭から引用され、
`node_unavailable: no_nodes` がそう伝えます。1 回で 50 件を処理する health check で作り、
残りが無いと報告されるまで繰り返します:

```text
check_health(agent_id="<id>", fix=true, checks=["missing_nodes"])
```

この修復は記録を変更しないので、ロックされた記憶にも使えます。埋め込みモデルを変えた後は、
もう一度実行してください。

### Block による到達は既定で on です { #block-reach-is-on-by-default }

2.6.0 から、Block による到達は off にしない限り on です。長い記録の埋め込み窓を超えた部分の本文を
検索で到達できるようにし、`reconstruct` は一致した Block を引用します ([設計](BLOCK_REACH_DESIGN.md))。

- **最初の起動で、上限つきのバックフィルが始まります**。すでに保存されている記録の Block を、記録を
  埋め込むのと同じ埋め込みサーバーで、Block のまとまりごとに 1 回の呼び出しで埋め込みます。この
  プロジェクト自身のストアでは、4,478 件の記録が 70,130 個の Block に分かれました。進み具合は
  `check_health` が `missing_blocks` として示し、`fix=true` で先へ進めます。記録の Block ができる
  までは、recall はその記録にほかの腕で到達します (Block による到達が off の時と同じです)。
- **Block は 2 通りに保存されます**。次元ごとに 1 ビットと、次元ごとに 1 バイトです。1,024 次元の
  モデルで Block あたり 1,152 バイトのベクトルです (128 + 1,024)。
- **recall は、ベクトルのある問い合わせのたびに索引を読み**、Block の腕だけが到達した記録のために
  `limit` を最大 2 行超えて返すことがあります。ベクトル検索がリモートの場合は効果がありません。
- **recall は遅くなり、メモリも増えます**。そのストアを 1 台のマシンで測ると、`recall` の中央値は
  Block の腕ありで 138 ms (2.5.12 は 17 ms。質問の埋め込みの時間は含みません)、プロセスのピークメモリは 180 MB (2.5.12 は 106 MB)
  でした。費用は Block の数とともに増えます。10 倍大きいストアは測っていません。
- **off にするには** `CPERSONA_BLOCK_BUILD_ENABLED=false` を設定します。埋め込みの呼び出しも、行も、
  キューの仕事も無くなり、読む側も一緒に off になります。`CPERSONA_BLOCK_RETRIEVAL_ENABLED=false`
  だけなら、索引は作られたまま読まれません。構築を off にして読む側だけを on にする設定は、起動時の
  エラーになります。

2.6.0a5 で Block の構築を on にしていた場合、その Block の集合はベクトルを持たないので、同じ
バックフィルが作り直します。

### Block の索引ファイルは既定で on です { #the-block-index-file-is-on-by-default }

2.6.7 から、Block の腕は調べる行を SQLite でなくデータベースの横のファイルから読みます。答えは同じです
([運用](operations.md#the-block-index-file))。

- **最初の起動でファイルを作ります**。キューで、収める Block ができた時点で作ります。10 万件の記憶と
  3,480,069 個の Block のストアでは 431,528,748 バイトで、Intel N150 で作るのに 11〜14 秒かかりました。
  作り終わるまで、想起は SQLite を読みます。
- **想起が速くなります**。そのストアとマシンで、実行前に登録した規則のもとで、想起の中央値は 960 ms から
  798 ms、931 ms から 844 ms に、Block の腕は約 575 ms から 64 ms に下がり、どの想起も同じ行を同じ順で
  返しました ([結果](https://github.com/Cloto-dev/CPersona/blob/master/benchmarks/measurements/results-recall-latency-block-index-file.md))。
- **書き込みに少し費用がかかります**。サーバーは書いた・消した Block の行ごとに約 16 バイトの記録を付け、
  そこでは Block の構築に足した費用は 1% 未満でした。ファイルは、作った後の記録が 35,000 件を超えると
  作り直されます。
- **Windows ではファイルをメモリに読み込みます** (マップしません)。
- **off にするには** `CPERSONA_BLOCK_INDEX=false` を設定します。サーバーは起動時に記録を止め、想起は
  SQLite を読みます。

## 変わった挙動 { #behaviour-that-changed }

配備が頼っているものと照らし合わせてください。特に断りのない限り、どれも off か、2.5 と同じです。

- **Block による到達は既定で on です** (2.6.0)。store は記録の Block をキューに入れてそう伝え
  (`blocks: {"status": "queued"}`)、recall は `limit` を最大 2 行超える予約の行を返すことがあり、
  `reconstruct` は一致した Block を引用します。off にする方法は[上](#block-reach-is-on-by-default)です。
- **Block の索引ファイルは既定で on です** (2.6.7)。最初の起動でデータベースの横にファイルを作り、想起は
  Block の腕の行をそこから読みます。答えは同じです。off にする方法は[上](#the-block-index-file-is-on-by-default)です。
- **`count` を省略した `reconstruct` は 10 項目を返します** (2.6.0。以前は 1)。Block による到達が on
  の状態で、これが 2.6.0 の推奨する、記憶から答えるための構成です。1 項目を前提にしていた配備は
  `CPERSONA_RECONSTRUCT_DEFAULT_COUNT=1` を設定してください。`CPERSONA_RECONSTRUCT_MAX_COUNT` だけを
  下げると、既定も一緒に下がります。

- **`limit` は返す行数です** (2.6.0a2)。融合がどこまで深く見るかは
  `max(limit, CPERSONA_RECALL_DEPTH_FLOOR)` で、floor の既定は 0 なので、順位は 2.5 と同じです。
- **`reconstruct` には `recall` と同じエージェントごとの読み取り権限が要ります** (2.6.0a2)。
  [アクセス制御](ACL_DESIGN.md)を設定している場合だけ関係します。
- **confidence は recall の並べ直しにもゲートにも使われなくなりました** (2.6.0a7)。
  `CPERSONA_CONFIDENCE_ENABLED=true` の場合、各行は今も `confidence` の値を持ちますが、順序は融合の
  順です。旧来の並べ直しでは上位に来ていたプロフィール行 (`update_profile`) は、最後に並び、`limit`
  が埋まると切り落とされることがあります。毎回エージェントに届けたい情報は、クライアントの指示
  ファイルかシステムプロンプトに書いてください。`CPERSONA_CONFIDENCE_ORDERING=legacy` で旧来の順に
  戻ります。
- **エピソード境界のペナルティは既定で off です** (2.6.0a7)。古いセッションを抑えるのに使っていた
  配備は `CPERSONA_EPISODE_PENALTY_ENABLED=true` を設定してください。
- **時期の手がかりは最大 3 行を足しえます** (2.6.0a8)。`time_cue` を渡すと、`recall` は最大
  `limit` + 3 行 (手がかりの確かさ別に 3 / 2 / 1) を返し、直近 24 時間だけを指す手がかりは使われません
  (`time_cue.ignored`)。手がかりを渡さなければ何も変わりません。
- **`declare_associations` は destructive と示されます** (2.6.0b1)。`retract` 引数が削除を行うため、
  destructive なツールの前に確認を求めるクライアントは、このツールの前にも確認を求めます。
- **`reconstruct` がたどるのは最大 5 段です** (2.6.0b1)。`traverse` と同じです。これより大きい
  `max_hops` は引き下げられ、`bounds.max_hops` が実際に使った値を示します。
- **別名は宣言したスコープにとどまります** (2.6.0b1)。グローバルにすでにある名前へ、プロジェクトの中から
  別名を付ける宣言は、そのプロジェクト自身の実体を登録するようになり、他のプロジェクトはその別名を
  読みません。2.6.0b1 より前にこの形で宣言された別名は、どこからでも読めるまま残ります。
  別名をどのスコープが宣言したかは、保存されたデータに記録されていないためです。
- **細かな修正** (2.6.0b1): `associations` を渡さない `store` は、空の `associations` を返さなく
  なりました。`retract` は `true` / `false` を id として受けません。次の行から始まる限定句も
  引用に付きます。エピソードの `reconstruct` の引用は保存された要約で測られ、`[Episode] ` の
  ラベルを含みません。`check_health` は、隙間や欠けた埋め込みのあるノード集合を報告して作り直します。
- **`session_key` は 256 文字までです** (2.6.0b2)。これを受けるすべてのツールが `maxLength: 256`
  を宣言し、それより長いキーは呼び出しが走る前に入力検証のエラーで拒否されます。プロセス id と
  開始時刻から作るキーや UUID は、この上限を大きく下回ります。
- **行の上限で切られた一覧はそう言います** (2.6.0b2)。`list_memories` と `list_episodes` は、
  これまでどおり `limit` を 500 行と 200 行に抑えます。呼び出し側がそれより多くを求め、上限の先に
  行がある時は、応答に `budget_rows` (上限の値) が付きます。求めた数より少なければ終わりと
  みなすクライアントは、このキーを読んでください。
- **ベクトル索引の遅れは、毎クエリがテーブルから読む行をすべて数えます** (2.6.0b2)。
  `python -m cpersona.vector_index status` に `rows_read_exactly`、`excluded`、`unembedded` が
  加わり、`build` に `unembedded` が加わります。`check_health` の `vector_index_tail_grown` は
  `rows_read_exactly` を再 build の比率と比べるので、`fix=true` が欠けた埋め込みを埋めた後は、
  `rows_past_watermark` が 0 のままでも現れることがあります。索引を build し直すと、その後に埋め込みを得た行は索引に入ります。
- **細かな修正** (2.6.0b2): メモリ上のデータベースでは、`export_memories` が一貫した複製を
  読みます。ベクトル索引の経路は、コピーが要る窓を、テーブル走査と同じ範囲で 1 チャンクずつ
  採点します。
- **`reconstruct` の予算は、見出しの引用 1 つ分に満たなければそこまで引き上げます** (2.6.0)。
  既定の `CPERSONA_RECONSTRUCT_QUOTE_CHARS` 800 では、それより小さい `budget` は 800 になり、
  `budget_policy` がそう申告します。以前は 500 までしか引き上げず、見出しが 800 まで伸びても
  超過は報告されませんでした。
- **recall の抜粋は、Block の検索が on のときだけ Block 索引を読みます** (2.6.0)。
  `reconstruct` の引用と同じ扱いです。`CPERSONA_BLOCK_BUILD_ENABLED=true` で
  `CPERSONA_BLOCK_RETRIEVAL_ENABLED=false` なら、`excerpt_basis` は `lexical` になります。
- **Block の集合は、ノードの区切りを守り、再順位付けのベクトルがすべてそろっているときだけ
  最新とみなします** (2.6.0)。Block の構築が on の配備では、レコードのノードができる前に作られた
  集合を、移行後に backfill と `check_health(fix=true)` が作り直します。
- **既定以外の `CPERSONA_PRIOR_FAR_WEIGHT` は較正の一部になります** (2.6.0)。1 以外を設定した
  配備は、2.6.0 の最初の起動で較正し直します。別の重みで測ったゲートは復元されないためです。
  既定のままなら何も変わりません。
- **時間の手がかりは、既定で粗探索の索引を通して走査窓の外を探します** (2.6.4a2)。
  `CPERSONA_CUE_COARSE_ENABLED` が未設定なら、手がかりの期間のうち走査窓より後ろの部分を、
  粗探索の索引があれば探し、無ければ探さずに `time_cue.remainder` でそう伝えます。保存済みの
  ベクトルを全部読むことはありません。窓に収まるストアでは何も変わりません。`true` と `false` の
  意味は変わりません ([設定](BINARY_COARSE_SEARCH_DESIGN.md#7-settings))。
- **サーバーが instructions を送り、3 つのツールがセッションと一緒に読み込まれます**
  (2.6.5a2)。`initialize` の instructions は、CPersona のツールをいつ使うかの案内
  (約 500 文字) で始まり、運用コンテキストの summary が設定されていればそのあとに続きます。
  以前は summary が無いと空でした。Claude Code では `reconstruct`・`store`・`archive_episode`
  がツール検索を通さずセッションの開始時に読み込まれるので、どのセッションもこの 3 つの定義を
  持ち、起動はサーバーのツールを最大 5 秒待ちます。ツールの説明文 5 本が短くなり、外した細部は
  説明文が名前を挙げる設計文書にあります。
- **答えの脇に置いた行は recall の回数を得ません** (2.6.0)。confidence が有効なとき、Block の
  別枠、時期の別枠、関連の別枠は `recall_count` を増やさず、その `confidence` は
  その行自身の履歴を読みます。
- **細かな修正** (2.6.0): preview の長さで切られた `reconstruct` の引用は、`expand` で
  それが始まった範囲全体を渡します。`shortfall_reason` は、予算が窓の中の項目を切ったときだけ
  予算のせいにします。`bounds.reached` は recall の上限の内側の行だけを数えます。表せる範囲を
  超える `time_cue` は例外にせず範囲の端で止め、文字列でない単位は拒否します。失敗するノードや
  Block の構築が、待ち行列の他のタスクを止めなくなりました。`api` の埋め込みモードでは、ノードの
  点検が全レコードの本文を読まなくなりました。

2.6 で新しく入り、求めない限り何もしないもの: `reconstruct` ツール、recall の trace (`trace=true`)、
時期の手がかり (`time_cue`)、`declare_associations` または `store` で宣言する連想、関連の別枠
(`CPERSONA_RECALL_PROPAGATION_SEAT`)。

## 結果を確かめる { #checking-the-result }

- `check_health(agent_id="<id>", checks=["missing_nodes"])` が、作るべき記録は残っていないと報告する。
- `deep_check` が `stale_scoring_version` を報告しない。
- 答えを知っている recall をいくつか試し、それが返ってくる。

## 2.5 に戻す { #going-back-to-25 }

移行の前に取ったバックアップ (データベースと較正ファイル) を復元してから、2.5.12 をインストールします。
**移行済みのデータベースを 2.5 で開かないでください。** 2.5.12 は新しいスキーマを「誤読するかもしれない」
という警告付きで開くだけで、スキーマ版 17 のデータベースを元に戻す手段はありません。
